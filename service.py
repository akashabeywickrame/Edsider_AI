"""EdSider AI as a live service - the check, always on.

check_new.py answers one question per process: start Python, load the
model, check, exit. That is fine for a terminal and wrong for anything
that needs to ask "is this a duplicate?" continuously - the model load
alone costs several seconds, every single time.

This is the same three-pass logic wrapped so the model and the bank's
fingerprints load ONCE, when the process starts, and stay in memory.
After that, a request costs an embed-and-compare - a few hundred
milliseconds - not a model reload. Nothing about how a duplicate is
decided changes; only when the expensive part happens changes.

Run with:
    .\\venv\\Scripts\\uvicorn.exe service:app --host 0.0.0.0 --port 8000

Or directly:
    .\\venv\\Scripts\\python.exe service.py

Then browse to http://localhost:8000/docs for a form to try it by hand,
or POST to /check. See README.md for the request and response shape.
"""

import os
import secrets
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src import store, dedupe
from src.dedupe.embeddings import Embedder, DEFAULT_MODEL
from src.intake import extract as intake_extract
from src.intake import review as intake_review
from src.normalize import content_hash, extract_text, has_attachments

# Populated once by load_state(), not per request - that is the entire
# point of this file over the CLI. A plain dict rather than globals so
# tests can swap in a fake embedder without touching the real model.
state = {"embedder": None, "index": None, "rows": None,
         "model_name": DEFAULT_MODEL}


def load_state():
    """Load the model (if not already warm) and rebuild the fingerprint
    index from whatever is currently in the database.

    Called once at startup, and callable again - see /reload - to pick
    up questions imported since the process started, without restarting
    it and paying the model-load cost twice.
    """
    with store.connect() as conn:
        rows = [dict(r) for r in
                store.comparable_questions(conn, state["model_name"])]
    if state["embedder"] is None:
        state["embedder"] = Embedder(state["model_name"])
    state["rows"] = rows
    state["index"] = dedupe.VectorIndex(rows)


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_state()
    yield


app = FastAPI(
    title="EdSider AI - duplicate check",
    description="Check one question against the bank. See /check.",
    version="1.0",
    lifespan=lifespan,
)


# ------------------------------------------------------------- schemas

class CheckRequest(BaseModel):
    text: str = Field(..., min_length=1,
                       description="The question - plain text or Draft.js JSON, either works")
    id: Optional[str] = Field(
        None, description="This question's own id, if it already has one - "
                          "so re-checking an existing question never matches itself")
    grade: Optional[str] = None
    subject: Optional[str] = None
    use_guards: bool = True
    threshold: Optional[float] = Field(
        None, ge=0.0, le=1.0,
        description="Defaults to the review threshold if not given")
    top_k: int = Field(5, ge=1, le=20)


class Match(BaseModel):
    id: str
    text: str
    grade: Optional[str]
    subject: Optional[str]
    raw_score: float
    penalty: float
    score: float
    verdict: str
    reasons: List[str]


class CheckResponse(BaseModel):
    verdict: str
    exact_duplicate_of: List[str]
    matches: List[Match]
    elapsed_ms: float


# ---------------------------------------------------------------- routes

