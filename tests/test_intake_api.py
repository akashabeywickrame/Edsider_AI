"""Tests for the paper-intake / teacher / admin API routes added to
service.py - the first slice of a real HTTP API standing in front of
what app.py's "Paper intake" page, teacher_app.py and masd_app.py
currently do by importing src.store directly.

Route functions are called directly rather than through a running
server - same reasoning as test_service.py: no real model, no real
HTTP stack, just the request/response wiring and the calls into
src.store and src.intake it is supposed to make. A dependency injected
via Depends() (current_identity) is never resolved when a function is
called this way, so every test passes `identity=` explicitly instead
of relying on a header.
"""

import sys
from io import BytesIO
from pathlib import Path

import numpy as np
import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import dedupe, store
import service
from service import (
    AssignIn, CommentIn, DocumentUpdate, FinishIn, QuestionCreate,
    QuestionUpdate, ResplitRequest, add_comment, assign_document,
    attach_image, check_document_duplicates, create_question,
    finish_review, get_document, get_image, list_comments,
    list_document_images, list_documents, list_questions, list_teachers,
    my_documents, precheck_document_duplicates, preview_resplit,
    remove_image, remove_question, resplit_document, touch_teacher, edit_image,
    update_document, update_question, upload_document,
)


def _upload(filename, content):
    return UploadFile(file=BytesIO(content), filename=filename)


def _unit(v):
    v = np.array(v, dtype=float)
    return v / np.linalg.norm(v)


class FakeEmbedder:
    """Same shape as test_service.py's - a vector per exact text, so a
    test controls "meaning" directly instead of needing the real model."""

    def __init__(self, vectors):
        self.vectors = vectors

    def encode(self, texts, is_query=False):
        if isinstance(texts, str):
            texts = [texts]
        return [self.vectors[t] for t in texts]


@pytest.fixture
def docx_bytes():
    docx = pytest.importorskip("docx")
    buf = BytesIO()
    d = docx.Document()
    d.add_paragraph("Grade 9 - Mathematics")
    d.add_paragraph("1. Add 12 and 5. (2 marks)")
    d.add_paragraph("2. Subtract 5 from 12. (2 marks)")
    d.save(buf)
    return buf.getvalue()


@pytest.fixture
def document(local_db, docx_bytes):
    """One uploaded, extracted paper - the starting point most of these
    tests build on."""
    return upload_document(file=_upload("paper.docx", docx_bytes),
                           lang="eng", style="number",
                           identity="data-entry-1")


# --------------------------------------------------------------- upload

def test_upload_extracts_and_creates_two_questions(document):
    assert document.file_type == "docx"
    assert document.status == "extracted"
    assert document.uploaded_by == "data-entry-1"
    assert document.paper_grade == "G09"
    assert document.paper_subject == "Maths"

    qs = list_questions(document.id)
    assert [q.question_number for q in qs] == ["1", "2"]
    # The (2 marks) annotation is pulled into its own column, and
    # stripped from the stored text - see CLAUDE.md's "stripping
    # marks annotations" note.
    assert qs[0].marks == 2
    assert "marks" not in (qs[0].text or "").lower()


def test_upload_rejects_an_empty_file(local_db):
    with pytest.raises(HTTPException) as err:
        upload_document(file=_upload("empty.docx", b""), lang="eng",
                        style="number", identity="data-entry-1")
    assert err.value.status_code == 400


def test_list_documents_filters_by_status(document):
    ids = {d.id for d in list_documents(status=None)}
    assert document.id in ids
    assert document.id in {d.id for d in list_documents(status="extracted")}
    assert document.id not in {d.id for d in list_documents(status="checked")}


def test_get_missing_document_is_404(local_db):
    with pytest.raises(HTTPException) as err:
        get_document(999999)
    assert err.value.status_code == 404


# ----------------------------------------------------------- editing

