"""Paper intake in its own schema: normalised tables (a paper's grade and
subject are ids on EdSider's lists, a question does not repeat them, parts,
group instructions and settings are rows of their own), papers assigned to
Data Entry Operators who alone may work on them, and the one-off copy of
the papers the old public-schema tables hold."""

import json
import sys
from pathlib import Path

import numpy as np
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import store
from src.edsider import labels
import service
from service import DocumentUpdate, StaffIn, add_staff_member, get_document, update_document

SUPER = {"identity": "boss@example.com", "name": "Boss", "role": "superadmin"}
WHO = "x-ms-client-principal-name"
ADMIN, OP1, OP2, TEACHER, TEACHER2 = ("adm@example.com", "op1@example.com",
                                      "op2@example.com", "tea@example.com", "tea2@example.com")
PAPER_TEXT = ("1. Which device is an output device?\n(1) Mouse\n(2) Printer\n(3) Scanner\n"
              "(4) Keyboard\n2. Define the term system. (2 marks)\n")


def test_the_label_columns_are_edsiders_lists():
    assert store.DOCUMENT_LABELS == labels.LIST_FOR


def test_every_route_naming_a_paper_checks_who_has_it():
    """A route with a paper, question or picture in its path that forgets
    paper_access would let any Data Entry Operator open any paper."""
    missing = []
    for route in service.app.routes:
        path = getattr(route, "path", "")
        if not any(f"{{{k}}}" in path for k in ("doc_id", "q_id", "image_id")):
            continue
        calls, stack = [], list(route.dependant.dependencies)
        while stack:
            d = stack.pop()
            calls.append(d.call)
            stack.extend(d.dependencies)
        if service.paper_access not in calls:
            missing.append(f"{sorted(route.methods)} {path}")
    assert not missing


def _new_paper(conn, **kw):
    return store.create_intake_document(conn, "paper.pdf", file_type="pdf", **kw)


