"""Data entry's last step: a finished paper's questions go into the question
bank (`questions`, its links in `question_assessments`, its pictures in
`question_files`) and the intake paper is archived - read back from the
bank from then on, and closed to every change."""

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import files, store
import service

PNG = b"\x89PNG\r\n\x1a\n a diagram"
SECTIONS = [{"label": "Part I", "kind": "mcq"}, {"label": "Part II", "kind": "structured"}]
DRAFTS = [
    {"seq": 1, "question_number": "1", "section": "Part I", "marks": 1, "answer": "(2)",
     "text": "Which is an output device?\n(1) Mouse\n(2) Printer\n(3) Scanner\n(4) Keyboard"},
    {"seq": 2, "question_number": "01", "section": "Part II", "text": "A school library system."},
    {"seq": 3, "question_number": "01(a)", "section": "Part II", "parent_seq": 2, "depth": 1,
     "marks": 4, "text": "Define the term system.", "answer": "An organised whole."},
    {"seq": 4, "question_number": "01(b)", "section": "Part II", "parent_seq": 2, "depth": 1,
     "marks": 6, "text": "Draw the block diagram."},
]


def _finished_paper():
    with store.connect() as conn:
        doc_id = store.create_intake_document(conn, "GIT.pdf", paper_grade="G12",
                                              paper_subject="General Information Technology",
                                              sections=SECTIONS)
        store.add_extracted_questions(conn, doc_id, DRAFTS)
        qs = {q["seq"]: q for q in store.intake_questions(conn, doc_id)}
        image_id = store.add_question_image(conn, qs[4]["id"], "question", PNG, "image/png")
        store.update_intake_document(conn, doc_id, status="checked")
    return doc_id, qs, image_id


def test_finishing_moves_the_paper_into_the_question_bank_and_archives_it(local_db):
    doc_id, before, image_id = _finished_paper()
    doc = service.finish_data_entry(doc_id)
    assert doc.status == "published" and doc.archived_at

    with store.connect() as conn:
        bank = {r["id"]: r for r in conn.execute(
            "SELECT * FROM questions WHERE id LIKE ?", (f"intake-{doc_id}-%",)).fetchall()}
        links = {r["question_id"]: r for r in conn.execute(
            "SELECT * FROM question_assessments WHERE assessment_id = ?",
            (f"intake-{doc_id}",)).fetchall()}
        left = conn.execute("SELECT count(*) AS n FROM intake.question WHERE document_id = ?",
                            (doc_id,)).fetchone()["n"]
        after = store.intake_questions(conn, doc_id)
        images = store.question_images_for_document(conn, doc_id)
        picture = store.get_question_image(conn, image_id)

    stem, part_b = f"intake-{doc_id}-2", f"intake-{doc_id}-4"
    assert set(bank) == {f"intake-{doc_id}-{n}" for n in (1, 2, 3, 4)}
    assert bank[stem]["shape"] == "parent" and bank[part_b]["parent_id"] == stem
    assert bank[part_b]["grade"] == "G12"
    # Its place on the paper, as EdSider keeps it: a 0-mark parent, the
    # parts their own marks, all numbered 1 in their part.
    assert (links[stem]["mark_allocation"], links[part_b]["mark_allocation"]) == (0, 6)
    assert {links[k]["question_number"] for k in (stem, part_b)} == {1}
    assert links[part_b]["printed_number"] == "01(b)" and links[part_b]["section_id"] == "Part II"
    assert left == 0                                      # nothing stored twice

    # Read back from the bank, the same as it was.
    assert [(q["id"], q["question_number"], q["depth"], q["section"], q["marks"])
            for q in after] == [(before[n]["id"], before[n]["question_number"],
                                 before[n]["depth"], before[n]["section"], before[n]["marks"])
                                for n in (1, 2, 3, 4)]
    assert after[0]["answer"] == "(2)"
    assert [i["id"] for i in images[before[4]["id"]]] == [image_id]
    assert picture["image_blob"] == PNG                  # still in file storage


def test_an_archived_paper_cannot_change_and_cannot_go_twice(local_db, monkeypatch):
    for var in ("EDSIDER_REQUIRE_SIGNIN", "EDSIDER_API_KEY", "WEBSITE_SITE_NAME"):
        monkeypatch.delenv(var, raising=False)
    doc_id, _, _ = _finished_paper()
    service.finish_data_entry(doc_id)
    client = TestClient(service.app)
    assert client.patch(f"/intake/documents/{doc_id}", json={"notes": "x"}).status_code == 409
    assert client.post(f"/intake/documents/{doc_id}/finish-data-entry").status_code == 409
    assert client.get(f"/intake/documents/{doc_id}/questions").status_code == 200


def test_a_paper_cannot_go_before_its_duplicate_check(local_db):
    with store.connect() as conn:
        doc_id = store.create_intake_document(conn, "p.pdf")
        store.add_extracted_questions(conn, doc_id, DRAFTS[:1])
    with pytest.raises(HTTPException) as err:
        service.finish_data_entry(doc_id)
    assert err.value.status_code == 409


def test_without_file_storage_files_stay_in_the_database(local_db, monkeypatch):
    """An App Service without the storage settings keeps files in the
    database, not on its own disk, which every deploy replaces."""
    monkeypatch.setattr(files, "_backend", None)
    monkeypatch.delenv("EDSIDER_BLOB_CONNECTION_STRING", raising=False)
    monkeypatch.delenv("EDSIDER_FILES_DIR", raising=False)
    monkeypatch.setenv("WEBSITE_SITE_NAME", "eds-dev-ai-api")
    assert not files.available()
    with store.connect() as conn:
        doc_id = store.create_intake_document(conn, "p.pdf", file_blob=b"%PDF")
        row = conn.execute("SELECT file_path, file_blob FROM intake.document_file "
                           "WHERE document_id = ?", (doc_id,)).fetchone()
        assert store.intake_file_blob(conn, doc_id) == b"%PDF"
    assert row["file_path"] is None and bytes(row["file_blob"]) == b"%PDF"
    assert files.describe() == "database (no file storage set)"