def test_update_document_and_question(document):
    updated = update_document(document.id, DocumentUpdate(paper_title="Term 1"))
    assert updated.paper_title == "Term 1"

    qs = list_questions(document.id)
    assert qs[0].last_edited_by is None
    q = update_question(qs[0].id, QuestionUpdate(marks=5, reviewed=True), identity="data-entry-1")
    assert q.marks == 5
    assert q.reviewed is True
    assert q.teacher_reviewed is False
    assert q.last_edited_by == "data-entry-1"
    assert q.last_edited_at is not None


def test_update_missing_question_is_404(local_db):
    with pytest.raises(HTTPException) as err:
        update_question(999999, QuestionUpdate(marks=1), identity="data-entry-1")
    assert err.value.status_code == 404


def test_add_and_remove_a_question_by_hand(document):
    created = create_question(document.id,
                              QuestionCreate(text="3. A new one.", marks=1))
    assert created.text == "3. A new one."
    # Inherited from the paper, same as an extracted question's own
    # fallback (add_extracted_questions) - not left blank.
    assert created.grade == document.paper_grade
    assert created.subject == document.paper_subject

    remove_question(created.id)
    with pytest.raises(HTTPException):
        update_question(created.id, QuestionUpdate(marks=1), identity="data-entry-1")


def test_add_a_part_is_numbered_like_the_paper_would(document):
    qs = list_questions(document.id)
    part = create_question(
        document.id, QuestionCreate(text="Explain your working.",
                                    parent_seq=qs[0].seq))
    assert part.question_number == f"{qs[0].question_number}(a)"
    assert part.depth == 1
    assert part.parent_seq == qs[0].seq


def test_a_missed_question_is_placed_where_it_belongs(document):
    # The paper has questions 1 and 2. The page sends after_seq from the
    # place data entry picked: a part of question 1, then a whole question
    # between 1 (with its new part) and 2 - not at the end of the paper.
    q1, q2 = list_questions(document.id)
    create_question(document.id, QuestionCreate(
        text="Show your working.", question_number="1(a)",
        parent_seq=q1.seq, after_seq=q1.seq))
    create_question(document.id, QuestionCreate(
        text="A question the extractor missed.", question_number="1A",
        after_seq=q1.seq + 1))  # after question 1's new part
    numbers = [q.question_number for q in list_questions(document.id)]
    assert numbers == ["1", "1(a)", "1A", "2"]
    # Question 2 moved down two places and is still the same row.
    moved = [q for q in list_questions(document.id) if q.id == q2.id][0]
    assert moved.seq == q2.seq + 2


def test_a_part_keeps_its_question_when_rows_move(document):
    q1, q2 = list_questions(document.id)
    part = create_question(document.id, QuestionCreate(
        text="Explain.", question_number="2(a)", parent_seq=q2.seq, after_seq=q2.seq))
    # A whole question placed before question 2 moves 2 and its part down:
    # the part must still point at question 2.
    create_question(document.id, QuestionCreate(
        text="Before two.", question_number="1A", after_seq=q1.seq))
    after = {q.id: q for q in list_questions(document.id)}
    assert after[part.id].parent_seq == after[q2.id].seq


def test_a_question_can_go_before_the_first(document):
    create_question(document.id, QuestionCreate(text="First.", question_number="0", after_seq=0))
    assert [q.question_number for q in list_questions(document.id)][0] == "0"