def require_api_key(x_api_key: Optional[str] = Header(None)):
    """Refuse a request without the right X-API-Key header - when a key
    is configured.

    The service answers from EdSider's whole question bank, and /reload
    makes it re-read that bank; neither belongs open to anyone who can
    reach the host. The key is read from EDSIDER_API_KEY - an app setting
    on the server, never in the code - and EdSider's own backend sends it.
    With no key set (a laptop, the tests) everything stays open, exactly
    as before. /health is never gated: the host's uptime probe cannot
    send a header.
    """
    expected = os.environ.get("EDSIDER_API_KEY")
    if not expected:
        return
    # compare_digest, not ==, so response timing does not reveal how much
    # of a guessed key was right.
    if not x_api_key or not secrets.compare_digest(
            x_api_key.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(401, "Missing or wrong X-API-Key header.")


def current_identity(
    x_ms_client_principal_name: Optional[str] = Header(None)
):
    """Who is actually calling - same header, same fallback as app.py's
    current_reviewer() and teacher_app.py's current_teacher(). Azure App
    Service's Easy Auth injects this once configured; a local run (or
    the tests) falls back to "reviewer", same as those two Streamlit
    pages did before this API existed.

    A comment's or a decision's author must come from this, never from
    the request body - letting the caller name itself as anyone it
    likes would make every "who said this" in the review tools fiction.
    """
    return x_ms_client_principal_name or "reviewer"


@app.get("/health")
def health():
    """Is it up, and what is it actually serving against.

    Reachable before startup finishes loading, and even before EDSIDER_DB
    was set at all - store.DSN is None until then, not a crash.
    """
    return {
        "status": "ok" if state["embedder"] is not None else "loading",
        "database": store.display_name(),
        "comparable_questions": len(state["rows"]) if state["rows"] is not None else 0,
        "model": state["model_name"],
    }


@app.post("/reload", dependencies=[Depends(require_api_key)])
def reload_index():
    """Pick up questions imported since this process started.

    Re-embedding is cheap for anything already fingerprinted - embed.py
    and this share the same content_hash cache - so this only pays for
    what is genuinely new.
    """
    before = len(state["rows"]) if state["rows"] is not None else 0
    load_state()
    after = len(state["rows"])
    return {"reloaded": True, "comparable_questions": after,
            "added": after - before}


@app.post("/check", response_model=CheckResponse,
          dependencies=[Depends(require_api_key)])
def check(req: CheckRequest):
    if state["embedder"] is None or state["index"] is None:
        raise HTTPException(503, "Still loading the model - try again shortly.")

    started = time.perf_counter()
    text = extract_text(req.text)
    if not text.strip():
        raise HTTPException(400, "No question text found after extraction.")

    # Pass 1 - exact match. One indexed lookup, no model involved, so the
    # common case (a question already in the bank, retyped) never pays
    # for an embedding at all.
    new_hash = content_hash(text)
    with store.connect() as conn:
        exact = store.find_by_hash(conn, new_hash, exclude_id=req.id)
    if exact:
        elapsed = (time.perf_counter() - started) * 1000
        return CheckResponse(
            verdict="EXACT DUPLICATE",
            exact_duplicate_of=[r["id"] for r in exact],
            matches=[],
            elapsed_ms=round(elapsed, 1),
        )

    # Passes 2 and 3. Embedded as a passage, not a query - the bank was
    # built with passage vectors, and a duplicate check compares like
    # with like. See check_new.py for the same reasoning.
    vector = state["embedder"].encode(text, is_query=False)[0]
    question = {"id": req.id, "text": text, "vector": vector,
                "grade": req.grade, "subject": req.subject}
    threshold = (req.threshold if req.threshold is not None
                else dedupe.REVIEW_THRESHOLD)

    hits = dedupe.check_one(question, state["index"], use_guards=req.use_guards,
                            threshold=threshold, top_k=req.top_k)
    flagged = [h for h in hits if h["score"] >= threshold]
    verdict = flagged[0]["verdict"] if flagged else "DISTINCT"

    matches = [
        Match(id=h["match"]["id"], text=h["match"]["text"],
              grade=h["match"]["grade"], subject=h["match"]["subject"],
              raw_score=round(h["raw_score"], 4), penalty=round(h["penalty"], 4),
              score=round(h["score"], 4), verdict=h["verdict"],
              reasons=h["reasons"])
        for h in hits
    ]

    elapsed = (time.perf_counter() - started) * 1000
    return CheckResponse(verdict=verdict, exact_duplicate_of=[],
                         matches=matches, elapsed_ms=round(elapsed, 1))


# ===================================================================
# Paper intake, teacher review, paper assignment
#
# Everything below is the API surface the three Streamlit apps built
# for this workflow (app.py's "Paper intake" page, teacher_app.py,
# masd_app.py) would call instead of importing src.store directly - the
# first step towards the React frontend described in design-system/.
# Every route is a thin wrapper around a function that already exists
# in src.store or src.intake: nothing about how a paper is extracted,
# split, reviewed, assigned or promoted changes here, only how a caller
# reaches it. Kept in this file, not a second process, so there is one
# API to deploy and one place EDSIDER_API_KEY protects.
# ===================================================================

# ------------------------------------------------------------ schemas

class IntakeDocumentOut(BaseModel):
    id: int
    filename: str
    file_type: Optional[str] = None
    extractor: Optional[str] = None
    uploaded_by: Optional[str] = None
    uploaded_at: Optional[str] = None
    paper_grade: Optional[str] = None
    paper_subject: Optional[str] = None
    paper_title: Optional[str] = None
    split_style: Optional[str] = None
    status: str
    assigned_to: Optional[str] = None
    sections: List[Dict[str, Any]] = []


class IntakeDocumentDetail(IntakeDocumentOut):
    # Left off the list endpoint - a paper's full read-text can be
    # tens of thousands of characters, and a list of papers never
    # needs to show it.
    raw_text: Optional[str] = None


class IntakeQuestionOut(BaseModel):
    id: int
    document_id: int
    seq: int
    question_number: Optional[str] = None
    text: Optional[str] = None
    grade: Optional[str] = None
    subject: Optional[str] = None
    marks: Optional[int] = None
    topic: Optional[str] = None
    answer: Optional[str] = None
    extraction_confidence: Optional[str] = None
    section: Optional[str] = None
    parent_seq: Optional[int] = None
    depth: int = 0
    reviewed: bool = False
    teacher_reviewed: bool = False


class QuestionUpdate(BaseModel):
    """Every field a reviewer may change - the same set
    store._INTAKE_Q_EDITABLE accepts. Only fields actually present in
    the request body are applied (see update_question's exclude_unset),
    so a PATCH never resets a column the caller did not mean to touch."""
    question_number: Optional[str] = None
    text: Optional[str] = None
    grade: Optional[str] = None
    subject: Optional[str] = None
    marks: Optional[int] = None
    topic: Optional[str] = None
    answer: Optional[str] = None
    extraction_confidence: Optional[str] = None
    section: Optional[str] = None
    reviewed: Optional[bool] = None
    teacher_reviewed: Optional[bool] = None


class QuestionCreate(BaseModel):
    """'Add a question the extractor missed' - a whole question, or a
    part of an existing one when parent_seq names it."""
    text: str = Field(..., min_length=1)
    question_number: Optional[str] = None
    marks: Optional[int] = None
    section: Optional[str] = None
    parent_seq: Optional[int] = Field(
        None, description="The seq of the question this is a part of, "
                          "within the same document - omit for a "
                          "standalone question.")


class DocumentUpdate(BaseModel):
    paper_grade: Optional[str] = None
    paper_subject: Optional[str] = None
    paper_title: Optional[str] = None
    status: Optional[str] = None


class ResplitPreview(BaseModel):
    question_count: int
    sections: List[Dict[str, Any]]
    currently_stored: int


class ResplitRequest(BaseModel):
    style: str


class CommentIn(BaseModel):
    comment: str = Field(..., min_length=1)


class CommentOut(BaseModel):
    id: int
    question_id: int
    author: Optional[str] = None
    comment: str
    created_at: Optional[str] = None


class ImageOut(BaseModel):
    id: int
    question_id: int
    slot: str
    content_type: Optional[str] = None
    created_at: Optional[str] = None


class TeacherOut(BaseModel):
    identity: str
    display_name: Optional[str] = None


class AssignIn(BaseModel):
    teacher_identity: str = Field(..., min_length=1)


class FinishIn(BaseModel):
    status: Literal["refer_back", "review_done"] = Field(
        ..., description="Refer Back - issues need correction, goes back "
                         "to data-entry. Review Done - no issues; "
                         "promotes the paper into the real bank.")


def _document_out(doc, detail=False):
    data = dict(
        id=doc["id"], filename=doc["filename"], file_type=doc["file_type"],
        extractor=doc["extractor"], uploaded_by=doc["uploaded_by"],
        uploaded_at=str(doc["uploaded_at"]) if doc["uploaded_at"] else None,
        paper_grade=doc["paper_grade"], paper_subject=doc["paper_subject"],
        paper_title=doc["paper_title"], split_style=doc["split_style"],
        status=doc["status"], assigned_to=doc["assigned_to"],
        sections=store.intake_sections(doc),
    )
    if detail:
        return IntakeDocumentDetail(raw_text=doc["raw_text"], **data)
    return IntakeDocumentOut(**data)


def _question_out(q):
    d = dict(q)
    d["reviewed"] = bool(d.get("reviewed"))
    d["teacher_reviewed"] = bool(d.get("teacher_reviewed"))
    return IntakeQuestionOut(**d)


def _require_document(conn, doc_id):
    doc = store.get_intake_document(conn, doc_id)
    if doc is None:
        raise HTTPException(404, f"No intake document with id {doc_id}.")
    return doc


def _require_question(conn, q_id):
    q = store.get_intake_question(conn, q_id)
    if q is None:
        raise HTTPException(404, f"No intake question with id {q_id}.")
    return q


# ----------------------------------------------------- intake documents

@app.get("/intake/documents", response_model=List[IntakeDocumentOut],
         dependencies=[Depends(require_api_key)])
def list_documents(status: Optional[str] = None):
    """Every uploaded paper, newest first - the data-entry tool's own
    list and the assigning administrator's "ready to assign" /
    "already assigned" tables both read this, filtering on `status`
    client-side-friendly via the optional query param rather than two
    different endpoints for what is the same table."""
    with store.connect() as conn:
        docs = [d for d in store.intake_documents(conn)
               if status is None or d["status"] == status]
    return [_document_out(d) for d in docs]


@app.post("/intake/documents", response_model=IntakeDocumentDetail,
         dependencies=[Depends(require_api_key)])
def upload_document(
    file: UploadFile = File(...),
    lang: str = Form(intake_extract.DEFAULT_OCR_LANG),
    style: str = Form(intake_extract.DEFAULT_SPLIT_STYLE),
    identity: str = Depends(current_identity),
):
    """Upload a paper and extract its questions in one call - app.py's
    file_uploader + "Extract questions" button, as one request.

    Extraction needs a real file on disk (pdfplumber, python-docx and
    Tesseract all read paths, not in-memory streams), so the upload is
    written to a temp file and removed again once read, exactly as
    app.py already does; the file's own bytes are kept in the database
    afterwards (file_blob) for the paper-preview pane, not on disk.
    """
    contents = file.file.read()
    if not contents:
        raise HTTPException(400, "The uploaded file is empty.")
    suffix = Path(file.filename or "").suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(contents)
        tmp_path = tmp.name
    try:
        result = intake_extract.extract(
            tmp_path, style=style, lang=lang, filename=file.filename)
    finally:
        os.unlink(tmp_path)

    with store.connect() as conn:
        doc_id = store.create_intake_document(
            conn, file.filename or "upload", file_type=result["file_type"],
            extractor=result["extractor"], uploaded_by=identity,
            paper_grade=result["paper"]["grade"],
            paper_subject=result["paper"]["subject"],
            paper_title=result["paper"]["title"],
            sections=result["sections"], raw_text=result["raw_text"],
            split_style=style, file_blob=contents,
        )
        store.add_extracted_questions(
            conn, doc_id, result["questions"],
            fallback_grade=result["paper"]["grade"],
            fallback_subject=result["paper"]["subject"],
        )
        doc = store.get_intake_document(conn, doc_id)
    return _document_out(doc, detail=True)


@app.get("/intake/documents/{doc_id}", response_model=IntakeDocumentDetail,
         dependencies=[Depends(require_api_key)])
def get_document(doc_id: int):
    with store.connect() as conn:
        doc = _require_document(conn, doc_id)
    return _document_out(doc, detail=True)


@app.patch("/intake/documents/{doc_id}", response_model=IntakeDocumentDetail,
          dependencies=[Depends(require_api_key)])
def update_document(doc_id: int, body: DocumentUpdate):
    fields = body.model_dump(exclude_unset=True)
    with store.connect() as conn:
        _require_document(conn, doc_id)
        if fields:
            store.update_intake_document(conn, doc_id, **fields)
        doc = store.get_intake_document(conn, doc_id)
    return _document_out(doc, detail=True)


@app.post("/intake/documents/{doc_id}/resplit/preview",
         response_model=ResplitPreview, dependencies=[Depends(require_api_key)])
def preview_resplit(doc_id: int, body: ResplitRequest):
    """What a different split style WOULD find, without changing
    anything - the live count the design shows before "Re-split this
    paper" is pressed. Pure text processing (intake_extract.structure),
    so this is cheap enough to call on every style change."""
    with store.connect() as conn:
        doc = _require_document(conn, doc_id)
        stored = len(store.intake_questions(conn, doc_id))
    if not doc["raw_text"]:
        raise HTTPException(
            409, "This paper was uploaded before its raw text was kept, "
                "so it cannot be re-split - upload it again.")
    parsed = intake_extract.structure(doc["raw_text"], style=body.style)
    return ResplitPreview(question_count=len(parsed["questions"]),
                          sections=parsed["sections"],
                          currently_stored=stored)


@app.post("/intake/documents/{doc_id}/resplit",
         response_model=IntakeDocumentDetail,
         dependencies=[Depends(require_api_key)])
def resplit_document(doc_id: int, body: ResplitRequest):
    """Commit a re-split - destructive, same as app.py's own button:
    the previous drafts (and any edits already made to them) are
    replaced by a fresh segmentation. See store.replace_intake_questions."""
    with store.connect() as conn:
        doc = _require_document(conn, doc_id)
        if not doc["raw_text"]:
            raise HTTPException(
                409, "This paper was uploaded before its raw text was "
                    "kept, so it cannot be re-split - upload it again.")
        parsed = intake_extract.structure(doc["raw_text"], style=body.style)
        meta = intake_extract.detect_paper_meta(
            doc["raw_text"], filename=doc["filename"])
        # A paper read before grades/subjects came back as the bank's own
        # codes may hold something like "13" or "Combined Mathematics" -
        # not a real code, so a fresh reading replaces it; a reviewer's
        # own already-valid choice is left alone. Same rule as app.py.
        grade = (doc["paper_grade"]
                if intake_extract.is_grade_code(doc["paper_grade"])
                else meta["grade"] or doc["paper_grade"])
        subject = (doc["paper_subject"]
                  if doc["paper_subject"] in intake_extract.KNOWN_SUBJECTS
                  else meta["subject"] or doc["paper_subject"])
        store.replace_intake_questions(
            conn, doc_id, parsed["questions"],
            fallback_grade=grade, fallback_subject=subject)
        store.update_intake_document(
            conn, doc_id, split_style=body.style, status="extracted",
            sections=parsed["sections"], paper_grade=grade,
            paper_subject=subject,
            paper_title=doc.get("paper_title") or meta["title"],
        )
        doc = store.get_intake_document(conn, doc_id)
    return _document_out(doc, detail=True)


@app.post("/intake/documents/{doc_id}/check-duplicates",
         dependencies=[Depends(require_api_key)])
def check_document_duplicates(doc_id: int) -> Dict[str, Any]:
    """The within-paper duplicate check - src.intake.review.check_batch,
    gated the same way app.py gates its button: every question must be
    reviewed first, because running it on uncorrected text is
    meaningless. Marks the document 'checked' on completion, which is
    what makes it appear in the assigning administrator's "ready to
    assign" list (see list_documents(status="checked")).
    """
    with store.connect() as conn:
        _require_document(conn, doc_id)
        everything = [dict(q) for q in store.intake_questions(conn, doc_id)]
    if not everything:
        raise HTTPException(400, "This paper has no questions yet.")
    pending = sum(1 for q in everything if not q["reviewed"])
    if pending:
        raise HTTPException(
            409, f"{pending} question(s) still not marked reviewed.")
    if state["embedder"] is None:
        raise HTTPException(503, "Still loading the model - try again shortly.")

    # A question with parts holds only their shared stem - comparing
    # that against the bank finds nothing useful. Same filter as app.py.
    stems = {d["parent_seq"] for d in everything if d.get("parent_seq")}
    fresh = [d for d in everything
            if d["seq"] not in stems and (d["text"] or "").strip()]
    out = intake_review.check_batch(fresh, state["embedder"])
    with store.connect() as conn:
        store.update_intake_document(conn, doc_id, status="checked")
    return out


@app.post("/intake/documents/{doc_id}/assign", response_model=IntakeDocumentDetail,
         dependencies=[Depends(require_api_key)])
def assign_document(doc_id: int, body: AssignIn):
    """The assigning administrator's action - hands a checked paper to
    one teacher, who must already have opened the teacher app once
    (see list_teachers/touch_teacher below) for their identity to exist
    at all. Moves the paper straight to 'teacher_review'."""
    with store.connect() as conn:
        _require_document(conn, doc_id)
        known = {t["identity"] for t in store.list_teachers(conn)}
        if body.teacher_identity not in known:
            raise HTTPException(
                404, f"{body.teacher_identity!r} has never opened the "
                    "teacher app, so cannot be assigned a paper yet.")
        store.assign_document(conn, doc_id, body.teacher_identity)
        doc = store.get_intake_document(conn, doc_id)
    return _document_out(doc, detail=True)


@app.post("/intake/documents/{doc_id}/finish", response_model=IntakeDocumentDetail,
         dependencies=[Depends(require_api_key)])
def finish_review(doc_id: int, body: FinishIn):
    """The teacher's "Finish Review" action. Review Done and promotion
    happen together, same as teacher_app.py: three people (data-entry,
    the assigning administrator, and the teacher) have all signed off
    by this point, so nothing is left for a later manual step to do.
    See store.promote_document for what "promoted" means.
    """
    with store.connect() as conn:
        doc = _require_document(conn, doc_id)
        if doc["status"] != "teacher_review":
            raise HTTPException(
                409, f"Cannot finish review from status {doc['status']!r} "
                    "- this paper is not currently with a teacher.")
        store.update_intake_document(conn, doc_id, status=body.status)
        if body.status == "review_done":
            store.promote_document(conn, doc_id)
        doc = store.get_intake_document(conn, doc_id)
    return _document_out(doc, detail=True)


# ----------------------------------------------------- intake questions

@app.get("/intake/documents/{doc_id}/questions",
        response_model=List[IntakeQuestionOut],
        dependencies=[Depends(require_api_key)])
def list_questions(doc_id: int):
    with store.connect() as conn:
        _require_document(conn, doc_id)
        qs = store.intake_questions(conn, doc_id)
    return [_question_out(q) for q in qs]


@app.post("/intake/documents/{doc_id}/questions", response_model=IntakeQuestionOut,
         dependencies=[Depends(require_api_key)])
def create_question(doc_id: int, body: QuestionCreate):
    """'Add a question the extractor missed' - a whole question, or,
    with parent_seq, a part of one already on the paper. Numbering
    mirrors the paper's own style: a part with no number given gets
    the parent's number plus the next letter after its existing parts,
    same as app.py's own "Add a question" expander."""
    with store.connect() as conn:
        doc = _require_document(conn, doc_id)
        drafts = [dict(q) for q in store.intake_questions(conn, doc_id)]
        by_seq = {d["seq"]: d for d in drafts}
        parent = by_seq.get(body.parent_seq) if body.parent_seq is not None else None
        if body.parent_seq is not None and parent is None:
            raise HTTPException(
                404, f"No question with seq {body.parent_seq} on this paper.")

        number = body.question_number
        if parent and not number:
            siblings = sum(1 for d in drafts if d.get("parent_seq") == parent["seq"])
            number = f"{parent['question_number'] or ''}({chr(ord('a') + siblings)})"

        seq = max((d["seq"] for d in drafts), default=0) + 1
        new_id = store.add_intake_question(
            conn, doc_id, seq, body.text, question_number=number,
            # Grade/subject are paper-level, same as every extracted
            # question's own fallback (add_extracted_questions) - a
            # hand-added question is still a question on this paper.
            grade=doc["paper_grade"], subject=doc["paper_subject"],
            marks=body.marks,
            section=(parent["section"] if parent else body.section),
            parent_seq=parent["seq"] if parent else None,
            depth=((parent.get("depth") or 0) + 1) if parent else 0,
        )
        q = store.get_intake_question(conn, new_id)
    return _question_out(q)


@app.patch("/intake/questions/{q_id}", response_model=IntakeQuestionOut,
          dependencies=[Depends(require_api_key)])
def update_question(q_id: int, body: QuestionUpdate):
    fields = body.model_dump(exclude_unset=True)
    for key in ("reviewed", "teacher_reviewed"):
        if key in fields:
            fields[key] = int(fields[key])
    with store.connect() as conn:
        _require_question(conn, q_id)
        if fields:
            store.update_intake_question(conn, q_id, **fields)
        q = store.get_intake_question(conn, q_id)
    return _question_out(q)


@app.delete("/intake/questions/{q_id}", status_code=204,
           dependencies=[Depends(require_api_key)])
def remove_question(q_id: int):
    with store.connect() as conn:
        _require_question(conn, q_id)
        store.delete_intake_question(conn, q_id)


# ------------------------------------------------------------ comments

@app.get("/intake/documents/{doc_id}/comments",
        response_model=Dict[str, List[CommentOut]],
        dependencies=[Depends(require_api_key)])
def list_comments(doc_id: int):
    with store.connect() as conn:
        _require_document(conn, doc_id)
        by_q = store.question_comments_for_document(conn, doc_id)
    return {
        str(qid): [CommentOut(id=c["id"], question_id=c["question_id"],
                              author=c["author"], comment=c["comment"],
                              created_at=str(c["created_at"]))
                  for c in cs]
        for qid, cs in by_q.items()
    }


@app.post("/intake/questions/{q_id}/comments", response_model=CommentOut,
         dependencies=[Depends(require_api_key)])
def add_comment(q_id: int, body: CommentIn,
                identity: str = Depends(current_identity)):
    with store.connect() as conn:
        _require_question(conn, q_id)
        comment_id = store.add_question_comment(
            conn, q_id, identity, body.comment.strip())
        c = store.get_question_comment(conn, comment_id)
    return CommentOut(id=c["id"], question_id=c["question_id"],
                      author=c["author"], comment=c["comment"],
                      created_at=str(c["created_at"]))


# -------------------------------------------------------------- images

@app.get("/intake/documents/{doc_id}/images",
        response_model=Dict[str, List[ImageOut]],
        dependencies=[Depends(require_api_key)])
def list_document_images(doc_id: int):
    """Metadata only, grouped by question - never the image bytes
    themselves (see get_image below), so opening a paper with a dozen
    attached pictures does not pull every one of them into this
    response. Mirrors store.question_images_for_document()."""
    with store.connect() as conn:
        _require_document(conn, doc_id)
        by_q = store.question_images_for_document(conn, doc_id)
    return {
        str(qid): [ImageOut(id=i["id"], question_id=i["question_id"],
                            slot=i["slot"], content_type=i["content_type"],
                            created_at=str(i["created_at"]))
                  for i in imgs]
        for qid, imgs in by_q.items()
    }


@app.post("/intake/questions/{q_id}/images", response_model=ImageOut,
         dependencies=[Depends(require_api_key)])
def attach_image(q_id: int, slot: str = Form(...), file: UploadFile = File(...)):
    """`slot` is 'question', 'answer', or 'option:<label>' - a specific
    MCQ option's own printed label, from intake_extract.split_mcq_options
    - see intake_question_images for why an option is never its own row."""
    contents = file.file.read()
    if not contents:
        raise HTTPException(400, "The uploaded image is empty.")
    with store.connect() as conn:
        _require_question(conn, q_id)
        image_id = store.add_question_image(
            conn, q_id, slot, contents, content_type=file.content_type)
        img = store.get_question_image(conn, image_id)
    return ImageOut(id=image_id, question_id=q_id, slot=img["slot"],
                    content_type=img["content_type"], created_at=None)


@app.get("/intake/images/{image_id}", dependencies=[Depends(require_api_key)])
def get_image(image_id: int):
    """The image's own bytes, with the content type it was uploaded
    with - an <img src="/intake/images/{id}"> tag's whole job, kept
    separate from the metadata listing above (see store.get_question_image)."""
    with store.connect() as conn:
        img = store.get_question_image(conn, image_id)
    if img is None:
        raise HTTPException(404, f"No image with id {image_id}.")
    return Response(content=img["image_blob"],
                    media_type=img["content_type"] or "application/octet-stream")


@app.delete("/intake/images/{image_id}", status_code=204,
           dependencies=[Depends(require_api_key)])
def remove_image(image_id: int):
    with store.connect() as conn:
        store.delete_question_image(conn, image_id)


# ------------------------------------------------------- teachers / admin

@app.get("/teachers", response_model=List[TeacherOut],
        dependencies=[Depends(require_api_key)])
def list_teachers():
    """Who the assigning administrator can hand a paper to - only
    identities that have opened the teacher app at least once (see
    touch_teacher below), so assigning can never name someone Easy
    Auth will not actually report back."""
    with store.connect() as conn:
        teachers = store.list_teachers(conn)
    return [TeacherOut(identity=t["identity"], display_name=t["display_name"])
           for t in teachers]


@app.post("/teachers/me/touch", response_model=TeacherOut,
         dependencies=[Depends(require_api_key)])
def touch_teacher(identity: str = Depends(current_identity)):
    """Register the calling identity as a teacher - the API equivalent
    of teacher_app.py's "first visit creates the row" upsert_teacher()
    call. The teacher frontend calls this once when its page loads,
    before listing "my papers", exactly as the Streamlit page does."""
    with store.connect() as conn:
        store.upsert_teacher(conn, identity)
        t = next(t for t in store.list_teachers(conn) if t["identity"] == identity)
    return TeacherOut(identity=t["identity"], display_name=t["display_name"])


@app.get("/teachers/me/documents", response_model=List[IntakeDocumentOut],
        dependencies=[Depends(require_api_key)])
def my_documents(identity: str = Depends(current_identity)):
    """Every paper ever assigned to the calling identity - deliberately
    keyed off the request's own header, not a path parameter, so one
    teacher can never list another's papers by guessing their name."""
    with store.connect() as conn:
        docs = store.documents_assigned_to(conn, identity)
    return [_document_out(d) for d in docs]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
