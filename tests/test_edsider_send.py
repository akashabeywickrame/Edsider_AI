"""Sending a finished paper to EdSider (src/edsider/send.py, and the
send-to-edsider route). Against a stand-in for EdSider's database that
records every row and hands back ids, as its sequences would - the real
one is not written to by the tests."""

import datetime
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import store
from src.edsider import export, send
import service
from test_edsider_export import DOC, LISTS, PEOPLE, QUESTIONS

TWO_TEACHERS = dict(PEOPLE, teachers=[
    {"assigned_to": "t1@gmail.com", "to_staff_id": 256, "by_staff_id": 240},
    {"assigned_to": "t2@gmail.com", "to_staff_id": 288, "by_staff_id": 240}])


class FakeEdsider:
    """Records each INSERT as (table, {column: value}); RETURNING gets the
    next id. `fail_on` names a table whose insert raises. `renamed` is
    whether the reviewers' table has EdSider's current name,
    assessment_review, or the one older copies keep, assessment_review_new."""

    def __init__(self, fail_on=None, titles=(), renamed=True):
        self.rows, self.next_id, self.fail_on = [], 1000, fail_on
        self.inserts, self._all = 0, []
        self.renamed = renamed
        self.titles = {t.lower(): i for i, t in enumerate(titles, 1)}
        self.committed = self.rolled_back = self.closed = False

    def cursor(self):
        return self

    def execute(self, sql, params=()):
        self._result = None
        if sql.startswith("SELECT now()"):
            self._result = (datetime.datetime(2026, 10, 27, 9, 0),)
        elif "lower(title)" in sql:
            found = self.titles.get(params[0].lower())
            self._result = (found,) if found else None
        elif "assessment_term_condition" in sql:
            self._result = ("<p>Terms</p>",)
        elif "to_regclass('assessment.assessment_review')" in sql:
            self._result = (self.renamed,)
        elif sql.startswith("INSERT INTO"):
            table = sql.split()[2]
            if table == self.fail_on:
                raise RuntimeError(f"{table} refused the row")
            columns = sql[sql.index("(") + 1:sql.index(")")].split(", ")
            # One row, or many in one statement - each recorded, each its id.
            got = []
            for start in range(0, len(params), len(columns)):
                self.rows.append((table, dict(zip(columns, params[start:start + len(columns)]))))
                if "RETURNING" in sql:
                    self.next_id += 1
                    got.append((self.next_id,))
            self.inserts += 1
            if "RETURNING" in sql:
                self._result = got[0] if len(got) == 1 else None
                self._all = got

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._all

    def close(self):
        self.closed = True

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def table(self, name):
        return [row for table, row in self.rows if table == name]


def _package(people=TWO_TEACHERS):
    return export.build_package(DOC, QUESTIONS, LISTS, people, arrival_state="ReadyToPublish",
                                image_src=lambda img: "data:image/png;base64,AAAA")


def test_a_paper_is_written_in_edsiders_order_with_edsiders_ids():
    edsider = FakeEdsider()
    result = send.write_package(edsider, _package())

    order = list(dict.fromkeys(table for table, _ in edsider.rows))
    assert order == ["assessment.assessment", "assessment.assessment_schedule",
                     "assessment.assessment_assessor", "assessment.assessment_part",
                     "assessment.assessment_part_section", "question.passage",
                     "question.question", "assessment.assessment_question",
                     "assessment.assessment_operation", "assessment.assessment_review"]
    paper, = edsider.table("assessment.assessment")
    assert (paper["assessment_state"], paper["status_id"]) == ("ReadyToPublish", 11)
    assert paper["terms_content"] == "<p>Terms</p>" and paper["created_by"] == "314"
    aid = result["assessment_id"]
    assert all(r["assessment_id"] == aid for r in edsider.table("assessment.assessment_question"))
    assert edsider.table("question.passage")[0]["title"] == f"ASMNT{aid} Q1 - Q2"

    # A question's parts are written first, and the question names their ids.
    questions = edsider.table("question.question")
    parent = next(r for r in questions if r["qtype"] == 5 and r.get("sub_questions"))
    kids = [int(i) for i in parent["sub_questions"].split(",")]
    assert all(k in result["questions"].values() for k in kids)
    # Part (1)'s own (a) and (b) go in its place - EdSider has one level.
    assert set(result["questions"]) == {1, 2, 3, 5, 6, 7, 8}
    assert not any(k in r for r in questions for k in ("key", "sub_question_keys", "passage_key"))

    # Both teachers are recorded as having reviewed it.
    assert [r["teacher_id"] for r in edsider.table("assessment.assessment_review")] == [256, 288]
    assert not edsider.committed            # the caller commits
    # Questions and links go in a few statements, not one per row.
    assert edsider.inserts < len(edsider.rows) - len(questions) // 2


