"""Reopening a sent or archived paper (service.reopen_document): taken back
out of EdSider while EdSider has not started on it (src/edsider/recall.py,
replaced here - the real database is not written to by the tests), its
questions back in intake as they were, with data entry, ready to be sent
again. Refused - nothing changed on either side - once EdSider has."""

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import store
import service
from service import StaffIn, add_staff_member
from test_edsider_send import OPERATOR, FakeEdsider, checked_paper  # noqa: F401 (fixture)

SUPER = {"identity": "boss@example.com", "name": "Boss", "role": "superadmin"}
WHO = "x-ms-client-principal-name"


class Recall:
    """Stands in for recall.recall(): records what it was asked to take
    back, or refuses as EdSider would once it has started on the paper."""

    def __init__(self, refuse=None):
        self.calls, self.refuse = [], refuse

    def __call__(self, pg, assessment_id, question_ids):
        self.calls.append((assessment_id, sorted(question_ids)))
        if self.refuse:
            raise service.edsider_recall.RecallRefused(self.refuse)
        return {"found": True, "questions": len(self.calls[-1][1])}


def _send(doc_id, monkeypatch):
    sent = FakeEdsider()
    monkeypatch.setattr(service.edsider_send, "connect", lambda: sent)
    return service.send_to_edsider(doc_id, service.SendIn(), user=OPERATOR)


@pytest.fixture
def sent_paper(checked_paper, monkeypatch):
    """checked_paper with what the bank does not keep set on it - a
    question type, a compulsory question, a picture in file storage - then
    sent to EdSider (#1001) and archived."""
    doc_id, (q1, q2), _ = checked_paper
    with store.connect() as conn:
        conn.execute("UPDATE intake.question SET question_type = 'essay', compulsory = TRUE "
                     "WHERE id = ?", (q1,))
        image = conn.execute(
            "INSERT INTO intake.question_image (question_id, slot, image_path, content_type) "
            "VALUES (?, 'question', 'intake/pic.png', 'image/png') RETURNING id",
            (q2,)).fetchone()["id"]
    out = _send(doc_id, monkeypatch)
    assert out.status == "published" and out.archived_at and out.edsider_assessment_id == 1001
    return doc_id, (q1, q2), image


def _reopen(doc_id, monkeypatch, recall):
    edsider = FakeEdsider()
    monkeypatch.setattr(service.edsider_send, "connect", lambda: edsider)
    monkeypatch.setattr(service.edsider_recall, "recall", recall)
    return edsider, service.reopen_document(doc_id, service.ReopenIn(reason="Q2's marks"),
                                            user=OPERATOR)


def test_a_sent_paper_comes_back_to_data_entry_as_it_was(sent_paper, monkeypatch):
    doc_id, (q1, q2), image = sent_paper
    recall = Recall()
    edsider, out = _reopen(doc_id, monkeypatch, recall)

    # Taken out of EdSider - its paper and every question the send wrote.
    assert recall.calls and recall.calls[0][0] == 1001
    assert len(recall.calls[0][1]) == 7       # the stand-in package's questions
    assert edsider.committed and edsider.closed
    # Back with data entry, editable, no longer sent.
    assert out.status == "in_review" and out.archived_at is None
    assert out.edsider_assessment_id is None
    with store.connect() as conn:
        qs = {q["id"]: q for q in store.intake_questions(conn, doc_id)}
        pictures = conn.execute("SELECT id, question_id FROM intake.question_image").fetchall()
        history = [r["action"] for r in store.document_history(conn, doc_id)]
        assert store.last_send(conn, doc_id) is None
        recalled = conn.execute("SELECT recalled_at, recalled_by FROM intake.edsider_send "
                                "WHERE ok").fetchone()
    # The same questions, with what the bank does not keep.
    assert set(qs) == {q1, q2}
    assert qs[q1]["question_type"] == "essay" and qs[q1]["compulsory"] is True
    assert [(p["id"], p["question_id"]) for p in pictures] == [(image, q2)]
    assert recalled["recalled_at"] and recalled["recalled_by"] == OPERATOR["identity"]
    assert "taken back from EdSider (#1001)" in history and "reopened - Q2's marks" in history


def test_a_reopened_paper_is_checked_again_and_sent_again(sent_paper, monkeypatch):
    doc_id, _, _ = sent_paper
    _reopen(doc_id, monkeypatch, Recall())
    with pytest.raises(HTTPException) as err:          # the duplicate check first
        _send(doc_id, monkeypatch)
    assert err.value.status_code == 409 and "duplicate check" in err.value.detail
    with store.connect() as conn:
        store.update_intake_document(conn, doc_id, status="checked")
    out = _send(doc_id, monkeypatch)
    assert out.status == "published" and out.edsider_assessment_id == 1001


