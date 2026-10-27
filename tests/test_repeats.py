"""A question repeated on its own paper: data entry flags it from the
duplicate check, a teacher replaces it with a new question (or keeps it,
saying why), and the question keeps its history - what it was before."""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import store
import service
from service import StaffIn, add_staff_member

SUPER = {"identity": "boss@example.com", "name": "Boss", "role": "superadmin"}
WHO = "x-ms-client-principal-name"
ADMIN, OP, T1, T2 = "adm@example.com", "op@example.com", "t1@example.com", "t2@example.com"
PNG = b"\x89PNG\r\n\x1a\n a diagram"


@pytest.fixture
def client(local_db, monkeypatch):
    for var in ("EDSIDER_REQUIRE_SIGNIN", "EDSIDER_BOOTSTRAP_ADMINS", "EDSIDER_API_KEY",
                "WEBSITE_SITE_NAME"):
        monkeypatch.delenv(var, raising=False)
    for email, role in ((ADMIN, "admin"), (OP, "employee"), (T1, "teacher"), (T2, "teacher")):
        add_staff_member(StaffIn(email=email, name=email.split("@")[0], role=role), user=SUPER)
    return TestClient(service.app)


def call(client, who, method, path, **kw):
    return client.request(method, path, headers={WHO: who}, **kw)


@pytest.fixture
def paper(local_db):
    """A paper whose question 3 repeats question 1, with the operator's
    duplicate check run on it ('checked')."""
    with store.connect() as conn:
        doc_id = store.create_intake_document(conn, "GIT.pdf")
        ids = [store.add_intake_question(conn, doc_id, seq, text, question_number=str(seq), marks=2,
                                         answer=answer)
               for seq, text, answer in (
                   (1, "Define the term system.", "An organised whole."),
                   (2, "Name two input devices.", "Mouse, keyboard."),
                   (3, "Define the term system.", "An organised whole."))]
        store.add_question_image(conn, ids[2], "question", PNG, "image/png")
        store.assign_review(conn, doc_id, "data_entry", OP)
        conn.execute("UPDATE intake.question SET reviewed = TRUE WHERE document_id = ?", (doc_id,))
        store.update_intake_document(conn, doc_id, status="checked")
    return doc_id, ids


def _with_teachers(client, doc_id):
    assert call(client, OP, "POST", f"/intake/documents/{doc_id}/finish-data-entry").status_code == 200
    call(client, ADMIN, "POST", f"/intake/documents/{doc_id}/assign",
         json={"teacher_identities": [T1, T2]})


def _questions(client, who, doc_id):
    return {q["id"]: q for q in call(client, who, "GET", f"/intake/documents/{doc_id}/questions").json()}