def test_reviewers_go_to_the_old_table_on_a_database_copied_before_the_rename():
    """EdSider renamed assessment_review_new to assessment_review; its
    pre-prod copy kept the old name, and its publish route reads the new
    one - so each database is written under the name it has."""
    edsider = FakeEdsider(renamed=False)
    send.write_package(edsider, _package())
    assert edsider.table("assessment.assessment_review") == []
    assert [r["teacher_id"] for r in edsider.table("assessment.assessment_review_new")] == [256, 288]


def test_a_title_edsider_already_has_is_refused_before_writing():
    """EdSider's screens keep titles unique, its database does not (found
    sending the same paper to the pre-prod copy twice)."""
    title = _package()["assessment"]["title"]
    edsider = FakeEdsider(titles=[title.upper()])
    with pytest.raises(send.TitleTaken, match="already has a paper titled"):
        send.write_package(edsider, _package())
    assert edsider.rows == []


def test_a_paper_with_problems_is_not_written():
    edsider = FakeEdsider()
    with pytest.raises(ValueError, match="cannot be sent"):
        send.write_package(edsider, _package(dict(PEOPLE, sender={})))
    assert edsider.rows == []


def test_sending_is_off_until_a_database_is_named(monkeypatch):
    monkeypatch.delenv("EDSIDER_SEND_DB", raising=False)
    assert send.target() is None
    with pytest.raises(send.SendNotSetUp):
        send.connect()
    with pytest.raises(HTTPException) as err:
        service.send_to_edsider(1, service.SendIn())
    assert err.value.status_code == 409


# ---- the route: sent, recorded and archived - or nothing at all ----

@pytest.fixture
def ready_paper(local_db, monkeypatch):
    monkeypatch.setenv("EDSIDER_SEND_DB", "examonliine_db_prod_copy")
    with store.connect() as conn:
        doc_id = store.create_intake_document(conn, "GIT.pdf")
        store.add_intake_question(conn, doc_id, 1, "Explain what an operating system does.",
                                  question_number="1", marks=9)
        store.update_intake_document(conn, doc_id, status="ready_to_publish")
    # The converter's own rows for a finished paper - this test is about
    # the sending, not the converting (test_edsider_export.py).
    package = _package()          # built before build_package is replaced
    monkeypatch.setattr(service.edsider_export, "build_package", lambda *a, **kw: package)
    return doc_id


def test_a_ready_paper_is_sent_recorded_and_archived(ready_paper, monkeypatch):
    edsider = FakeEdsider()
    monkeypatch.setattr(service.edsider_send, "connect", lambda: edsider)
    out = service.send_to_edsider(ready_paper, service.SendIn())
    assert edsider.committed and edsider.closed
    assert out.status == "published" and out.archived_at
    assert out.edsider_assessment_id == 1001     # EdSider's id for the paper
    with store.connect() as conn:
        sent = store.last_send(conn, ready_paper)
    assert (sent["target"], sent["arrival_state"], sent["ok"]) == (
        "examonliine_db_prod_copy", "ReadyToPublish", True)

    with pytest.raises(HTTPException) as err:     # never twice
        service.send_to_edsider(ready_paper, service.SendIn())
    assert err.value.status_code == 409


