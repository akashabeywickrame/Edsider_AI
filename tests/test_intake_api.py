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

from src import store
import service
from service import (
    AssignIn, CommentIn, DocumentUpdate, FinishIn, QuestionCreate,
    QuestionUpdate, ResplitRequest, add_comment, assign_document,
    attach_image, check_document_duplicates, create_question,
    finish_review, get_document, get_image, list_comments,
    list_document_images, list_documents, list_questions, list_teachers,
    my_documents, preview_resplit, remove_image, remove_question,
    resplit_document, touch_teacher, update_document, update_question,
    upload_document,
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
    q = update_question(qs[0].id, QuestionUpdate(marks=5, reviewed=True))
    assert q.marks == 5
    assert q.reviewed is True
    assert q.teacher_reviewed is False


def test_update_missing_question_is_404(local_db):
    with pytest.raises(HTTPException) as err:
        update_question(999999, QuestionUpdate(marks=1))
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
        update_question(created.id, QuestionUpdate(marks=1))


def test_add_a_part_is_numbered_like_the_paper_would(document):
    qs = list_questions(document.id)
    part = create_question(
        document.id, QuestionCreate(text="Explain your working.",
                                    parent_seq=qs[0].seq))
    assert part.question_number == f"{qs[0].question_number}(a)"
    assert part.depth == 1
    assert part.parent_seq == qs[0].seq


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
        update_question(q.id, QuestionUpdate(reviewed=True))
    service.state["embedder"] = None
    with pytest.raises(HTTPException) as err:
        check_document_duplicates(document.id)
    assert err.value.status_code == 503


def test_check_duplicates_marks_the_document_checked(document):
    qs = list_questions(document.id)
    for q in qs:
        update_question(q.id, QuestionUpdate(reviewed=True))

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


# ------------------------------------------- assign, teacher, promote

def test_assign_requires_a_teacher_who_has_opened_the_app(document):
    with pytest.raises(HTTPException) as err:
        assign_document(document.id, AssignIn(teacher_identity="N. Perera"))
    assert err.value.status_code == 404

    teacher = touch_teacher(identity="N. Perera")
    assert teacher.identity == "N. Perera"
    assert any(t.identity == "N. Perera" for t in list_teachers())

    assigned = assign_document(document.id, AssignIn(teacher_identity="N. Perera"))
    assert assigned.status == "teacher_review"
    assert assigned.assigned_to == "N. Perera"
    assert document.id in {d.id for d in my_documents(identity="N. Perera")}


def test_my_documents_only_shows_the_calling_identity(document):
    touch_teacher(identity="N. Perera")
    touch_teacher(identity="S. Kumar")
    assign_document(document.id, AssignIn(teacher_identity="N. Perera"))
    assert document.id in {d.id for d in my_documents(identity="N. Perera")}
    assert document.id not in {d.id for d in my_documents(identity="S. Kumar")}


def test_finish_review_rejects_a_paper_not_with_a_teacher(document):
    with pytest.raises(HTTPException) as err:
        finish_review(document.id, FinishIn(status="refer_back"))
    assert err.value.status_code == 409


def test_finish_review_refer_back_does_not_promote(document):
    touch_teacher(identity="N. Perera")
    assign_document(document.id, AssignIn(teacher_identity="N. Perera"))

    finished = finish_review(document.id, FinishIn(status="refer_back"))
    assert finished.status == "refer_back"
    with store.connect() as conn:
        promoted = [r for r in store.all_questions(conn)
                   if r["id"].startswith(f"intake-{document.id}-")]
    assert promoted == []


def test_finish_review_done_promotes_every_question(document):
    qs = list_questions(document.id)
    touch_teacher(identity="N. Perera")
    assign_document(document.id, AssignIn(teacher_identity="N. Perera"))

    finished = finish_review(document.id, FinishIn(status="review_done"))
    assert finished.status == "published"

    with store.connect() as conn:
        promoted = {r["id"] for r in store.all_questions(conn)
                   if r["id"].startswith(f"intake-{document.id}-")}
    assert promoted == {f"intake-{document.id}-{q.seq}" for q in qs}
