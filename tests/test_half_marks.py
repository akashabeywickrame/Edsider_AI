"""Half marks - 2.5, 1.5 - read from a paper, entered by data entry, kept
exactly in intake and on the paper's bank copy, and sent as they are."""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import store
from src.intake import extract
import service


# ----------------------------------------------------------- read from a paper

def test_a_half_mark_is_read_from_the_paper():
    drafts = extract.segment("1. Define a system. (2.5 marks)\n2. Name a device. [1.5]\n")
    assert [d["marks"] for d in drafts] == [2.5, 1.5]
    assert [d["text"] for d in drafts] == ["Define a system.", "Name a device."]


def test_two_and_a_half_marks_is_not_read_as_five():
    """Read as whole numbers only, the "5" after the point was itself a
    number followed by "marks"."""
    assert extract.segment("1. Explain the term. 2.5 marks\n")[0]["marks"] == 2.5


def test_a_whole_mark_stays_a_whole_number():
    marks = extract.segment("1. Define a system. (2 marks)\n2. Name it. (3.0 marks)\n")
    assert [(d["marks"], type(d["marks"])) for d in marks] == [(2, int), (3, int)]


def test_half_marked_parts_add_up_to_their_question():
    text = ("1. A farmer grows two crops on the same land.\n"
            "(i) Explain what is meant by intercropping in this context. [2.5]\n"
            "(ii) State two advantages of the practice for the farmer. [1.5]\n")
    marks = {d["question_number"]: d["marks"] for d in extract.segment(text)}
    assert (marks["1(i)"], marks["1(ii)"]) == (2.5, 1.5)
    assert marks["1"] == 4 and isinstance(marks["1"], int)


def test_marks_add_up_as_a_person_would():
    assert extract.mark_total([0.1, 0.2]) == 0.3
    assert extract.mark_total([2.5, 1.5]) == 4


# ------------------------------------------------------------ the database

def test_a_database_with_whole_number_marks_is_changed_once(local_db):
    """The live database had `marks INTEGER`; the next start changes it,
    keeping every mark, and the start after that leaves it alone."""
    with store.connect() as conn:
        doc_id = store.create_intake_document(conn, "p.pdf")
        store.add_extracted_questions(conn, doc_id, [{"seq": 1, "text": "Q", "marks": 3}])
        conn.execute("ALTER TABLE intake.question ALTER COLUMN marks TYPE INTEGER")
        assert not store._schema_is_current(conn)
    store.use_database(store.DSN, schema=store.SCHEMA_NAME)    # as a fresh start
    with store.connect() as conn:
        assert store._schema_is_current(conn)
        store.update_intake_question(conn, store.intake_questions(conn, doc_id)[0]["id"],
                                     marks=2.5)
        assert store.intake_questions(conn, doc_id)[0]["marks"] == 2.5


# ------------------------------------------------------------ entered, stored

@pytest.fixture
def paper(local_db, monkeypatch):
    for var in ("EDSIDER_REQUIRE_SIGNIN", "EDSIDER_API_KEY", "WEBSITE_SITE_NAME"):
        monkeypatch.delenv(var, raising=False)
    with store.connect() as conn:
        doc_id = store.create_intake_document(conn, "p.pdf", paper_grade="G12")
        store.add_extracted_questions(conn, doc_id, [
            {"seq": 1, "question_number": "01", "text": "A school library system."},
            {"seq": 2, "question_number": "01(a)", "parent_seq": 1, "depth": 1,
             "marks": 2.5, "text": "Define the term system."},
            {"seq": 3, "question_number": "01(b)", "parent_seq": 1, "depth": 1,
             "marks": 4, "text": "Draw the block diagram."},
        ])
        qs = {q["seq"]: q for q in store.intake_questions(conn, doc_id)}
    return doc_id, qs


def test_data_entry_can_enter_half_marks(paper):
    doc_id, qs = paper
    client = TestClient(service.app)
    one = client.patch(f"/intake/questions/{qs[3]['id']}", json={"marks": 1.5})
    assert one.status_code == 200 and one.json()["marks"] == 1.5
    many = client.post(f"/intake/documents/{doc_id}/marks",
                       json={"marks": [{"question_id": qs[2]["id"], "marks": 0.5}]})
    assert many.status_code == 200
    with store.connect() as conn:
        stored = {q["seq"]: q["marks"] for q in store.intake_questions(conn, doc_id)}
    assert stored == {1: None, 2: 0.5, 3: 1.5}


def test_marks_cannot_be_negative(paper):
    _doc_id, qs = paper
    client = TestClient(service.app)
    assert client.patch(f"/intake/questions/{qs[2]['id']}",
                        json={"marks": -1}).status_code == 422


def test_the_bank_copy_keeps_a_half_mark_exactly(paper):
    """`questions.marks` is a whole number - changing the 690 MB bank's
    column would lock it for the whole rewrite - so a half mark is kept on
    the paper's link and read back from there once the paper is archived."""
    doc_id, qs = paper
    with store.connect() as conn:
        store.sync_paper_to_bank(conn, doc_id)
        link = {r["question_id"]: r for r in conn.execute(
            "SELECT question_id, marks, mark_allocation FROM question_assessments "
            "WHERE assessment_id = ?", (f"intake-{doc_id}",)).fetchall()}
        bank = {r["id"]: r["marks"] for r in conn.execute(
            "SELECT id, marks FROM questions WHERE id LIKE ?", (f"intake-{doc_id}-%",))}
    half, whole = (f"intake-{doc_id}-{qs[n]['id']}" for n in (2, 3))
    assert (link[half]["marks"], link[half]["mark_allocation"]) == (2.5, 2.5)
    assert (bank[half], bank[whole]) == (None, 4)

    with store.connect() as conn:
        store.update_intake_document(conn, doc_id, status="ready_to_publish")
        store.archive_document(conn, doc_id)
        back = {q["question_number"]: q["marks"] for q in store.intake_questions(conn, doc_id)}
    assert back == {"01": None, "01(a)": 2.5, "01(b)": 4}