def test_only_a_main_assessment_administrator_sends(ready_paper, monkeypatch):
    """Asked for: the Send to EdSider button is MASAD's alone - an admin a
    superadmin made here is not one."""
    with store.connect() as conn:
        store.sync_edsider_teachers(conn, [
            {"email": "masad@gmail.com", "name": "Main", "roles": ["MASAD"], "user_id": 314,
             "staff_id": 278},
            {"email": "t@gmail.com", "name": "Teacher", "roles": ["TEACH"], "user_id": 5,
             "staff_id": 4}])
    plain_admin = {"identity": "t@gmail.com", "name": "Teacher", "role": "admin"}
    with pytest.raises(HTTPException) as err:
        service.send_to_edsider(ready_paper, service.SendIn(), user=plain_admin)
    assert err.value.status_code == 403

    masad = {"identity": "masad@gmail.com", "name": "Main", "role": "admin"}
    assert service.me(masad).masad and not service.me(plain_admin).masad
    edsider = FakeEdsider()
    monkeypatch.setattr(service.edsider_send, "connect", lambda: edsider)
    out = service.send_to_edsider(ready_paper, service.SendIn(), user=masad)
    assert out.status == "published" and edsider.committed


def test_a_send_edsider_refuses_leaves_the_paper_as_it_was(ready_paper, monkeypatch):
    edsider = FakeEdsider(fail_on="assessment.assessment_question")
    monkeypatch.setattr(service.edsider_send, "connect", lambda: edsider)
    with pytest.raises(HTTPException) as err:
        service.send_to_edsider(ready_paper, service.SendIn())
    assert err.value.status_code == 502
    assert edsider.rolled_back and not edsider.committed
    with store.connect() as conn:
        doc = store.get_intake_document(conn, ready_paper)
        attempts = conn.execute("SELECT ok, error FROM intake.edsider_send WHERE document_id = ?",
                                (ready_paper,)).fetchall()
    assert doc["status"] == "ready_to_publish" and not doc["archived_at"]
    assert [a["ok"] for a in attempts] == [False] and "refused the row" in attempts[0]["error"]


def test_only_a_paper_ready_to_publish_is_sent(ready_paper):
    with store.connect() as conn:
        store.update_intake_document(conn, ready_paper, status="teacher_review")
    with pytest.raises(HTTPException) as err:
        service.send_to_edsider(ready_paper, service.SendIn())
    assert err.value.status_code == 409


# ---- EDSIDER_FLOW=data_entry_only: data entry sends it, no review here ----

OPERATOR = {"identity": "op@gmail.com", "name": "Operator", "role": "employee"}


@pytest.fixture
def checked_paper(local_db, monkeypatch):
    """Two questions, the duplicate check run on them ('checked'), on a
    server where data entry sends papers itself. build_package is replaced
    as in ready_paper, keeping what it was asked for in `asked`."""
    monkeypatch.setenv("EDSIDER_SEND_DB", "examonliine_db_prod_copy")
    monkeypatch.setenv("EDSIDER_FLOW", "data_entry_only")
    with store.connect() as conn:
        doc_id = store.create_intake_document(conn, "GIT.pdf")
        q1 = store.add_intake_question(conn, doc_id, 1, "Explain what an operating system does.",
                                       question_number="1", marks=9)
        q2 = store.add_intake_question(conn, doc_id, 2, "Explain what an operating system does.",
                                       question_number="2", marks=9)
        store.update_intake_document(conn, doc_id, status="checked")
    package, asked = _package(), {}
    monkeypatch.setattr(service.edsider_export, "build_package",
                        lambda *a, **kw: asked.update(kw) or package)
    return doc_id, (q1, q2), asked