def test_edsider_having_started_on_it_leaves_both_sides_as_they_were(sent_paper, monkeypatch):
    doc_id, _, _ = sent_paper
    why = "EdSider has started on paper #1001: assessors have been assigned to it."
    edsider = FakeEdsider()
    monkeypatch.setattr(service.edsider_send, "connect", lambda: edsider)
    monkeypatch.setattr(service.edsider_recall, "recall", Recall(refuse=why))
    with pytest.raises(HTTPException) as err:
        service.reopen_document(doc_id, service.ReopenIn(), user=OPERATOR)
    assert err.value.status_code == 409 and err.value.detail == why
    assert edsider.rolled_back and not edsider.committed and edsider.closed
    with store.connect() as conn:
        doc = store.get_intake_document(conn, doc_id)
        assert store.last_send(conn, doc_id)["assessment_id"] == 1001
    assert doc["status"] == "published" and doc["archived_at"]


def test_a_paper_archived_before_its_rows_were_kept_comes_back_from_the_bank(
        sent_paper, monkeypatch):
    """Papers sent before reopening existed (dev's #146 and #147): their
    questions are rebuilt from the question bank, with their own ids."""
    doc_id, (q1, q2), image = sent_paper
    with store.connect() as conn:
        conn.execute("DELETE FROM intake.archived_question WHERE document_id = ?", (doc_id,))
        before = store.intake_questions(conn, doc_id)     # read from the bank
    _, out = _reopen(doc_id, monkeypatch, Recall())
    assert out.status == "in_review"
    with store.connect() as conn:
        after = store.intake_questions(conn, doc_id)
        pictures = conn.execute("SELECT id, question_id FROM intake.question_image").fetchall()
    keep = ("id", "seq", "parent_seq", "question_number", "text", "marks", "section")
    assert [{k: q[k] for k in keep} for q in after] == [{k: q[k] for k in keep} for q in before]
    assert {q["id"] for q in after} == {q1, q2} and all(q["reviewed"] for q in after)
    assert [(p["id"], p["question_id"]) for p in pictures] == [(image, q2)]


def test_an_archived_paper_never_sent_reopens_without_edsider(checked_paper, monkeypatch):
    doc_id, _, _ = checked_paper
    with store.connect() as conn:
        store.archive_document(conn, doc_id, ready_statuses=("checked",))
    recall = Recall()
    monkeypatch.setattr(service.edsider_recall, "recall", recall)
    out = service.reopen_document(doc_id, service.ReopenIn(), user=OPERATOR)
    assert out.status == "in_review" and not recall.calls


def test_only_a_sent_or_archived_paper_is_reopened(checked_paper):
    doc_id, _, _ = checked_paper
    with pytest.raises(HTTPException) as err:
        service.reopen_document(doc_id, service.ReopenIn(), user=OPERATOR)
    assert err.value.status_code == 409 and "can be changed as it is" in err.value.detail


def test_a_paper_is_taken_back_only_from_the_database_it_was_sent_to(sent_paper, monkeypatch):
    doc_id, _, _ = sent_paper
    monkeypatch.setenv("EDSIDER_SEND_DB", "examonline_db_n22_ai")
    recall = Recall()
    monkeypatch.setattr(service.edsider_recall, "recall", recall)
    with pytest.raises(HTTPException) as err:
        service.reopen_document(doc_id, service.ReopenIn(), user=OPERATOR)
    assert err.value.status_code == 409 and "examonliine_db_prod_copy" in err.value.detail
    assert not recall.calls


def test_any_data_entry_operator_reopens_it_but_a_teacher_cannot(sent_paper, monkeypatch):
    """Through the API: the paper rule (paper_access) lets the reopen call
    through on a sent paper, and only that call."""
    doc_id, (q1, _), _ = sent_paper
    for var in ("EDSIDER_REQUIRE_SIGNIN", "EDSIDER_BOOTSTRAP_ADMINS", "EDSIDER_API_KEY",
                "WEBSITE_SITE_NAME"):
        monkeypatch.delenv(var, raising=False)
    for email, role in (("op2@example.com", "employee"), ("tea@example.com", "teacher")):
        add_staff_member(StaffIn(email=email, name=email.split("@")[0], role=role), user=SUPER)
    client = TestClient(service.app)
    path = f"/intake/documents/{doc_id}"

    def as_(who, method, url, **kw):
        return client.request(method, url, headers={WHO: who}, **kw)

    assert as_("op2@example.com", "PATCH", path, json={"paper_title": "GIT Gr12"}).status_code == 409
    assert as_("tea@example.com", "POST", f"{path}/reopen", json={}).status_code == 403
    monkeypatch.setattr(service.edsider_send, "connect", lambda: FakeEdsider())
    monkeypatch.setattr(service.edsider_recall, "recall", Recall())
    # Not the operator who sent it - any Data Entry Operator may.
    r = as_("op2@example.com", "POST", f"{path}/reopen", json={"reason": "typo in Q1"})
    assert r.status_code == 200 and r.json()["status"] == "in_review"
    assert as_("op2@example.com", "PATCH", path, json={"paper_title": "GIT Gr12"}).status_code == 200