def test_grade_and_subject_are_ids_on_edsiders_lists(local_db):
    with store.connect() as conn:
        # What the extractor read: a grade EdSider has, a subject name it
        # does not ("Combined Maths" is EdSider's name) - left unset.
        doc_id = _new_paper(conn, paper_grade="G13", paper_subject="Combined Mathematics")
        store.add_extracted_questions(conn, doc_id, [{"seq": 1, "text": "Prove it."}])
        row = conn.execute("SELECT grade_id, subject_id FROM intake.document WHERE id = ?",
                           (doc_id,)).fetchone()
        intake_schema = store.schema_names()[0]
        question_columns = {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = 'question'", (intake_schema,))}
    assert (row["grade_id"], row["subject_id"]) == (13, None)
    assert not {"grade", "subject", "depth"} & question_columns

    doc = update_document(doc_id, DocumentUpdate(subject_id=13))
    assert (doc.paper_grade, doc.paper_subject) == ("G13", "Combined Maths")
    with store.connect() as conn:
        assert store.intake_questions(conn, doc_id)[0]["subject"] == "Combined Maths"
    # By name or code too - the older callers' way - but only what is listed.
    assert update_document(doc_id, DocumentUpdate(paper_grade="12")).grade_id == 12
    for bad in (DocumentUpdate(subject_id=999), DocumentUpdate(paper_grade="G99"),
                DocumentUpdate(status="lost")):
        with pytest.raises(HTTPException) as err:
            update_document(doc_id, bad)
        assert err.value.status_code == 400


def test_parts_groups_and_settings_are_rows_of_their_own(local_db):
    sections = [{"label": "Part I", "heading": "Part I", "kind": "mcq",
                 "instructions": "Answer all.", "marks_each": 2, "questions": 2},
                {"label": "Part II", "heading": "Part II", "kind": "structured",
                 "instructions": None, "marks_each": None, "questions": 1}]
    drafts = [{"seq": 1, "text": "Pick one.", "section": "Part I"},
              {"seq": 2, "text": "Pick another.", "section": "Part I"},
              {"seq": 3, "text": "Explain:", "section": "Part II"},
              {"seq": 4, "text": "(a) the first thing at length", "section": "Part II",
               "parent_seq": 3},
              {"seq": 5, "text": "(i) a detail of the first thing", "section": "Part II",
               "parent_seq": 4}]
    settings = {"schedule_at": "2026-11-02T09:30", "time_limit": True, "duration_value": 120,
                "duration_unit": "minutes", "pause_option": False, "part_notation": "roman",
                "parts": 2,
                "marking": {"parts": {"Part I": {"question_count": 2, "answer_count": 2,
                                                 "marks_each": 2},
                                      "Part II": {"question_count": 1, "answer_count": 1,
                                                  "marks_each": None}},
                            "notes": "", "total": 4, "target_total": 100, "scale": 25}}
    with store.connect() as conn:
        doc_id = _new_paper(conn, sections=sections, instruction_groups=[
            {"section": "Part I", "from": "1", "to": "2", "text": "Use the chart."}])
        store.add_extracted_questions(conn, doc_id, drafts)
        store.update_intake_document(conn, doc_id, assessment_settings=settings)
        doc = store.get_intake_document(conn, doc_id)
        parts_before = conn.execute("SELECT id, label FROM intake.part WHERE document_id = ? "
                                    "ORDER BY seq", (doc_id,)).fetchall()
        questions = store.intake_questions(conn, doc_id)

    assert [s["label"] for s in doc["sections"]] == ["Part I", "Part II"]
    assert [s["questions"] for s in doc["sections"]] == [2, 1]
    assert doc["instruction_groups"] == [{"section": "Part I", "from": "1", "to": "2",
                                          "title": None, "text": "Use the chart."}]
    assert doc["assessment_settings"] == {**settings, "marking": {**settings["marking"],
                                                                  "scale": 25}}
    assert [q["depth"] for q in questions] == [0, 0, 0, 1, 2]
    assert [q["section"] for q in questions] == ["Part I"] * 2 + ["Part II"] * 3

    # Editing a part's instructions keeps the part - and its questions.
    edited = [dict(s, instructions="Answer any four.") if s["label"] == "Part II" else s
              for s in doc["sections"]]
    with store.connect() as conn:
        store.update_intake_document(conn, doc_id, sections=edited)
        assert conn.execute("SELECT id, label FROM intake.part WHERE document_id = ? "
                            "ORDER BY seq", (doc_id,)).fetchall() == parts_before
        # A re-split replaces the parts with the new reading's.
        store.replace_intake_questions(conn, doc_id, [{"seq": 1, "text": "Only one.",
                                                       "section": "Section A"}],
                                       sections=[{"label": "Section A", "kind": None}],
                                       instruction_groups=[])
        doc = store.get_intake_document(conn, doc_id)
    assert [s["label"] for s in doc["sections"]] == ["Section A"]
    assert doc["instruction_groups"] == []


# ------------------------------------------------ who may work on a paper

class FakeEmbedder:
    def encode(self, texts, is_query=False):
        if isinstance(texts, str):
            texts = [texts]
        out = []
        for i, _ in enumerate(texts):
            v = np.zeros(8)
            v[i % 8] = 1.0
            out.append(v)
        return out


@pytest.fixture
def client(local_db, monkeypatch):
    for var in ("EDSIDER_REQUIRE_SIGNIN", "EDSIDER_BOOTSTRAP_ADMINS", "EDSIDER_API_KEY",
                "WEBSITE_SITE_NAME"):
        monkeypatch.delenv(var, raising=False)
    for email, role in ((ADMIN, "admin"), (OP1, "employee"), (OP2, "employee"),
                        (TEACHER, "teacher"), (TEACHER2, "teacher")):
        add_staff_member(StaffIn(email=email, name=email.split("@")[0], role=role),
                         user=SUPER)
    return TestClient(service.app)


def call(client, who, method, path, **kw):
    return client.request(method, path, headers={WHO: who}, **kw)


def test_only_an_administrator_uploads_and_only_the_assigned_operator_works(client):
    paste = {"text": PAPER_TEXT, "title": "GIT Gr12"}
    assert call(client, OP1, "POST", "/intake/documents/from-text", json=paste).status_code == 403
    doc = call(client, ADMIN, "POST", "/intake/documents/from-text", json=paste).json()
    path = f"/intake/documents/{doc['id']}"

    # Nobody's yet: no operator can open it, and it shows as unassigned.
    assert call(client, OP1, "GET", path).status_code == 403
    unassigned = call(client, ADMIN, "GET", "/intake/documents/search?operator=none").json()
    assert [d["id"] for d in unassigned["items"]] == [doc["id"]]
    assert call(client, ADMIN, "POST", f"{path}/operator",
                json={"operator_identity": TEACHER}).status_code == 404

    given = call(client, ADMIN, "POST", f"{path}/operator", json={"operator_identity": OP1})
    assert given.json()["operator"] == OP1
    assert call(client, OP1, "GET", path).status_code == 200
    assert call(client, OP2, "GET", path).status_code == 403
    q = call(client, OP1, "GET", f"{path}/questions").json()[0]
    assert call(client, OP2, "PATCH", f"/intake/questions/{q['id']}",
                json={"reviewed": True}).status_code == 403
    assert call(client, OP1, "GET", "/intake/documents/search").json()["total"] == 1
    assert call(client, OP2, "GET", "/intake/documents/search").json()["total"] == 0
    # Asking for someone else's papers still shows only their own.
    assert call(client, OP2, "GET", f"/intake/documents/search?operator={OP1}").json()["total"] == 0

    # Reassigned: the first operator loses it.
    call(client, ADMIN, "POST", f"{path}/operator", json={"operator_identity": OP2})
    assert call(client, OP1, "GET", path).status_code == 403
    assert call(client, OP2, "GET", path).status_code == 200


def test_a_paper_goes_operator_teacher_and_back(client):
    doc = call(client, ADMIN, "POST", "/intake/documents/from-text",
               json={"text": PAPER_TEXT, "title": "GIT Gr12"}).json()
    path = f"/intake/documents/{doc['id']}"
    call(client, ADMIN, "POST", f"{path}/operator", json={"operator_identity": OP1})
    qs = call(client, OP1, "GET", f"{path}/questions").json()
    for q in qs:
        assert call(client, OP1, "PATCH", f"/intake/questions/{q['id']}",
                    json={"reviewed": True}).status_code == 200

    service.state["embedder"] = FakeEmbedder()
    try:
        assert call(client, OP1, "POST", f"{path}/check-duplicates").status_code == 200
    finally:
        service.state["embedder"] = None
    assert call(client, OP1, "GET", path).json()["status"] == "checked"
    # Finishing copies it to the question bank and passes it on.
    done = call(client, OP1, "POST", f"{path}/finish-data-entry")
    assert done.status_code == 200 and done.json()["status"] == "data_entry_done"

    # With its two teachers: each may open it and tick it, the operator may
    # only look.
    assert call(client, TEACHER, "GET", path).status_code == 403
    assert call(client, ADMIN, "POST", f"{path}/assign",
                json={"teacher_identities": [TEACHER]}).status_code == 422   # two, not one
    call(client, ADMIN, "POST", f"{path}/assign", json={"teacher_identities": [TEACHER, TEACHER2]})
    q = f"/intake/questions/{qs[0]['id']}"
    assert call(client, OP1, "PATCH", q, json={"text": "changed"}).status_code == 409
    assert call(client, OP1, "GET", path).status_code == 200

    # Each teacher's ticks are their own.
    assert call(client, TEACHER, "PATCH", q, json={"teacher_reviewed": True}).json()["teacher_reviewed"]
    ticks = {t: [x["teacher_reviewed"] for x in call(client, t, "GET", f"{path}/questions").json()]
             for t in (TEACHER, TEACHER2)}
    assert ticks[TEACHER][0] is True and ticks[TEACHER2][0] is False

    # One finishes: still with the teachers, and that one can change nothing.
    done = call(client, TEACHER, "POST", f"{path}/finish", json={"status": "review_done"}).json()
    assert done["status"] == "teacher_review" and done["teachers_done"] == [TEACHER]
    assert call(client, TEACHER, "PATCH", q, json={"teacher_reviewed": False}).status_code == 409

    # The other sends it back: the operator can change it again, no teacher can.
    assert call(client, TEACHER2, "POST", f"{path}/finish",
                json={"status": "refer_back"}).status_code == 200
    assert call(client, OP1, "PATCH", q, json={"text": "fixed"}).status_code == 200
    assert call(client, TEACHER2, "PATCH", q, json={"teacher_reviewed": True}).status_code == 409

    # Data entry finishes again: straight back to both teachers, both to review again.
    service.state["embedder"] = FakeEmbedder()
    try:
        call(client, OP1, "POST", f"{path}/check-duplicates")
    finally:
        service.state["embedder"] = None
    again = call(client, OP1, "POST", f"{path}/finish-data-entry").json()
    assert again["status"] == "teacher_review" and again["teachers_done"] == []
    call(client, TEACHER, "POST", f"{path}/finish", json={"status": "review_done"})
    assert call(client, ADMIN, "GET", path).json()["status"] == "teacher_review"
    last = call(client, TEACHER2, "POST", f"{path}/finish", json={"status": "review_done"}).json()
    assert last["status"] == "ready_to_publish"

    history = call(client, ADMIN, "GET", f"{path}/history").json()
    actions = [(h["action"], h.get("stage")) for h in history]
    assert actions[:9] == [
        ("uploaded", None), ("assigned", "data_entry"), ("status", None),
        ("copied to the question bank", None), ("finished", "data_entry"), ("status", None),
        ("assigned", "teacher"), ("assigned", "teacher"), ("status", None)]
    assert history[0]["actor"] == ADMIN and history[1]["assigned_to"] == OP1
    assert {h["assigned_to"] for h in history if h["action"] == "assigned"
            and h.get("stage") == "teacher"} == {TEACHER, TEACHER2}


# ----------------------------------------- the copy from the old tables

OLD_TABLES = """
CREATE TABLE intake_documents (
    id INTEGER PRIMARY KEY, filename TEXT NOT NULL, file_type TEXT, extractor TEXT,
    uploaded_by TEXT, paper_grade TEXT, paper_subject TEXT, notes TEXT, raw_text TEXT,
    split_style TEXT, status TEXT NOT NULL DEFAULT 'extracted', assigned_to TEXT,
    uploaded_at TIMESTAMPTZ DEFAULT now(), paper_title TEXT, sections TEXT, file_blob BYTEA,
    assessment_settings TEXT, instruction_groups TEXT, edsider_labels TEXT);
CREATE TABLE intake_questions (
    id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL, seq INTEGER NOT NULL,
    question_number TEXT, text TEXT, grade TEXT, subject TEXT, marks INTEGER, topic TEXT,
    answer TEXT, extraction_confidence TEXT DEFAULT 'ok', reviewed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP, section TEXT, parent_seq INTEGER,
    depth INTEGER NOT NULL DEFAULT 0, teacher_reviewed INTEGER NOT NULL DEFAULT 0,
    question_type TEXT, last_edited_by TEXT, last_edited_at TIMESTAMPTZ);
CREATE TABLE intake_question_comments (
    id INTEGER PRIMARY KEY, question_id INTEGER NOT NULL, author TEXT, comment TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE intake_question_images (
    id INTEGER PRIMARY KEY, question_id INTEGER NOT NULL, slot TEXT NOT NULL,
    image_blob BYTEA NOT NULL, content_type TEXT, created_at TIMESTAMPTZ DEFAULT now());
"""


def test_the_old_tables_are_copied_once_keeping_their_ids(local_db):
    add_staff_member(StaffIn(email=OP1, name="Op", role="employee"), user=SUPER)
    with store.connect() as conn:
        conn.executescript(OLD_TABLES)
        conn.execute(
            """INSERT INTO intake_documents
               (id, filename, file_type, extractor, uploaded_by, paper_grade, paper_subject,
                raw_text, status, assigned_to, paper_title, sections, file_blob,
                assessment_settings, instruction_groups, edsider_labels)
               VALUES (41, 'git.pdf', 'pdf', 'pdfplumber', ?, 'G12', 'Combined Mathematics',
                       'the text', 'teacher_review', ?, 'First Term', ?, ?, ?, ?, ?)""",
            (OP1, TEACHER,
             json.dumps([{"label": "Part I", "heading": "Part I", "kind": "mcq",
                          "instructions": None, "marks_each": 1, "questions": 1}]),
             b"%PDF-1.4",
             json.dumps({"duration_value": 90, "foo": 1,
                         "marking": {"parts": {"Part I": {"question_count": 1,
                                                          "answer_count": 1,
                                                          "marks_each": 1}},
                                     "notes": "n", "total": 1, "target_total": None}}),
             json.dumps([{"section": "Part I", "from": "1", "to": "1", "text": "Read."}]),
             json.dumps({"medium_id": 1, "term_id": 99})))
        conn.execute(
            """INSERT INTO intake_questions (id, document_id, seq, question_number, text,
                   grade, subject, section, parent_seq, depth, reviewed)
               VALUES (500, 41, 1, '1', 'Explain:', 'G12', 'x', 'Part I', NULL, 0, 1),
                      (501, 41, 2, '1(a)', 'the part', 'G12', 'x', 'Part I', 1, 1, 0)""")
        conn.execute("INSERT INTO intake_question_comments (id, question_id, author, comment) "
                     "VALUES (7, 501, ?, 'check this')", (TEACHER,))
        conn.execute("INSERT INTO intake_question_images (id, question_id, slot, image_blob) "
                     "VALUES (9, 500, 'question', ?)", (b"png",))
        report = store.migrate_old_intake_tables(conn)

    assert (report["documents"], report["questions"], report["comments"], report["images"],
            report["operators"]) == (1, 2, 1, 1, 1)
    assert (41, "subject", "Combined Mathematics") in report["unmatched"]
    assert (41, "term", 99) in report["unmatched"]
    assert report["dropped_settings"] == [(41, ["foo"])]

    with store.connect() as conn:
        doc = store.get_intake_document(conn, 41)
        qs = store.intake_questions(conn, 41)
        assert store.intake_file_blob(conn, 41) == b"%PDF-1.4"
        assert store.question_comments_for_document(conn, 41)[501][0]["comment"] == "check this"
        assert store.get_question_image(conn, 9)["image_blob"] == b"png"
        # New papers and questions are numbered after the copied ones.
        new_id = _new_paper(conn)
        store.add_extracted_questions(conn, new_id, [{"seq": 1, "text": "New."}])
        new_q = store.intake_questions(conn, new_id)[0]["id"]
        again = store.migrate_old_intake_tables(conn)

    assert (doc["paper_grade"], doc["paper_subject"], doc["status"]) == ("G12", None,
                                                                         "teacher_review")
    assert (doc["operator"], doc["assigned_to"]) == (OP1, TEACHER)
    assert doc["edsider_labels"]["medium_id"] == 1 and doc["edsider_labels"]["term_id"] is None
    assert doc["assessment_settings"]["duration_value"] == 90
    assert doc["assessment_settings"]["marking"]["parts"]["Part I"]["marks_each"] == 1
    assert doc["instruction_groups"][0]["text"] == "Read."
    assert [(q["id"], q["depth"], q["reviewed"]) for q in qs] == [(500, 0, True),
                                                                   (501, 1, False)]
    assert new_id == 42 and new_q == 502
    assert again["documents"] == 0