def test_the_paper_finder_filters_and_pages(document, docx_bytes):
    other = upload_document(file=_upload("physics term test.docx", docx_bytes),
                            lang="eng", style="number", identity="data-entry-1")
    update_document(document.id, DocumentUpdate(paper_subject="Chemistry", paper_grade="G13",
                                                 paper_title="Model Paper"))
    update_document(other.id, DocumentUpdate(paper_subject="Physics", paper_grade="G12"))
    with service.store.connect() as conn:
        service.store.update_intake_document(conn, document.id, status="refer_back")

    def ids(**filters):
        return [d.id for d in service.search_documents(**filters).items]

    assert ids(subject="Chemistry") == [document.id]
    assert ids(grade="G12") == [other.id]
    assert ids(status="refer_back") == [document.id]
    assert ids(status="extracted", subject="Chemistry") == []
    # Search matches the file name or the title, any case.
    assert ids(q="PHYSICS") == [other.id]
    assert ids(q="model") == [document.id]
    # Newest first, a page at a time, with the total for every page.
    first = service.search_documents(limit=1, offset=0)
    second = service.search_documents(limit=1, offset=1)
    assert first.total == second.total == 2
    assert [first.items[0].id, second.items[0].id] == [other.id, document.id]
    # The filter menus' counts.
    assert first.facets["subject"] == {"Chemistry": 1, "Physics": 1}
    assert first.facets["status"]["refer_back"] == 1


def test_add_a_part_of_an_unknown_question_is_404(document):
    with pytest.raises(HTTPException) as err:
        create_question(document.id,
                        QuestionCreate(text="x", parent_seq=999999))
    assert err.value.status_code == 404


# ------------------------------------------------------------ comments

def test_comments_round_trip(document):
    qs = list_questions(document.id)
    c = add_comment(qs[0].id, CommentIn(comment="Check the wording."),
                    identity="N. Perera")
    assert c.author == "N. Perera"
    assert c.comment == "Check the wording."

    by_q = list_comments(document.id)
    assert by_q[str(qs[0].id)][0].comment == "Check the wording."


def test_comment_text_cannot_be_blank():
    with pytest.raises(Exception):
        CommentIn(comment="")


# -------------------------------------------------------------- images

def test_images_round_trip(document):
    qs = list_questions(document.id)
    picture = b"not-really-a-png-but-the-api-never-looks-inside-it"
    img = attach_image(qs[0].id, slot="question", file=_upload("q.png", picture))
    assert img.slot == "question"

    by_q = list_document_images(document.id)
    assert by_q[str(qs[0].id)][0].id == img.id

    resp = get_image(img.id)
    assert resp.body == picture

    # A label under it, only when the paper names its pictures; moved to
    # where it belongs when the extractor put it in the wrong place.
    assert edit_image(img.id, service.ImageEditIn(label="A")) == {
        "id": img.id, "slot": "question", "label": "A"}
    assert list_document_images(document.id)[str(qs[0].id)][0].label == "A"
    moved = edit_image(img.id, service.ImageEditIn(slot="answer"))
    assert moved == {"id": img.id, "slot": "answer", "label": "A"}
    assert edit_image(img.id, service.ImageEditIn(slot="option:II", label=""))["label"] is None
    assert list_document_images(document.id)[str(qs[0].id)][0].slot == "option:II"
    for bad in ("earlier:1:question", "nowhere"):
        with pytest.raises(HTTPException) as err:
            edit_image(img.id, service.ImageEditIn(slot=bad))
        assert err.value.status_code == 400

    remove_image(img.id)
    assert list_document_images(document.id) == {}


def test_attach_image_to_missing_question_is_404(local_db):
    with pytest.raises(HTTPException) as err:
        attach_image(999999, slot="question", file=_upload("q.png", b"x"))
    assert err.value.status_code == 404


def test_get_missing_image_is_404(local_db):
    with pytest.raises(HTTPException) as err:
        get_image(999999)
    assert err.value.status_code == 404


# ------------------------------------------------------------- resplit

def test_resplit_preview_does_not_change_anything(document):
    preview = preview_resplit(document.id, ResplitRequest(style="question_word"))
    assert preview.currently_stored == 2
    # Unchanged by the preview - still the original 2, not whatever
    # the "question_word"-keyed style alone would find.
    assert len(list_questions(document.id)) == 2