def test_data_entry_sends_a_checked_paper_itself_arriving_ready(checked_paper, monkeypatch):
    doc_id, _, asked = checked_paper
    edsider = FakeEdsider()
    monkeypatch.setattr(service.edsider_send, "connect", lambda: edsider)
    out = service.send_to_edsider(doc_id, service.SendIn(), user=OPERATOR)
    assert edsider.committed and asked["arrival_state"] == "Ready"
    assert out.status == "published" and out.archived_at and out.edsider_assessment_id == 1001
    with store.connect() as conn:
        assert store.last_send(conn, doc_id)["arrival_state"] == "Ready"
        # The operator who sent it is the paper's data entry, finished.
        entry = store.document_people(conn, doc_id)["data_entry"]
    assert entry["assigned_to"] == OPERATOR["identity"] and entry["finished_at"]


def test_data_entry_sends_only_once_checked_and_every_repeat_settled(checked_paper):
    doc_id, (q1, q2), _ = checked_paper
    with store.connect() as conn:
        store.update_intake_document(conn, doc_id, status="in_review")
    with pytest.raises(HTTPException) as err:
        service.send_to_edsider(doc_id, service.SendIn(), user=OPERATOR)
    assert err.value.status_code == 409 and "duplicate check" in err.value.detail

    with store.connect() as conn:
        store.update_intake_document(conn, doc_id, status="checked")
        store.flag_repeat(conn, q2, q1, flagged_by=OPERATOR["identity"])
    with pytest.raises(HTTPException) as err:
        service.send_to_edsider(doc_id, service.SendIn(), user=OPERATOR)
    assert err.value.status_code == 409 and "repeat flag" in err.value.detail

    # The checklist shows the open flag on its own question.
    preview = service.edsider_preview(doc_id, user=OPERATOR)
    flagged = [p for p in preview.problems if p["question_id"] == q2]
    assert flagged and flagged[0]["level"] == "error"


def test_data_entry_settles_its_own_repeat_flags(checked_paper, monkeypatch):
    """No teacher here to replace or keep a repeat - data entry does."""
    doc_id, (q1, q2), _ = checked_paper
    with store.connect() as conn:
        store.flag_repeat(conn, q2, q1, flagged_by=OPERATOR["identity"])
    assert service.repeat_settler(OPERATOR) == OPERATOR
    service.keep_flagged_question(q2, service.KeepQuestionIn(note="Asks for a different unit."),
                                  identity=OPERATOR["identity"], user=OPERATOR)
    with store.connect() as conn:
        assert store.open_flags(conn, doc_id) == 0
    # With a teacher review here, settling a repeat stays the teacher's.
    monkeypatch.delenv("EDSIDER_FLOW")
    with pytest.raises(HTTPException) as err:
        service.repeat_settler(OPERATOR)
    assert err.value.status_code == 403


def test_without_the_setting_data_entry_neither_sends_nor_previews(checked_paper, monkeypatch):
    doc_id, _, _ = checked_paper
    monkeypatch.delenv("EDSIDER_FLOW")
    for route in (lambda: service.send_to_edsider(doc_id, service.SendIn(), user=OPERATOR),
                  lambda: service.edsider_preview(doc_id, user=OPERATOR)):
        with pytest.raises(HTTPException) as err:
            route()
        assert err.value.status_code == 403


def test_data_entry_only_turns_the_teacher_review_off(checked_paper):
    doc_id, _, _ = checked_paper
    assert service.me(OPERATOR).flow == "data_entry_only"
    assert service.health()["flow"] == "data_entry_only"
    with pytest.raises(HTTPException) as err:
        service.finish_data_entry(doc_id)
    assert err.value.status_code == 409
    with pytest.raises(HTTPException) as err:
        service.assign_document(doc_id, service.AssignIn(teacher_identities=["t@gmail.com"]))
    assert err.value.status_code == 409