def test_a_flagged_repeat_is_replaced_and_its_old_version_kept(client, paper):
    doc_id, (q1, _q2, q3) = paper
    flagged = call(client, OP, "POST", f"/intake/questions/{q3}/flag",
                   json={"repeat_of": q1, "note": "Same as question 1, word for word."}).json()
    assert flagged["repeat"]["status"] == "open" and flagged["repeat"]["repeat_of_number"] == "1"
    _with_teachers(client, doc_id)

    # The teacher sees it, and cannot finish while it is open.
    seen = _questions(client, T1, doc_id)[q3]
    assert seen["repeat"]["repeat_of"] == q1 and seen["repeat"]["flagged_by"] == OP
    refused = call(client, T1, "POST", f"/intake/documents/{doc_id}/finish",
                   json={"status": "review_done"})
    assert refused.status_code == 409 and "repeats" in refused.json()["detail"]

    # The teacher writes a new question in its place.
    new = call(client, T1, "POST", f"/intake/questions/{q3}/replace", json={
        "text": "Explain the term feedback in a system.", "answer": "Output returned as input.",
        "marks": 3, "reason": "Repeated question 1 on this paper.", "keep_pictures": False}).json()
    assert new["text"] == "Explain the term feedback in a system." and new["marks"] == 3
    assert new["repeat"]["status"] == "replaced" and new["repeat"]["resolved_by"] == T1
    assert new["revisions"] == 1

    # Its history: the earlier question, who replaced it, when and why, and
    # the picture that went with it - kept, not deleted.
    history = call(client, T2, "GET", f"/intake/questions/{q3}/history").json()
    assert history["current"]["text"] == "Explain the term feedback in a system."
    earlier, = history["earlier"]
    assert (earlier["text"], earlier["answer"], earlier["marks"]) == (
        "Define the term system.", "An organised whole.", 2)
    assert earlier["replaced_by"] == T1 and earlier["reason"] == "Repeated question 1 on this paper."
    assert [p["slot"] for p in earlier["pictures"]] == ["question"]
    with store.connect() as conn:
        shown = store.question_images_for_document(conn, doc_id).get(q3, [])
    # The earlier picture is kept but no longer shown on the question - and
    # shown once: the bank's copy (made when data entry finished) is not
    # listed beside the paper's own.
    assert [i["slot"].split(":")[0] for i in shown] == ["earlier"]

    # The other teacher sees what was done; both can now finish.
    assert _questions(client, T2, doc_id)[q3]["repeat"]["status"] == "replaced"
    assert call(client, T1, "POST", f"/intake/documents/{doc_id}/finish",
                json={"status": "review_done"}).status_code == 200

    log = [h["action"] for h in call(client, ADMIN, "GET", f"/intake/documents/{doc_id}/history").json()]
    assert "Q3 flagged as a repeat of Q1" in log and "Q3 replaced with a new question" in log


def test_a_teacher_may_keep_a_flagged_question_saying_why(client, paper):
    doc_id, (q1, _q2, q3) = paper
    call(client, OP, "POST", f"/intake/questions/{q3}/flag", json={"repeat_of": q1})
    _with_teachers(client, doc_id)
    assert call(client, T2, "POST", f"/intake/questions/{q3}/keep", json={"note": ""}).status_code == 422
    kept = call(client, T2, "POST", f"/intake/questions/{q3}/keep",
                json={"note": "Asked again on purpose, in Part II."}).json()
    assert kept["repeat"]["status"] == "kept" and kept["text"] == "Define the term system."
    assert kept["revisions"] == 0


def test_only_data_entry_flags_and_only_a_teacher_replaces(client, paper):
    doc_id, (q1, q2, q3) = paper
    assert call(client, T1, "POST", f"/intake/questions/{q3}/flag",
                json={"repeat_of": q1}).status_code == 403
    assert call(client, OP, "POST", f"/intake/questions/{q3}/flag",
                json={"repeat_of": q3}).status_code == 400      # not itself
    call(client, OP, "POST", f"/intake/questions/{q3}/flag", json={"repeat_of": q1})
    assert call(client, OP, "POST", f"/intake/questions/{q3}/replace",
                json={"text": "x"}).status_code == 403
    # Taken back before a teacher acts on it.
    back = call(client, OP, "DELETE", f"/intake/questions/{q3}/flag").json()
    assert back["repeat"] is None


def test_the_history_outlives_archiving(paper):
    doc_id, (q1, _q2, q3) = paper
    with store.connect() as conn:
        store.flag_repeat(conn, q3, q1, flagged_by=OP)
        store.replace_question(conn, q3, "Explain feedback.", answer="Output fed back.",
                               replaced_by=T1, keep_pictures=False)
        store.sync_paper_to_bank(conn, doc_id)
        store.update_intake_document(conn, doc_id, status="ready_to_publish")
        store.archive_document(conn, doc_id)
        earlier = store.question_history(conn, q3)
        flags = store.question_flags(conn, doc_id)
    assert [e["text"] for e in earlier] == ["Define the term system."]
    # Its picture went with the earlier version, and is still there
    # through the paper's bank copy.
    assert [p["slot"] for p in earlier[0]["pictures"]] == ["question"]
    assert flags[q3]["status"] == "replaced"