def test_resplit_commits_a_different_style(document):
    updated = resplit_document(document.id, ResplitRequest(style="question_word"))
    assert updated.split_style == "question_word"
    # Neither paragraph contains the word "Question", so this style
    # finds nothing to split on and the whole text lands as one draft -
    # the exact ambiguity SPLIT_STYLES exists for (CLAUDE.md).
    assert len(list_questions(document.id)) == 1


# -------------------------------------------------- duplicate check

def test_check_duplicates_requires_every_question_reviewed(document):
    with pytest.raises(HTTPException) as err:
        check_document_duplicates(document.id)
    assert err.value.status_code == 409


def test_check_duplicates_needs_the_model_loaded(document):
    for q in list_questions(document.id):
        update_question(q.id, QuestionUpdate(reviewed=True), identity="data-entry-1")
    service.state["embedder"] = None
    with pytest.raises(HTTPException) as err:
        check_document_duplicates(document.id)
    assert err.value.status_code == 503


def test_check_duplicates_marks_the_document_checked(document):
    qs = list_questions(document.id)
    for q in qs:
        update_question(q.id, QuestionUpdate(reviewed=True), identity="data-entry-1")

    service.state["embedder"] = FakeEmbedder({
        q.text: _unit([1.0, i, 0.0]) for i, q in enumerate(list_questions(document.id))
    })
    try:
        out = check_document_duplicates(document.id)
    finally:
        service.state["embedder"] = None

    assert out["exact_groups"] == []
    doc = get_document(document.id)
    assert doc.status == "checked"
    assert doc.id in {d.id for d in list_documents(status="checked")}


def test_a_paper_both_teachers_finished_is_not_checked_or_flagged_again(document):
    """Asked for: once a paper is ready to publish its review is over, so a
    duplicate check or a repeat flag has nothing left to change."""
    q1, q2 = [q.id for q in list_questions(document.id)][:2]
    for status in ("ready_to_publish", "published"):
        with store.connect() as conn:
            store.set_document_status(conn, document.id, status)
        for attempt in (lambda: check_document_duplicates(document.id),
                        lambda: precheck_document_duplicates(document.id),
                        lambda: service.flag_repeated_question(
                            q2, service.FlagRepeatIn(repeat_of=q1), identity="op"),
                        lambda: service.unflag_repeated_question(q2, identity="op")):
            with pytest.raises(HTTPException) as err:
                attempt()
            assert err.value.status_code == 409 and "finished reviewing" in err.value.detail


def test_check_duplicates_leaves_a_paper_with_its_teachers(document):
    """Paper #90: an admin ran the check from data entry while two teachers
    had the paper; it went back to 'checked', and neither teacher could tick
    a question any more."""
    for q in list_questions(document.id):
        update_question(q.id, QuestionUpdate(reviewed=True), identity="data-entry-1")
    with store.connect() as conn:
        store.set_document_status(conn, document.id, "teacher_review")
    service.state["embedder"] = FakeEmbedder({
        q.text: _unit([1.0, i, 0.0]) for i, q in enumerate(list_questions(document.id))
    })
    try:
        check_document_duplicates(document.id)
    finally:
        service.state["embedder"] = None
    assert get_document(document.id).status == "teacher_review"


# --------------------------------------------- early (pre-review) check

def test_precheck_needs_the_model_loaded(document):
    service.state["embedder"] = None
    service.state["index"] = None
    with pytest.raises(HTTPException) as err:
        precheck_document_duplicates(document.id)
    assert err.value.status_code == 503


def test_precheck_does_not_require_review_or_change_status(document):
    """The whole point of the early check is to run BEFORE the ~40
    minute to an hour review pass - requiring it first, or marking the
    document along the way, would defeat that."""
    qs = list_questions(document.id)
    assert all(not q.reviewed for q in qs)  # nothing reviewed yet

    service.state["embedder"] = FakeEmbedder({
        qs[0].text: _unit([1.0, 0.0, 0.0]),
        qs[1].text: _unit([0.0, 1.0, 0.0]),
    })
    service.state["index"] = dedupe.VectorIndex([])
    try:
        out = precheck_document_duplicates(document.id)
    finally:
        service.state["embedder"] = None
        service.state["index"] = None

    assert out["questions_checked"] == 2
    assert out["against_bank"] == []
    doc = get_document(document.id)
    assert doc.status == "extracted"  # unchanged - informational only


