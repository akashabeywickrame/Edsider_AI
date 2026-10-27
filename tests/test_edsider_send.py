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
    next id. `fail_on` names a table whose insert raises."""

    def __init__(self, fail_on=None, titles=()):
        self.rows, self.next_id, self.fail_on = [], 1000, fail_on
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
        elif sql.startswith("INSERT INTO"):
            table = sql.split()[2]
            if table == self.fail_on:
                raise RuntimeError(f"{table} refused the row")
            columns = sql[sql.index("(") + 1:sql.index(")")].split(", ")
            self.rows.append((table, dict(zip(columns, params))))
            if "RETURNING" in sql:
                self.next_id += 1
                self._result = (self.next_id,)

    def fetchone(self):
        return self._result

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
                     "assessment.assessment_operation", "assessment.assessment_review_new"]
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
    assert set(result["questions"]) == {1, 2, 3, 4, 7, 8}
    assert not any(k in r for r in questions for k in ("key", "sub_question_keys", "passage_key"))

    # Both teachers are recorded as having reviewed it.
    assert [r["teacher_id"] for r in edsider.table("assessment.assessment_review_new")] == [256, 288]
    assert not edsider.committed            # the caller commits


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
