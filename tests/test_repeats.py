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
         json={"teacher_identities": [T1]})


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
    history = call(client, ADMIN, "GET", f"/intake/questions/{q3}/history").json()
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

    # Settled, the teacher can finish: the paper is ready to publish.
    done = call(client, T1, "POST", f"/intake/documents/{doc_id}/finish",
                json={"status": "review_done"}).json()
    assert done["status"] == "ready_to_publish"

    log = [h["action"] for h in call(client, ADMIN, "GET", f"/intake/documents/{doc_id}/history").json()]
    assert "Q3 flagged as a repeat of Q1" in log and "Q3 replaced with a new question" in log


def test_the_owner_teacher_may_keep_a_flagged_question_saying_why(client, paper):
    doc_id, (q1, _q2, q3) = paper
    call(client, OP, "POST", f"/intake/questions/{q3}/flag", json={"repeat_of": q1})
    _with_teachers(client, doc_id)
    assert call(client, T1, "POST", f"/intake/questions/{q3}/keep", json={"note": ""}).status_code == 422
    kept = call(client, T1, "POST", f"/intake/questions/{q3}/keep",
                json={"note": "Asked again on purpose, in Part II."}).json()
    assert kept["repeat"]["status"] == "kept" and kept["text"] == "Define the term system."
    assert kept["revisions"] == 0


def test_the_owner_settles_repeats_and_the_syllabus_checker_only_checks(client, paper):
    """A paper given two teachers, as papers were before one became enough:
    the owner (the first assigned) answers the repeats; the syllabus
    checker checks and comments."""
    doc_id, (q1, _q2, q3) = paper
    call(client, OP, "POST", f"/intake/questions/{q3}/flag", json={"repeat_of": q1})
    assert call(client, OP, "POST", f"/intake/documents/{doc_id}/finish-data-entry").status_code == 200
    with store.connect() as conn:
        store.assign_teachers(conn, doc_id, [T1, T2], assigned_by=ADMIN)
    doc = call(client, ADMIN, "GET", f"/intake/documents/{doc_id}").json()
    assert doc["teacher_owner"] == T1 and doc["teachers"] == [T1, T2]

    for action, body in (("replace", {"text": "Explain feedback."}), ("keep", {"note": "On purpose."})):
        refused = call(client, T2, "POST", f"/intake/questions/{q3}/{action}", json=body)
        assert refused.status_code == 403 and "owner teacher" in refused.json()["detail"]
    assert call(client, T2, "POST", f"/intake/questions/{q3}/comments",
                json={"comment": "Out of the Grade 12 syllabus."}).status_code in (200, 201)

    # The checker's sign-off does not wait on the repeat; the owner's does.
    assert call(client, T2, "POST", f"/intake/documents/{doc_id}/finish",
                json={"status": "review_done"}).status_code == 200
    assert call(client, T1, "POST", f"/intake/documents/{doc_id}/finish",
                json={"status": "review_done"}).status_code == 409
    call(client, T1, "POST", f"/intake/questions/{q3}/keep", json={"note": "Asked again on purpose."})
    done = call(client, T1, "POST", f"/intake/documents/{doc_id}/finish", json={"status": "review_done"})
    assert done.json()["status"] == "ready_to_publish"


def test_reassigning_hands_the_paper_to_the_new_teacher(client, paper):
    doc_id, _ids = paper
    _with_teachers(client, doc_id)
    call(client, ADMIN, "POST", f"/intake/documents/{doc_id}/assign",
         json={"teacher_identities": [T2]})
    doc = call(client, ADMIN, "GET", f"/intake/documents/{doc_id}").json()
    assert (doc["teacher_owner"], doc["teachers"]) == (T2, [T2])


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


