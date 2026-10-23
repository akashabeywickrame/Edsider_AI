"""A paper's way into the question bank. When data entry finishes, its
questions are copied to `questions` (their places on the paper to
`question_assessments`, their pictures to `question_files`) and the paper
stays in intake for the teacher. When the teacher's review is done, the
copy is refreshed and the paper is ready to publish. An administrator then
archives it: it leaves intake, is read back from the bank, and cannot
change."""

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import files, store
import service
from service import FinishIn

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


def _checked_paper():
    with store.connect() as conn:
        doc_id = store.create_intake_document(conn, "GIT.pdf", paper_grade="G12",
                                              paper_subject="General Information Technology",
                                              sections=SECTIONS)
        store.add_extracted_questions(conn, doc_id, DRAFTS)
        qs = {q["seq"]: q for q in store.intake_questions(conn, doc_id)}
        image_id = store.add_question_image(conn, qs[4]["id"], "question", PNG, "image/png")
        store.update_intake_document(conn, doc_id, status="checked")
    return doc_id, qs, image_id


def _bank(doc_id):
    with store.connect() as conn:
        rows = {r["id"]: r for r in conn.execute(
            "SELECT * FROM questions WHERE id LIKE ?", (f"intake-{doc_id}-%",)).fetchall()}
        links = {r["question_id"]: r for r in conn.execute(
            "SELECT * FROM question_assessments WHERE assessment_id = ?",
            (f"intake-{doc_id}",)).fetchall()}
        pictures = conn.execute("SELECT * FROM question_files WHERE question_id = ANY(?)",
                                (list(rows),)).fetchall()
    return rows, links, pictures


def test_finishing_data_entry_copies_the_paper_and_keeps_it_for_the_teacher(local_db):
    doc_id, qs, image_id = _checked_paper()
    doc = service.finish_data_entry(doc_id)
    assert doc.status == "data_entry_done" and not doc.archived_at

    rows, links, pictures = _bank(doc_id)
    stem, part_b = (f"intake-{doc_id}-{qs[n]['id']}" for n in (2, 4))
    assert len(rows) == 4
    assert rows[stem]["shape"] == "parent" and rows[part_b]["parent_id"] == stem
    assert rows[part_b]["grade"] == "G12"
    # Its place on the paper, as EdSider keeps it: a 0-mark parent, the
    # parts their own marks, all numbered 1 in their part.
    assert (links[stem]["mark_allocation"], links[part_b]["mark_allocation"]) == (0, 6)
    assert {links[k]["question_number"] for k in (stem, part_b)} == {1}
    assert (links[part_b]["printed_number"], links[part_b]["position"]) == ("01(b)", 4)
    assert [(p["id"], p["question_id"]) for p in pictures] == [(image_id, part_b)]
    with store.connect() as conn:
        assert len(store.intake_questions(conn, doc_id)) == 4     # still in intake


def test_the_teachers_corrections_reach_the_bank_and_archiving_reads_it_back(
        local_db, monkeypatch):
    for var in ("EDSIDER_REQUIRE_SIGNIN", "EDSIDER_API_KEY", "WEBSITE_SITE_NAME"):
        monkeypatch.delenv(var, raising=False)
    doc_id, qs, image_id = _checked_paper()
    service.finish_data_entry(doc_id)
    with store.connect() as conn:
        store.assign_document(conn, doc_id, "t@gmail.com")
        # The teacher corrects (a) during review.
        store.update_intake_question(conn, qs[3]["id"], text="Define the term system clearly.")
    done = service.finish_review(doc_id, FinishIn(status="review_done"))
    assert done.status == "ready_to_publish"
    rows, _, _ = _bank(doc_id)
    assert rows[f"intake-{doc_id}-{qs[3]['id']}"]["text"] == "Define the term system clearly."

    with store.connect() as conn:
        before = store.intake_questions(conn, doc_id)
    archived = service.archive_document(doc_id)
    assert archived.status == "archived" and archived.archived_at

    with store.connect() as conn:
        left = conn.execute("SELECT count(*) AS n FROM intake.question WHERE document_id = ?",
                            (doc_id,)).fetchone()["n"]
        after = store.intake_questions(conn, doc_id)
        images = store.question_images_for_document(conn, doc_id)
        picture = store.get_question_image(conn, image_id)
    assert left == 0                                              # nothing stored twice
    keys = ("id", "seq", "parent_seq", "question_number", "depth", "section", "marks", "text")
    assert [{k: q[k] for k in keys} for q in after] == [{k: q[k] for k in keys} for q in before]
    assert [i["id"] for i in images[qs[4]["id"]]] == [image_id]
    assert picture["image_blob"] == PNG                           # still in file storage

    client = TestClient(service.app)
    assert client.patch(f"/intake/documents/{doc_id}", json={"notes": "x"}).status_code == 409
    assert client.post(f"/intake/documents/{doc_id}/archive").status_code == 409
    assert client.get(f"/intake/documents/{doc_id}/questions").status_code == 200


def test_each_step_waits_for_the_one_before(local_db):
    with store.connect() as conn:
        doc_id = store.create_intake_document(conn, "p.pdf")
        store.add_extracted_questions(conn, doc_id, DRAFTS[:1])
    for step in (service.finish_data_entry, service.archive_document):
        with pytest.raises(HTTPException) as err:
            step(doc_id)
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