def test_precheck_finds_a_match_against_the_whole_bank(document):
    qs = list_questions(document.id)
    service.state["embedder"] = FakeEmbedder({
        qs[0].text: _unit([1.0, 0.0, 0.0]),
        qs[1].text: _unit([0.0, 1.0, 0.0]),
    })
    service.state["index"] = dedupe.VectorIndex([
        {"id": "BANK1", "text": "An existing bank question", "grade": "G09",
         "subject": "Maths", "vector": list(_unit([1.0, 0.0, 0.01]))},
    ])
    try:
        out = precheck_document_duplicates(document.id)
    finally:
        service.state["embedder"] = None
        service.state["index"] = None

    assert len(out["against_bank"]) == 1
    hit = out["against_bank"][0]
    assert hit["question_id"] == qs[0].id
    assert hit["matches"][0]["id"] == "BANK1"
    doc = get_document(document.id)
    assert doc.status == "extracted"


# ------------------------------------------- assign, teacher, promote

TWO = AssignIn(teacher_identities=["N. Perera", "s. kumar"])


def test_assign_requires_two_teachers_who_have_opened_the_app(document):
    touch_teacher(identity="S. Kumar")
    with pytest.raises(HTTPException) as err:
        assign_document(document.id, TWO)
    assert err.value.status_code == 404

    teacher = touch_teacher(identity="N. Perera")
    assert teacher.identity == "N. Perera"
    assert any(t.identity == "N. Perera" for t in list_teachers())
    with pytest.raises(HTTPException) as err:
        assign_document(document.id, AssignIn(teacher_identities=["N. Perera", "n. perera"]))
    assert err.value.status_code == 400

    assigned = assign_document(document.id, TWO)
    assert assigned.status == "teacher_review"
    assert assigned.teachers == ["N. Perera", "S. Kumar"]
    assert assigned.assigned_to == "N. Perera, S. Kumar"
    assert document.id in {d.id for d in my_documents(identity="N. Perera")}


def test_my_documents_only_shows_the_calling_identity(document):
    for name in ("N. Perera", "S. Kumar", "A. Silva"):
        touch_teacher(identity=name)
    assign_document(document.id, TWO)
    assert document.id in {d.id for d in my_documents(identity="S. Kumar")}
    assert document.id not in {d.id for d in my_documents(identity="A. Silva")}


def test_finish_review_rejects_a_paper_not_with_a_teacher(document):
    with pytest.raises(HTTPException) as err:
        finish_review(document.id, FinishIn(status="refer_back"))
    assert err.value.status_code == 409


def test_finish_review_refer_back_does_not_promote(document):
    touch_teacher(identity="N. Perera")
    touch_teacher(identity="S. Kumar")
    assign_document(document.id, TWO)

    finished = finish_review(document.id, FinishIn(status="refer_back"))
    assert finished.status == "refer_back"
    with store.connect() as conn:
        promoted = [r for r in store.all_questions(conn)
                   if r["id"].startswith(f"intake-{document.id}-")]
    assert promoted == []


def test_finish_review_done_promotes_every_question(document):
    qs = list_questions(document.id)
    touch_teacher(identity="N. Perera")
    touch_teacher(identity="S. Kumar")
    assign_document(document.id, TWO)

    finished = finish_review(document.id, FinishIn(status="review_done"))
    assert finished.status == "ready_to_publish" and not finished.archived_at

    with store.connect() as conn:
        promoted = {r["id"] for r in store.all_questions(conn)
                   if r["id"].startswith(f"intake-{document.id}-")}
    assert promoted == {f"intake-{document.id}-{q.id}" for q in qs}