def test_a_repeat_is_replaced_by_a_multiple_choice_question(client, paper):
    """Asked for: the teacher's new question can be an MCQ, given its
    options and the correct one."""
    doc_id, (q1, _q2, q3) = paper
    call(client, OP, "POST", f"/intake/questions/{q3}/flag", json={"repeat_of": q1})
    _with_teachers(client, doc_id)
    assert call(client, T1, "POST", f"/intake/questions/{q3}/replace", json={
        "text": "Which is an input device?", "kind": "mcq",
        "options": ["Monitor", "Keyboard"], "correct": []}).status_code == 400
    new = call(client, T1, "POST", f"/intake/questions/{q3}/replace", json={
        "text": "Which is an input device?", "kind": "mcq", "marks": 1,
        "options": ["Monitor", "Keyboard", "Printer", ""], "correct": [1]}).json()
    assert new["question_type"] == "mcq" and new["mcq"]["stem"] == "Which is an input device?"
    assert [o["text"] for o in new["mcq"]["options"]] == ["Monitor", "Keyboard", "Printer"]
    assert new["mcq"]["correct_labels"] == ["2"] and new["repeat"]["status"] == "replaced"


def test_a_repeat_is_replaced_by_a_question_in_parts(client, paper):
    """Its parts go straight under it, the questions after them move down,
    and its marks are the parts' marks added up."""
    doc_id, (q1, q2, q3) = paper
    call(client, OP, "POST", f"/intake/questions/{q1}/flag", json={"repeat_of": q3})
    _with_teachers(client, doc_id)
    new = call(client, T1, "POST", f"/intake/questions/{q1}/replace", json={
        "text": "A school uses a computer network.", "kind": "sub",
        "parts": [{"text": "Define a network.", "marks": 2},
                  {"text": "Give two advantages of a network.", "marks": 3.5, "answer": "Sharing"}],
    }).json()
    assert new["marks"] == 5.5
    qs = call(client, T1, "GET", f"/intake/documents/{doc_id}/questions").json()
    assert [(q["question_number"], q["parent_seq"]) for q in qs] == [
        ("1", None), ("1(a)", 1), ("1(b)", 1), ("2", None), ("3", None)]
    assert qs[2]["answer"] == "Sharing" and qs[2]["marks"] == 3.5
    assert qs[3]["id"] == q2                         # moved down, the same question


def test_a_teacher_corrects_a_spelling_and_the_earlier_wording_is_kept(client, paper):
    """Asked for: teachers fix small mistakes, not only comment - and the
    administrator following the paper sees what changed."""
    doc_id, (_q1, q2, _q3) = paper
    _with_teachers(client, doc_id)
    fixed = call(client, T1, "POST", f"/intake/questions/{q2}/correct",
                 json={"text": "Name two input devices used in a school.",
                       "answer": "Mouse, keyboard."}).json()
    assert fixed["text"] == "Name two input devices used in a school." and fixed["revisions"] == 1
    history = call(client, ADMIN, "GET", f"/intake/questions/{q2}/history").json()
    earlier, = history["earlier"]
    assert earlier["text"] == "Name two input devices." and earlier["replaced_by"] == T1
    log = [h["action"] for h in call(client, ADMIN, "GET", f"/intake/documents/{doc_id}/history").json()]
    assert "Q2 corrected by the teacher" in log
    # Nothing changed, nothing kept; another teacher cannot.
    again = call(client, T1, "POST", f"/intake/questions/{q2}/correct",
                 json={"text": "Name two input devices used in a school."}).json()
    assert again["revisions"] == 1
    assert call(client, T2, "POST", f"/intake/questions/{q2}/correct",
                json={"text": "x"}).status_code == 403


def test_an_administrator_sees_the_teachers_own_ticks(client, paper):
    doc_id, (q1, _q2, _q3) = paper
    _with_teachers(client, doc_id)
    call(client, T1, "PATCH", f"/intake/questions/{q1}", json={"teacher_reviewed": True})
    ticks = {q["id"]: q["teacher_reviewed"]
             for q in call(client, ADMIN, "GET", f"/intake/documents/{doc_id}/questions").json()}
    assert ticks[q1] is True and sum(ticks.values()) == 1
