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

import json
import logging
import mimetypes
import os
import secrets
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

log = logging.getLogger("edsiderai")
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src import id_tokens, store, dedupe
from src.dedupe import guards
from src.dedupe.embeddings import Embedder, DEFAULT_MODEL
from src.intake import extract as intake_extract
from src.intake import review as intake_review
from src.normalize import content_hash, extract_text, has_attachments

# Populated once by load_state(), not per request - that is the entire
# point of this file over the CLI. A plain dict rather than globals so
# tests can swap in a fake embedder without touching the real model.
state = {"embedder": None, "index": None, "rows": None,
         "assessment_groups": None, "model_name": DEFAULT_MODEL}


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
        # Groups of more than one comparable question on the same paper
        # - only those can possibly hold a within-paper repeat. Same
        # filter app.py's load_assessment_groups() applies, kept here so
        # /within-paper-duplicates never pays for the 1,960-round-trip
        # version of this query on every request (see CLAUDE.md, "One
        # query instead of one per item").
        groups = store.questions_in_assessments(conn, state["model_name"])
    if state["embedder"] is None:
        state["embedder"] = Embedder(state["model_name"])
    state["rows"] = rows
    state["assessment_groups"] = {
        aid: [dict(r) for r in rs] for aid, rs in groups.items() if len(rs) > 1
    }
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

# The React frontend (frontend/) calls this API directly from the
# browser, from a different origin (Vite's dev server, and whatever
# domain the built app is eventually served from) - without CORS the
# browser refuses every request before it even reaches a route here.
# EDSIDER_ALLOWED_ORIGINS is a comma-separated list (an App Service
# setting, not a code change, same reasoning as EDSIDER_API_KEY); unset,
# it defaults to the Vite dev server's own ports for local development.
_allowed_origins = [
    o.strip() for o in
    os.environ.get("EDSIDER_ALLOWED_ORIGINS",
                   "http://localhost:5173,http://127.0.0.1:5173").split(",")
    if o.strip()
]

# The built frontend (frontend/dist, `npm run build`), served by this same
# process when present. One address for page and API is what lets sign-in
# work at all: Easy Auth adds the signed-in person's identity only to
# requests for the app they signed in to, and a browser does not carry one
# app's sign-in to another. With the page on eds-dev-ai-web and the API on
# eds-dev-ai-api, every API call arrived with no identity - everyone was
# "reviewer", and the allowlist was never consulted.
FRONTEND_DIR = Path(os.environ.get("EDSIDER_FRONTEND_DIR",
                                   ROOT / "frontend" / "dist")).resolve()


def _serving_frontend():
    return (FRONTEND_DIR / "index.html").is_file()


def _is_page_route(path):
    """The React app's own pages (see frontend/src/App.jsx)."""
    return path in ("/", "/sign-in", "/app") or path.startswith("/app/")


def _is_frontend_path(path):
    if not _serving_frontend():
        return False
    if _is_page_route(path):
        return True
    candidate = (FRONTEND_DIR / path.lstrip("/")).resolve()
    return candidate.is_file() and FRONTEND_DIR in candidate.parents


def _require_signin():
    """Whether a request with no identity is refused (401) instead of
    treated as a local run. On by default when running on Azure App
    Service - which always sets WEBSITE_SITE_NAME - because waiting for
    someone to remember an app setting left the deployed API open to
    anyone with any Google account. EDSIDER_REQUIRE_SIGNIN overrides
    either way ("false" only for a deliberate exception). Read per call,
    not at import, so a test or an App Service setting change applies."""
    setting = os.environ.get("EDSIDER_REQUIRE_SIGNIN", "").strip().lower()
    if setting in ("1", "true", "yes"):
        return True
    if setting in ("0", "false", "no"):
        return False
    return bool(os.environ.get("WEBSITE_SITE_NAME"))


# The page's forwarded Google/Microsoft ID token (see src/id_tokens.py).
# A header of our own, not Authorization: the API's own Easy Auth reads
# Authorization bearer tokens itself and may refuse one it was not set up
# for, before this code ever sees the request.
ID_TOKEN_HEADER = "x-edsiderai-id-token"


@app.middleware("http")
async def require_signed_in_staff(request, call_next):
    """Every API request, checked once here rather than route by route:
    a signed-in identity must be on the staff list (403 otherwise), and
    with EDSIDER_REQUIRE_SIGNIN no identity at all is a 401. /health
    stays open (an uptime probe cannot sign in), and so do the page's
    own files - the sign-in page has to load before anyone can sign in.
    Added before CORS, so CORS wraps it and even a refusal carries the
    headers a browser needs to read it."""
    path = request.url.path
    if request.method == "OPTIONS" or path == "/health" or _is_frontend_path(path):
        return await call_next(request)
    principal = request.headers.get("x-ms-client-principal-name")
    id_token = request.headers.get(ID_TOKEN_HEADER)
    if principal or id_token or _require_signin():
        try:
            await run_in_threadpool(_resolve_identity, principal, id_token)
        except HTTPException as e:
            return JSONResponse({"detail": e.detail}, status_code=e.status_code)
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
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

def require_api_key(x_api_key: Optional[str] = Header(None),
                    api_key: Optional[str] = None):
    """Refuse a request without the right key - when one is configured.

    The service answers from EdSider's whole question bank, and /reload
    makes it re-read that bank; neither belongs open to anyone who can
    reach the host. The key is read from EDSIDER_API_KEY - an app setting
    on the server, never in the code - and EdSider's own backend sends it.
    With no key set (a laptop, the tests) everything stays open, exactly
    as before. /health is never gated: the host's uptime probe cannot
    send a header.

    Accepted two ways: the `X-API-Key` header (every JSON call the
    frontend makes), or an `api_key` query parameter (the few routes used
    directly as a URL - an <img> or <iframe> src, which a browser never
    attaches a custom header to). Checking the header first means a
    caller that can send one never falls back to leaving the key sitting
    in a URL, which turns up in logs and browser history, for nothing.
    """
    expected = os.environ.get("EDSIDER_API_KEY")
    if not expected:
        return
    got = x_api_key or api_key
    # compare_digest, not ==, so response timing does not reveal how much
    # of a guessed key was right.
    if not got or not secrets.compare_digest(
            got.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(401, "Missing or wrong X-API-Key header.")


def _resolve_identity(x_ms_client_principal_name, id_token=None):
    """Shared by current_identity(), current_user() and require_superadmin()
    below - works out the caller's email, looks it up in the staff list
    (EdSider's own staff pattern, see CLAUDE.md, "Sign-in"), and decides
    who this request is allowed to be.

    The email comes from Easy Auth's own header when this app is the one
    the person signed in to; otherwise from the Google/Microsoft ID token
    the page forwards (src/id_tokens.py - signature, audience and expiry
    all checked here, never trusted from the page). The header wins:
    App Service sets it itself and strips any copy a caller sends.

    Neither at all means Easy Auth was never in front of this request -
    a local run, or the tests - so there is nothing to check against the
    list; this falls back to "reviewer" with no role, exactly as
    current_identity() always did. On Azure that fallback is off (see
    _require_signin) and it is a 401 instead.

    EDSIDER_BOOTSTRAP_ADMINS (comma-separated emails, an app setting,
    never in code) is the way into an otherwise-empty allowlist: without
    it, a freshly deployed app with Easy Auth on and no rows in
    authorized_users yet would lock out even the person meant to add
    everyone else.
    """
    # Called as a plain function (tests, other code), an omitted argument
    # arrives as FastAPI's Header(None) marker - truthy, and not a value.
    if not isinstance(x_ms_client_principal_name, str):
        x_ms_client_principal_name = None
    if not isinstance(id_token, str):
        id_token = None
    if x_ms_client_principal_name:
        email = x_ms_client_principal_name.strip().lower()
    elif id_token:
        try:
            email = id_tokens.email_from_id_token(id_token)
        except id_tokens.InvalidToken as e:
            # Usually an hour-old token - Google's and Microsoft's ID
            # tokens expire, and signing in again issues a fresh one. The
            # reason goes in the message and the log: "Invalid audience"
            # (a sign-in through an app registration not in
            # id_tokens.DEFAULT_AUDIENCES) looked exactly like "expired"
            # and cost a pre-prod deployment its sign-in. Never the token.
            log.warning("ID token refused: %s", e)
            raise HTTPException(401, f"Your sign-in was not accepted ({e}) - sign in again.")
    elif _require_signin():
        raise HTTPException(401, "Sign in first.")
    else:
        return {"identity": "reviewer", "name": "reviewer", "role": None}
    bootstrap = {
        e.strip().lower()
        for e in os.environ.get("EDSIDER_BOOTSTRAP_ADMINS", "").split(",")
        if e.strip()
    }
    if email in bootstrap:
        return {"identity": email, "name": email, "role": "superadmin"}
    with store.connect() as conn:
        # effective_role (store.STAFF_ROLE_SQL): nothing for an inactive
        # row; else a superadmin's role; else 'teacher' for anyone EdSider
        # lists as one - EdSider already decides who its teachers are.
        person = store.lookup_staff(conn, email)
    if person is not None and person["effective_role"]:
        return {"identity": email, "name": person["name"] or email,
                "role": person["effective_role"]}
    # Names the email: someone signed in to Google with the wrong account
    # needs to see WHICH one was refused to know to switch. Only ever sent
    # back to the person signed in as it.
    raise HTTPException(
        403, f"{email} is not allowed to use EdsiderAI. "
             "Ask an administrator to add this email, or sign in with a different account.")


def current_identity(
    x_ms_client_principal_name: Optional[str] = Header(None),
    x_edsiderai_id_token: Optional[str] = Header(None),
):
    """Who is actually calling - same header, same fallback as app.py's
    current_reviewer() and teacher_app.py's current_teacher(). Azure App
    Service's Easy Auth injects this once configured.

    A comment's or a decision's author must come from this, never from
    the request body - letting the caller name itself as anyone it
    likes would make every "who said this" in the review tools fiction.

    Raises 403 when Easy Auth reports a real, signed-in email that is
    not on the allowlist - EdSider's own "You are not a user! Please
    contact System Administrator!" rejection, see _resolve_identity().
    """
    return _resolve_identity(x_ms_client_principal_name, x_edsiderai_id_token)["identity"]


def current_user(
    x_ms_client_principal_name: Optional[str] = Header(None),
    x_edsiderai_id_token: Optional[str] = Header(None),
):
    """Identity AND role together - for /me (what the frontend shows in
    the top nav) and require_superadmin() below, which need to know not
    just who is calling but what they are allowed to do."""
    return _resolve_identity(x_ms_client_principal_name, x_edsiderai_id_token)


def require_superadmin(user: dict = Depends(current_user)):
    """Gates the user-management routes below - only a superadmin may
    decide who else can sign in, or what role they hold; today's 'admin'
    keeps its own operational tasks (assigning papers to teachers) but
    cannot touch this table. role is None only on a local run with no
    Easy Auth header at all (see _resolve_identity) - treated as
    superadmin there, the same "everything is open locally" rule
    BYPASS_SIGN_IN already applies on the frontend, so a developer
    without Easy Auth in front of them is never locked out of their own
    machine."""
    if user["role"] not in (None, "superadmin"):
        raise HTTPException(403, "Superadmin access required.")
    return user


def allow(*roles):
    """A route's own section rule: only these roles may call it - plus
    admin and superadmin, who oversee every section, and a local run
    (role None, no Easy Auth at all), where everything stays open. Each
    data route names its section with this (see the role-coverage test in
    tests/test_service.py), so a teacher cannot work the data-entry
    routes, an employee cannot act as a teacher, and neither can assign
    papers - by calling the API directly, not just by typing an address
    the page would not show them."""
    allowed = set(roles) | {"admin", "superadmin"}

    def check(user: dict = Depends(current_user)):
        if user["role"] is not None and user["role"] not in allowed:
            raise HTTPException(403, "Your role cannot use this part of EdsiderAI.")
        return user
    check.allowed_roles = frozenset(allowed)
    return check


EMPLOYEE = allow("employee")
TEACHER = allow("teacher")
EMPLOYEE_OR_TEACHER = allow("employee", "teacher")
ADMIN = allow()


def require_assigner(user: dict = Depends(current_user)):
    """Whoever assigns papers to teachers - admin or superadmin (or a
    local run, role None, same rule as require_superadmin)."""
    if user["role"] not in (None, "admin", "superadmin"):
        raise HTTPException(403, "Admin access required.")
    return user


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


@app.post("/reload", dependencies=[Depends(require_api_key), Depends(ADMIN)])
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
          dependencies=[Depends(require_api_key), Depends(EMPLOYEE)])
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

class InstructionGroup(BaseModel):
    """Instructions for a range of questions - "Answer questions 20 to 25
    using the chart below" - shown above the first of them. `section` is
    the part's label (None for a paper in one piece); `from_`/`to` are
    question numbers as printed. `title` is optional, for a part heading
    the extractor did not recognise ("Part 2")."""
    section: Optional[str] = None
    from_: str = Field(..., alias="from", min_length=1)
    to: str = Field(..., min_length=1)
    title: Optional[str] = None
    text: str = ""

    model_config = {"populate_by_name": True}


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
    # The paper's own general instructions (the block printed above every
    # part, e.g. "Answer all questions") - filled from the paper's opening
    # text by intake_extract.structure()'s `paper_instructions`, editable.
    # Not in `sections`, since it is not one of the paper's labelled parts.
    notes: Optional[str] = None
    assessment_settings: Dict[str, Any] = {}
    instruction_groups: List[InstructionGroup] = []


class IntakeDocumentDetail(IntakeDocumentOut):
    # Left off the list endpoint - a paper's full read-text can be
    # tens of thousands of characters, and a list of papers never
    # needs to show it.
    raw_text: Optional[str] = None
    # What the paper itself states (see intake_extract.suggest_settings) -
    # offered to the assessment form as its starting values, never saved
    # until the data-entry person saves the form.
    suggested_settings: Dict[str, Any] = {}


class McqOptionOut(BaseModel):
    label: str
    marker: str
    text: str


class BlankOut(BaseModel):
    # A fill-in-the-blank question's text is NEVER split (unlike an
    # MCQ's stem/options) - the whole paragraph, blanks and all, stays
    # the one question a reviewer reads and the one piece the duplicate-
    # check fingerprint sees, same reasoning as MCQ options living
    # inside `text`. `number` is only for matching this box to the right
    # blank in the paragraph and to the right line of the paper's own
    # answer key - not shown as a label of its own beyond that.
    number: str
    answer: Optional[str] = None


class McqInfoOut(BaseModel):
    # `stem` is the text with any detected options stripped off the end.
    # The question's REAL text column still holds stem + options as one
    # combined string (CLAUDE.md: "MCQ options live inside content", kept
    # together on purpose for the duplicate-check fingerprint) - this
    # split exists so a reviewer can correct the stem and each option as
    # its own field; saving (see /mcq below) reassembles them into that
    # one combined string again, each option led by its own real printed
    # `marker`, not a guessed-at substitute.
    stem: str
    options: List[McqOptionOut] = []
    # Every correct option's label, lowercase - a question can have more than
    # one. `correct` is the first of them, kept for callers that read one.
    correct_labels: List[str] = []
    correct: Optional[str] = None


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
    # Who last changed this question's own fields, and when - NULL for
    # a question nobody has edited since extraction. See
    # store.stamp_question_editor(), called from every PATCH route.
    last_edited_by: Optional[str] = None
    last_edited_at: Optional[str] = None
    # Computed, not stored - split_mcq_options() run on `text` as this
    # row is built (see _question_out), not fetched separately per
    # question. A page with 100+ questions doing that as its own request
    # per card was measured firing 100+ concurrent GETs that queued
    # behind each other for over 20 seconds, which then starved an
    # unrelated save sent while they were still in flight - computing it
    # inline here costs nothing beyond the regex scan _question_out
    # already reads `text` for.
    mcq: Optional[McqInfoOut] = None
    # Computed the same way and for the same reason as `mcq` above -
    # split_fill_blanks() run on `text`, each blank's `answer` filled in
    # from the paper's own answer key (fill_blank_answers() on `answer`,
    # matched by number) when extraction found one.
    blanks: List[BlankOut] = []
    # 'descriptive', 'mcq', 'fill_blank' or 'sub' - the effective type: a
    # reviewer's correction if one was made, else detected_type. Both
    # are sent so the page can say "detected as X" next to a correction.
    question_type: Optional[str] = None
    detected_type: Optional[str] = None


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
    # null clears the correction, so the detected type applies again.
    question_type: Optional[Literal["descriptive", "mcq", "fill_blank", "sub"]] = None


class MarkItemIn(BaseModel):
    question_id: int
    marks: Optional[int] = None


class MarksBulkIn(BaseModel):
    """Marks for many questions of one paper at once - a whole part set in one
    request, not one request per question (a 1000-mark paper has hundreds)."""
    marks: List[MarkItemIn] = Field(..., min_length=1)


class McqOptionIn(BaseModel):
    marker: str
    text: str


class McqUpdateIn(BaseModel):
    stem: str
    options: List[McqOptionIn] = []


class BlankIn(BaseModel):
    number: str
    answer: Optional[str] = None


class BlanksUpdateIn(BaseModel):
    # The question's own paragraph is never sent here - unlike an MCQ's
    # stem/options, a blank is never pulled out of or reassembled into
    # `text` (see BlankOut). Only the answer key changes.
    blanks: List[BlankIn] = []


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
    after_seq: Optional[int] = Field(
        None, description="Place the new question straight after this seq "
                          "(the questions after it move down one) - a "
                          "missed question 7 goes between 6 and 8, not at "
                          "the end of the paper. Omit to add at the end.")


class DocumentUpdate(BaseModel):
    paper_grade: Optional[str] = None
    paper_subject: Optional[str] = None
    paper_title: Optional[str] = None
    status: Optional[str] = None
    # A part's own `instructions` (the "Answer all the questions..."
    # block a paper prints under its heading) - store.update_intake_document
    # already accepts and JSON-encodes this column; only the request
    # schema was missing it. Sent whole, same as resplit_document writes
    # it - a part is addressed by matching `label` client-side, not by
    # a separate per-part endpoint, since there is no part id to address.
    sections: Optional[List[Dict[str, Any]]] = None
    # The paper's own general instructions - see IntakeDocumentOut.notes.
    notes: Optional[str] = None
    # The assessment form's values - stored whole, see store's
    # assessment_settings column.
    assessment_settings: Optional[Dict[str, Any]] = None
    # Every group instruction on the paper, sent whole (like `sections`) -
    # adding, editing or removing one is the page sending the new list.
    instruction_groups: Optional[List[InstructionGroup]] = None


class ResplitPreview(BaseModel):
    question_count: int
    sections: List[Dict[str, Any]]
    currently_stored: int


class ResplitRequest(BaseModel):
    style: str


class PastedPaperIn(BaseModel):
    """A paper's text read elsewhere - see create_document_from_text."""
    text: str = Field(..., min_length=1)
    # What to call it in the paper list (there is no filename to show).
    title: Optional[str] = None
    style: str = intake_extract.DEFAULT_SPLIT_STYLE


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
    # 'edsider' - from EdSider's own staff tables; 'added_here' - made a
    # teacher by a superadmin, or only known from opening the teacher app.
    source: Optional[str] = None


class TeacherSyncOut(BaseModel):
    teachers: int
    synced_at: Optional[str] = None


class MeOut(BaseModel):
    """What the frontend's top nav shows, and how it decides whether to
    render the app at all or a "contact your administrator" page -
    role is None only for a local run with no Easy Auth in front."""
    identity: str
    name: str
    role: Optional[Literal["superadmin", "admin", "teacher", "employee"]] = None


StaffRole = Literal["superadmin", "admin", "teacher", "employee"]


class StaffIn(BaseModel):
    """A superadmin adding someone (or re-adding to correct them)."""
    email: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    role: StaffRole
    subjects: Optional[List[str]] = None


class StaffUpdate(BaseModel):
    """Only the fields present are changed (see update_staff_member's
    exclude_unset). role null clears a superadmin's role, so an EdSider
    teacher goes back to being a teacher and anyone else loses access."""
    name: Optional[str] = None
    role: Optional[StaffRole] = None
    subjects: Optional[List[str]] = None
    active: Optional[bool] = None


class StaffOut(BaseModel):
    email: str
    name: Optional[str] = None
    # What they may sign in as right now - None means they cannot.
    effective_role: Optional[str] = None
    # What a superadmin set, if anything (None = "whatever EdSider says").
    role: Optional[str] = None
    in_edsider: bool = False
    active: bool = True
    subjects: List[str] = []
    added_by: Optional[str] = None
    added_at: Optional[str] = None
    synced_at: Optional[str] = None
    first_seen_at: Optional[str] = None
    last_seen_at: Optional[str] = None


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
        sections=store.intake_sections(doc), notes=doc["notes"],
        assessment_settings=store.intake_assessment_settings(doc),
        instruction_groups=store.intake_instruction_groups(doc),
    )
    if detail:
        suggested = intake_extract.suggest_settings(doc["raw_text"], data["sections"])
        return IntakeDocumentDetail(raw_text=doc["raw_text"],
                                    suggested_settings=suggested, **data)
    return IntakeDocumentOut(**data)


def _question_out(q, has_parts=False):
    """`has_parts` is passed in rather than looked up here - a whole
    paper's list knows it from one pass over the rows, and a single
    question asks the store once, instead of a query per row."""
    d = dict(q)
    d["reviewed"] = bool(d.get("reviewed"))
    d["teacher_reviewed"] = bool(d.get("teacher_reviewed"))
    # A draft with children is a container holding a shared stem, never
    # something answered directly - never an MCQ or fill-blank itself.
    # A leaf draft is, whether it is a whole question (depth 0) or one
    # part of a structured question (depth > 0) - the real Grade 12
    # General English paper's question 6 is "Underline the option that
    # best expresses the meaning..." with five depth-1 parts, each
    # itself a complete MCQ with its own four lettered options.
    if not has_parts:
        stem, options = intake_extract.split_mcq_options(d.get("text") or "")
        if options:
            labels = intake_extract.mcq_correct_labels(d.get("answer"))
            d["mcq"] = McqInfoOut(
                stem=stem, options=[McqOptionOut(**o) for o in options],
                correct_labels=labels, correct=labels[0] if labels else None)
        else:
            _stem, blank_positions = intake_extract.split_fill_blanks(d.get("text") or "")
            if blank_positions:
                expected = intake_extract.fill_blank_answers(d.get("answer"))
                d["blanks"] = [
                    BlankOut(number=b["number"], answer=expected.get(b["number"]))
                    for b in blank_positions
                ]
    d["detected_type"] = intake_extract.detect_question_type(
        d.get("text"), has_parts)
    d["question_type"] = d.get("question_type") or d["detected_type"]
    if d.get("last_edited_at") is not None:
        d["last_edited_at"] = d["last_edited_at"].isoformat()
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
         dependencies=[Depends(require_api_key), Depends(EMPLOYEE)])
def list_documents(status: Optional[str] = None):
    """Every uploaded paper, newest first - the data-entry tool's own
    list and the assigning administrator's "ready to assign" /
    "already assigned" tables both read this, filtering on `status`
    client-side-friendly via the optional query param rather than two
    different endpoints for what is the same table."""
    with store.connect() as conn:
        # Light columns only - every stored PDF came along with the list.
        _total, docs = store.search_intake_documents(conn, status=status, limit=None)
    return [_document_out(d) for d in docs]


class PaperSearchOut(BaseModel):
    total: int
    items: List[IntakeDocumentOut]
    # Papers per status / subject / grade, across all papers - the filter
    # menus show each choice with its count.
    facets: Dict[str, Dict[str, int]]


@app.get("/intake/documents/search", response_model=PaperSearchOut,
         dependencies=[Depends(require_api_key), Depends(EMPLOYEE)])
def search_documents(status: Optional[str] = None, subject: Optional[str] = None,
                     grade: Optional[str] = None, q: Optional[str] = None,
                     limit: int = 50, offset: int = 0):
    """The data-entry paper finder: one page of papers matching the
    filters (status, subject, grade, and a search over file name and
    title), newest first. A dropdown of every paper stops working long
    before 1,000 of them."""
    limit = max(1, min(limit, 200))
    with store.connect() as conn:
        total, docs = store.search_intake_documents(
            conn, status=status, subject=subject, grade=grade, search=q,
            limit=limit, offset=max(offset, 0))
        facets = store.intake_document_facets(conn)
    return PaperSearchOut(total=total, items=[_document_out(d) for d in docs], facets=facets)


@app.post("/intake/documents", response_model=IntakeDocumentDetail,
         dependencies=[Depends(require_api_key), Depends(EMPLOYEE)])
def upload_document(
    file: UploadFile = File(...),
    lang: str = Form(intake_extract.DEFAULT_OCR_LANG),
    style: str = Form(intake_extract.DEFAULT_SPLIT_STYLE),
    force_ocr: bool = Form(False),
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
            tmp_path, style=style, lang=lang, filename=file.filename,
            force_ocr=force_ocr)
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
            instruction_groups=result["instruction_groups"],
            notes=result["paper_instructions"],
            split_style=style, file_blob=contents,
        )
        store.add_extracted_questions(
            conn, doc_id, result["questions"],
            fallback_grade=result["paper"]["grade"],
            fallback_subject=result["paper"]["subject"],
        )
        doc = store.get_intake_document(conn, doc_id)
    return _document_out(doc, detail=True)


@app.post("/intake/documents/from-text", response_model=IntakeDocumentDetail,
         dependencies=[Depends(require_api_key), Depends(EMPLOYEE)])
def create_document_from_text(body: PastedPaperIn,
                              identity: str = Depends(current_identity)):
    """A paper whose text was read somewhere else - the local OCR tool on
    a data-entry computer (a scanned Sinhala/Tamil paper our server's
    Tesseract cannot read well), or text copied straight out of a
    document. Stored and split exactly like an upload (see
    extract_text()); there is no file, so the preview pane has nothing
    to show and the raw text is the only record of what was read."""
    title = (body.title or "").strip() or "Pasted text"
    result = intake_extract.extract_text(body.text, style=body.style, filename=title)
    if not result["raw_text"]:
        raise HTTPException(400, "The pasted text is empty.")
    with store.connect() as conn:
        doc_id = store.create_intake_document(
            conn, title, file_type=result["file_type"],
            extractor=result["extractor"], uploaded_by=identity,
            paper_grade=result["paper"]["grade"],
            paper_subject=result["paper"]["subject"],
            paper_title=result["paper"]["title"],
            sections=result["sections"], raw_text=result["raw_text"],
            instruction_groups=result["instruction_groups"],
            notes=result["paper_instructions"],
            split_style=body.style,
        )
        store.add_extracted_questions(
            conn, doc_id, result["questions"],
            fallback_grade=result["paper"]["grade"],
            fallback_subject=result["paper"]["subject"],
        )
        doc = store.get_intake_document(conn, doc_id)
    return _document_out(doc, detail=True)


@app.get("/intake/documents/{doc_id}", response_model=IntakeDocumentDetail,
         dependencies=[Depends(require_api_key), Depends(EMPLOYEE_OR_TEACHER)])
def get_document(doc_id: int):
    with store.connect() as conn:
        doc = _require_document(conn, doc_id)
    return _document_out(doc, detail=True)


@app.get("/intake/documents/{doc_id}/file", dependencies=[Depends(require_api_key), Depends(EMPLOYEE_OR_TEACHER)])
def get_document_file(doc_id: int):
    """The uploaded file's own bytes - a PDF embedded directly (real,
    selectable, copyable text, not a re-typed transcript), an image shown
    as one. Same Response-with-media_type pattern as get_image below;
    content type is guessed from the stored filename, since file_type is
    only the coarse 'pdf'/'docx'/'image'/'other' category.
    """
    with store.connect() as conn:
        doc = _require_document(conn, doc_id)
        blob = store.intake_file_blob(conn, doc_id)
    if blob is None:
        raise HTTPException(
            404, "No stored file for this paper - uploaded before this was "
                "kept, or it predates file_blob.")
    content_type, _ = mimetypes.guess_type(doc["filename"] or "")
    return Response(content=blob, media_type=content_type or "application/octet-stream")


@app.patch("/intake/documents/{doc_id}", response_model=IntakeDocumentDetail,
          dependencies=[Depends(require_api_key), Depends(EMPLOYEE)])
def update_document(doc_id: int, body: DocumentUpdate):
    # by_alias: an instruction group is stored with "from" - the name the
    # extractor writes it under - not the model's own "from_".
    fields = body.model_dump(exclude_unset=True, by_alias=True)
    with store.connect() as conn:
        _require_document(conn, doc_id)
        if fields:
            store.update_intake_document(conn, doc_id, **fields)
        doc = store.get_intake_document(conn, doc_id)
    return _document_out(doc, detail=True)


@app.post("/intake/documents/{doc_id}/resplit/preview",
         response_model=ResplitPreview, dependencies=[Depends(require_api_key), Depends(EMPLOYEE)])
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
         dependencies=[Depends(require_api_key), Depends(EMPLOYEE)])
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
        # A re-split rebuilds the group instructions from the text, like
        # the questions they sit between. The paper's general
        # instructions are only filled in when nobody has written any.
        store.update_intake_document(
            conn, doc_id, split_style=body.style, status="extracted",
            sections=parsed["sections"], paper_grade=grade,
            paper_subject=subject,
            paper_title=doc.get("paper_title") or meta["title"],
            instruction_groups=parsed["instruction_groups"],
            notes=doc.get("notes") or parsed["paper_instructions"],
        )
        doc = store.get_intake_document(conn, doc_id)
    return _document_out(doc, detail=True)


@app.post("/intake/documents/{doc_id}/precheck-duplicates",
         dependencies=[Depends(require_api_key), Depends(EMPLOYEE)])
def precheck_document_duplicates(doc_id: int) -> Dict[str, Any]:
    """An early, informational duplicate check - meant to run right after
    extraction, before anyone has spent the ~40 minutes to an hour
    correcting a single question, so whoever decides whether a paper is
    worth that review time (the company pays the teacher once a paper
    is handed over) can see upfront whether it is mostly repeats.

    Unlike check_document_duplicates below, this does not require every
    question reviewed first - that is the whole point of running it
    earlier - and it never changes the document's status: it is a
    heads-up a person reads and decides on, not a gate.

    Checks two different things, both already built for other pages:
    - within this paper, via the same intake_review.check_batch() the
      post-review check below uses;
    - against the WHOLE bank, via dedupe.check_one() - the same engine
      "Check a question" already runs for one question by hand, run
      here once per extracted draft, reusing the bank's own already-
      built index (state["index"]) rather than rebuilding it per
      question.

    Noisier than the post-review check by nature - it runs on raw,
    uncorrected extraction text (OCR errors, mis-split questions and
    all), not text a person has actually checked - which is exactly why
    it is informational only, never a gate.
    """
    with store.connect() as conn:
        _require_document(conn, doc_id)
        everything = [dict(q) for q in store.intake_questions(conn, doc_id)]
    if not everything:
        raise HTTPException(400, "This paper has no questions yet.")
    if state["embedder"] is None or state["index"] is None:
        raise HTTPException(503, "Still loading the model - try again shortly.")

    # A question with parts holds only their shared stem - comparing
    # that against the bank finds nothing useful. Same filter as
    # check_document_duplicates below.
    stems = {d["parent_seq"] for d in everything if d.get("parent_seq")}
    fresh = [d for d in everything
            if d["seq"] not in stems and (d["text"] or "").strip()]
    if not fresh:
        return {"within_paper": {"exact_groups": [], "similar_pairs": []},
               "against_bank": [], "questions_checked": 0}

    texts = [d["text"] for d in fresh]
    vectors = state["embedder"].encode(texts, is_query=False)
    within_paper = intake_review.check_batch(fresh, state["embedder"],
                                              vectors=vectors)

    against_bank = []
    for d, vector in zip(fresh, vectors):
        question = {"id": None, "text": d["text"], "vector": vector,
                   "grade": d.get("grade"), "subject": d.get("subject")}
        hits = dedupe.check_one(question, state["index"],
                                threshold=dedupe.REVIEW_THRESHOLD, top_k=3)
        flagged = [h for h in hits if h["score"] >= dedupe.REVIEW_THRESHOLD]
        if flagged:
            against_bank.append({
                "question_id": d["id"],
                "question_number": d.get("question_number"),
                "text": d["text"],
                "matches": [
                    {
                        "id": h["match"]["id"],
                        "text": h["match"]["text"],
                        "grade": h["match"].get("grade"),
                        "subject": h["match"].get("subject"),
                        "score": h["score"],
                        "raw_score": h["raw_score"],
                        "verdict": h["verdict"],
                        "reasons": h["reasons"],
                    }
                    for h in flagged
                ],
            })

    return {
        "within_paper": within_paper,
        "against_bank": against_bank,
        "questions_checked": len(fresh),
    }


@app.post("/intake/documents/{doc_id}/check-duplicates",
         dependencies=[Depends(require_api_key), Depends(EMPLOYEE)])
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
         dependencies=[Depends(require_api_key), Depends(ADMIN)])
def assign_document(doc_id: int, body: AssignIn):
    """The assigning administrator's action - hands a checked paper to
    one teacher: one of EdSider's own (see sync_teachers), or someone who
    has opened the teacher app here. Moves the paper straight to
    'teacher_review'."""
    with store.connect() as conn:
        _require_document(conn, doc_id)
        known = {t["identity"] for t in store.assignable_teachers(conn)}
        if body.teacher_identity not in known:
            raise HTTPException(
                404, f"{body.teacher_identity!r} is not an EdSider teacher "
                    "and has never opened the teacher app here.")
        store.assign_document(conn, doc_id, body.teacher_identity)
        doc = store.get_intake_document(conn, doc_id)
    return _document_out(doc, detail=True)


@app.post("/intake/documents/{doc_id}/finish", response_model=IntakeDocumentDetail,
         dependencies=[Depends(require_api_key), Depends(TEACHER)])
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
        dependencies=[Depends(require_api_key), Depends(EMPLOYEE_OR_TEACHER)])
def list_questions(doc_id: int):
    with store.connect() as conn:
        _require_document(conn, doc_id)
        qs = store.intake_questions(conn, doc_id)
    parent_seqs = {q["parent_seq"] for q in qs if q["parent_seq"] is not None}
    return [_question_out(q, has_parts=q["seq"] in parent_seqs) for q in qs]


@app.post("/intake/documents/{doc_id}/questions", response_model=IntakeQuestionOut,
         dependencies=[Depends(require_api_key), Depends(EMPLOYEE)])
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

        if body.after_seq is not None:
            # 0 is "before the first question" - a missed question 1.
            if body.after_seq != 0 and body.after_seq not in by_seq:
                raise HTTPException(
                    404, f"No question with seq {body.after_seq} on this paper.")
            store.make_room_after(conn, doc_id, body.after_seq)
            seq = body.after_seq + 1
            # The parent itself may have moved down to make room.
            if parent and parent["seq"] > body.after_seq:
                parent = dict(parent, seq=parent["seq"] + 1)
        else:
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
          dependencies=[Depends(require_api_key), Depends(EMPLOYEE_OR_TEACHER)])
def update_question(q_id: int, body: QuestionUpdate,
                    identity: str = Depends(current_identity)):
    fields = body.model_dump(exclude_unset=True)
    for key in ("reviewed", "teacher_reviewed"):
        if key in fields:
            fields[key] = int(fields[key])
    with store.connect() as conn:
        _require_question(conn, q_id)
        if fields:
            store.update_intake_question(conn, q_id, **fields)
            store.stamp_question_editor(conn, q_id, identity)
        q = store.get_intake_question(conn, q_id)
        has_parts = store.intake_question_has_children(conn, q_id)
    return _question_out(q, has_parts=has_parts)


@app.post("/intake/documents/{doc_id}/marks", response_model=List[IntakeQuestionOut],
         dependencies=[Depends(require_api_key), Depends(EMPLOYEE)])
def set_question_marks(doc_id: int, body: MarksBulkIn,
                       identity: str = Depends(current_identity)):
    """Sets each named question's marks in one transaction and returns the
    paper's questions as they now stand. A question that is not on this paper
    refuses the whole request, so a partial save never happens silently."""
    with store.connect() as conn:
        _require_document(conn, doc_id)
        on_paper = {q["id"] for q in store.intake_questions(conn, doc_id)}
        stray = [m.question_id for m in body.marks if m.question_id not in on_paper]
        if stray:
            raise HTTPException(
                404, f"Questions {stray} are not on paper {doc_id}.")
        for m in body.marks:
            store.update_intake_question(conn, m.question_id, marks=m.marks)
            store.stamp_question_editor(conn, m.question_id, identity)
        qs = store.intake_questions(conn, doc_id)
        parent_seqs = {q["parent_seq"] for q in qs if q["parent_seq"] is not None}
    return [_question_out(q, has_parts=q["seq"] in parent_seqs) for q in qs]


@app.delete("/intake/questions/{q_id}", status_code=204,
           dependencies=[Depends(require_api_key), Depends(EMPLOYEE)])
def remove_question(q_id: int):
    with store.connect() as conn:
        _require_question(conn, q_id)
        store.delete_intake_question(conn, q_id)


@app.patch("/intake/questions/{q_id}/mcq", response_model=IntakeQuestionOut,
          dependencies=[Depends(require_api_key), Depends(EMPLOYEE)])
def update_question_mcq(q_id: int, body: McqUpdateIn,
                        identity: str = Depends(current_identity)):
    """Reassembles an edited stem and options back into this question's
    one combined text field - the single place that join happens, so a
    reviewer fixing a wrongly-read option never has to reproduce the
    paper's own numbering style by hand. Each option is led by its own
    `marker` exactly as sent (normally round-tripped unchanged from what
    GET .../mcq returned), not a label-derived guess - see McqInfoOut.

    `marker.rstrip()` matters: _SUB_MARKER's own pattern ends in
    `[ \\t]*`, so a marker already carries whatever gap the paper printed
    ("I." plus every space before "Ignoring") - appending another space
    unconditionally would grow that gap by one character every time a
    question round-trips through this endpoint, since the next read
    would capture the now-longer gap as part of the marker again.
    """
    combined = body.stem.strip()
    if body.options:
        combined += "\n" + "\n".join(
            f"{o.marker.rstrip()} {o.text}".strip() for o in body.options)
    with store.connect() as conn:
        _require_question(conn, q_id)
        store.update_intake_question(conn, q_id, text=combined)
        store.stamp_question_editor(conn, q_id, identity)
        q = store.get_intake_question(conn, q_id)
        has_parts = store.intake_question_has_children(conn, q_id)
    return _question_out(q, has_parts=has_parts)


@app.patch("/intake/questions/{q_id}/blanks", response_model=IntakeQuestionOut,
          dependencies=[Depends(require_api_key), Depends(EMPLOYEE)])
def update_question_blanks(q_id: int, body: BlanksUpdateIn,
                           identity: str = Depends(current_identity)):
    """Reassembles a fill-in-the-blank question's edited per-blank
    answers back into one flat answer key ("1. for\\n2. across\\n..."),
    the same shape _attach_answers() reads out of the paper's own
    marking scheme and fill_blank_answers() parses back - a reviewer
    fixing a wrongly-read answer never has to reproduce that shape by
    hand. The question's own paragraph (`text`) is untouched - unlike an
    MCQ, a blank is never pulled out of or reassembled into it.
    """
    combined = "\n".join(
        f"{b.number}. {b.answer.strip()}" for b in body.blanks if (b.answer or "").strip()
    )
    with store.connect() as conn:
        _require_question(conn, q_id)
        store.update_intake_question(conn, q_id, answer=combined or None)
        store.stamp_question_editor(conn, q_id, identity)
        q = store.get_intake_question(conn, q_id)
        has_parts = store.intake_question_has_children(conn, q_id)
    return _question_out(q, has_parts=has_parts)


# ------------------------------------------------------------ comments

@app.get("/intake/documents/{doc_id}/comments",
        response_model=Dict[str, List[CommentOut]],
        dependencies=[Depends(require_api_key), Depends(EMPLOYEE_OR_TEACHER)])
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
         dependencies=[Depends(require_api_key), Depends(TEACHER)])
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
        dependencies=[Depends(require_api_key), Depends(EMPLOYEE_OR_TEACHER)])
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
         dependencies=[Depends(require_api_key), Depends(EMPLOYEE)])
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


@app.get("/intake/images/{image_id}", dependencies=[Depends(require_api_key), Depends(EMPLOYEE_OR_TEACHER)])
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
           dependencies=[Depends(require_api_key), Depends(EMPLOYEE)])
def remove_image(image_id: int):
    with store.connect() as conn:
        store.delete_question_image(conn, image_id)


# ------------------------------------------------------- teachers / admin

@app.get("/me", response_model=MeOut, dependencies=[Depends(require_api_key)])
def me(user: dict = Depends(current_user)):
    """The frontend calls this once on load: a 403 here means Easy Auth
    signed someone in but they are not on the allowlist, and the
    frontend shows a "contact your administrator" page instead of the
    app - this is the actual "at login, check the signed-in email
    against the list" gate, not something enforced route by route."""
    return MeOut(**user)


_STAFF_TIMES = ("added_at", "synced_at", "first_seen_at", "last_seen_at")


def _staff_out(row):
    d = dict(row)
    for key in _STAFF_TIMES:
        if d.get(key) is not None:
            d[key] = d[key].isoformat()
    d["subjects"] = json.loads(d["subjects"]) if d.get("subjects") else []
    return StaffOut(**d)


def _require_staff(conn, email):
    row = store.lookup_staff(conn, email)
    if row is None:
        raise HTTPException(404, f"No staff member {email!r}.")
    return row


@app.get("/admin/users", response_model=List[StaffOut],
        dependencies=[Depends(require_api_key)])
def list_staff_members(user: dict = Depends(require_superadmin)):
    """Every staff profile - EdSider's teachers, people a superadmin
    added, and anyone seen in the teacher app - one row each."""
    with store.connect() as conn:
        rows = store.list_staff(conn)
    return [_staff_out(r) for r in rows]


@app.post("/admin/users", response_model=StaffOut,
         dependencies=[Depends(require_api_key)])
def add_staff_member(body: StaffIn, user: dict = Depends(require_superadmin)):
    """A superadmin adding one person by hand - EdSider's own staff
    pattern (see CLAUDE.md, "Sign-in"), not a self-service sign-up with
    an emailed verification link. Re-adding an existing email corrects
    their name/role rather than erroring."""
    email = body.email.strip().lower()
    subjects = [s.strip() for s in body.subjects if s.strip()] if body.subjects is not None else None
    with store.connect() as conn:
        store.set_staff(conn, email, body.name.strip(), body.role,
                        added_by=user["identity"], subjects=subjects)
        row = store.lookup_staff(conn, email)
    return _staff_out(row)


@app.patch("/admin/users/{email}", response_model=StaffOut,
          dependencies=[Depends(require_api_key)])
def update_staff_member(email: str, body: StaffUpdate,
                        user: dict = Depends(require_superadmin)):
    """Change a name, role, subjects, or switch someone off (`active`) -
    including an EdSider teacher, whose sign-in EdSider grants but a
    superadmin here can still stop."""
    email = email.strip().lower()
    fields = body.model_dump(exclude_unset=True)
    if "subjects" in fields and fields["subjects"] is not None:
        fields["subjects"] = [s.strip() for s in fields["subjects"] if s.strip()]
    if fields.get("active") is False and email == user["identity"]:
        raise HTTPException(400, "You cannot switch off your own account.")
    with store.connect() as conn:
        _require_staff(conn, email)
        store.update_staff(conn, email, **fields)
        row = store.lookup_staff(conn, email)
    return _staff_out(row)


@app.delete("/admin/users/{email}", status_code=204,
           dependencies=[Depends(require_api_key)])
def remove_staff_member(email: str, user: dict = Depends(require_superadmin)):
    """Take away what a superadmin granted (see store.remove_staff_access
    - an EdSider teacher stays a teacher; switch them off with `active`)."""
    with store.connect() as conn:
        store.remove_staff_access(conn, email.strip().lower())


@app.get("/teachers", response_model=List[TeacherOut],
        dependencies=[Depends(require_api_key), Depends(ADMIN)])
def list_teachers():
    """Who the assigning administrator can hand a paper to - every
    EdSider teacher (keyed by the email they sign in with, which is what
    Easy Auth reports back, so an assignment always reaches them), plus
    anyone who has opened the teacher app here without being one."""
    with store.connect() as conn:
        teachers = store.assignable_teachers(conn)
    return [TeacherOut(identity=t["identity"], display_name=t["display_name"],
                       source=t["source"]) for t in teachers]


@app.post("/teachers/sync", response_model=TeacherSyncOut,
         dependencies=[Depends(require_api_key)])
def sync_teachers_from_edsider(user: dict = Depends(require_assigner)):
    """Re-read EdSider's teacher list now - the assignment page calls this
    when it opens, so a teacher added in EdSider a minute ago is already
    in the dropdown. A failure here (EdSider unreachable, PG_* unset) is
    a 502 naming the cause; the last list copied stays in place and the
    dropdown still works from it."""
    from scripts.sync import sync_teachers
    try:
        count = sync_teachers()
    except (Exception, SystemExit) as e:
        # SystemExit: _pg_connect() exits that way when PG_* is unset.
        raise HTTPException(502, f"Could not refresh teachers from EdSider: {e}")
    with store.connect() as conn:
        at = store.edsider_teachers_synced_at(conn)
    return TeacherSyncOut(teachers=count, synced_at=at.isoformat() if at else None)


class EdsiderListsOut(BaseModel):
    synced_at: Optional[str] = None
    # {kind: [{id, name, short_code, extra}]} - grade, subject, language,
    # term, year, exam_category, difficulty, source, author, part_notation,
    # part, assessment_category, grading_method, privilege, attempt,
    # time_unit, passage_type, question_type, answer_type,
    # assessment_status, chapter.
    lists: Dict[str, List[Dict[str, Any]]]


@app.get("/edsider/lists", response_model=EdsiderListsOut,
         dependencies=[Depends(require_api_key), Depends(EMPLOYEE)])
def edsider_lists():
    """EdSider's own reference lists as last copied - what a paper's
    labels are chosen from, so it is sent to EdSider with EdSider's ids."""
    with store.connect() as conn:
        at = store.edsider_lookups_synced_at(conn)
        return EdsiderListsOut(synced_at=at.isoformat() if at else None,
                               lists=store.edsider_lookups(conn))


@app.post("/edsider/lists/sync", response_model=EdsiderListsOut,
         dependencies=[Depends(require_api_key), Depends(ADMIN)])
def sync_edsider_lists():
    """Re-read EdSider's reference lists now. A failure (EdSider
    unreachable, PG_* unset) is a 502 naming the cause; the last copy
    stays in place and keeps working."""
    from scripts.sync import sync_lookups
    try:
        sync_lookups()
    except (Exception, SystemExit) as e:
        # SystemExit: _pg_connect() exits that way when PG_* is unset.
        raise HTTPException(502, f"Could not refresh EdSider's lists: {e}")
    return edsider_lists()


@app.post("/teachers/me/touch", response_model=TeacherOut,
         dependencies=[Depends(require_api_key), Depends(TEACHER)])
def touch_teacher(identity: str = Depends(current_identity)):
    """Register the calling identity as a teacher - the API equivalent
    of teacher_app.py's "first visit creates the row" upsert_teacher()
    call. The teacher frontend calls this once when its page loads,
    before listing "my papers", exactly as the Streamlit page does."""
    with store.connect() as conn:
        store.upsert_teacher(conn, identity)
        t = store.lookup_staff(conn, identity)
    return TeacherOut(identity=t["email"], display_name=t["name"],
                      source="edsider" if t["in_edsider"] else "added_here")


@app.get("/teachers/me/documents", response_model=List[IntakeDocumentOut],
        dependencies=[Depends(require_api_key), Depends(TEACHER)])
def my_documents(identity: str = Depends(current_identity)):
    """Every paper ever assigned to the calling identity - deliberately
    keyed off the request's own header, not a path parameter, so one
    teacher can never list another's papers by guessing their name."""
    with store.connect() as conn:
        docs = store.documents_assigned_to(conn, identity)
    return [_document_out(d) for d in docs]


# ===================================================================
# Find duplicates, duplicates within a paper, reviewed decisions,
# search by meaning, question bank
#
# The rest of app.py's own pages - the original 7-page data-entry tool,
# not the paper-intake workflow above. Every route here reads from
# `state` (the bank held in memory since load_state()/lifespan - see
# the top of this file) rather than re-querying the database per
# request, the same reasoning app.py's own cache_resource bank already
# uses: a whole-bank sweep is 128 million pairs, and paying a database
# round trip for 16,000+ rows on every single request would undo the
# entire point of this file over the CLI.
# ===================================================================

class QuestionBrief(BaseModel):
    id: str
    text: str
    grade: Optional[str] = None
    subject: Optional[str] = None
    topic: Optional[str] = None
    contributor: Optional[str] = None
    title: Optional[str] = None


class DecisionOut(BaseModel):
    id: int
    question_a: str
    question_b: str
    verdict: str
    similarity: Optional[float] = None
    guard_flags: Optional[str] = None
    decided_by: Optional[str] = None
    decided_at: Optional[str] = None
    only_in_a: List[str] = []
    only_in_b: List[str] = []
    text_a: Optional[str] = None
    text_b: Optional[str] = None


class DuplicatePairOut(BaseModel):
    a: QuestionBrief
    b: QuestionBrief
    score: float
    raw_score: float
    penalty: float
    verdict: str
    reasons: List[str]
    assessment_id: Optional[int] = None
    shared_passage_with: List[int] = []
    decision: Optional[DecisionOut] = None


class DuplicateSweepOut(BaseModel):
    threshold: float
    use_guards: bool
    pairs_with_guards: int
    pairs_without_guards: int
    pending: int
    pairs: List[DuplicatePairOut]


class WithinPaperSweepOut(BaseModel):
    threshold: float
    use_guards: bool
    papers_checked: int
    repeats_found: int
    pending: int
    pairs: List[DuplicatePairOut]


class DecisionIn(BaseModel):
    a: str
    b: str
    verdict: Literal["duplicate", "distinct"]
    similarity: Optional[float] = None
    guard_flags: Optional[str] = None


class SearchHitOut(BaseModel):
    score: float
    question: QuestionBrief


def _question_brief(q):
    return QuestionBrief(
        id=q["id"], text=q["text"] or "", grade=q.get("grade"),
        subject=q.get("subject"), topic=q.get("topic"),
        contributor=q.get("contributor"), title=q.get("title"),
    )


def _decision_out(d):
    only_a, only_b = d.get("only_in_a"), d.get("only_in_b")
    return DecisionOut(
        id=d["id"], question_a=d["question_a"], question_b=d["question_b"],
        verdict=d["verdict"], similarity=d.get("similarity"),
        guard_flags=d.get("guard_flags"), decided_by=d.get("decided_by"),
        decided_at=str(d["decided_at"]) if d.get("decided_at") else None,
        only_in_a=only_a or [], only_in_b=only_b or [],
        text_a=d.get("text_a"), text_b=d.get("text_b"),
    )


def _require_bank():
    if state["rows"] is None or state["index"] is None:
        raise HTTPException(503, "Still loading the model - try again shortly.")


@app.get("/duplicates", response_model=DuplicateSweepOut,
        dependencies=[Depends(require_api_key), Depends(EMPLOYEE)])
def find_duplicates(threshold: float = 0.95, use_guards: bool = True,
                    show_reviewed: bool = False, limit: int = 50):
    """The whole-bank sweep - app.py's "Find duplicates" page. Both
    with-guards and without-guards counts come back regardless of
    `use_guards`, the same "safety checks removed N pairs" comparison
    the page itself shows.
    """
    _require_bank()
    with_guards = dedupe.find_pairs(state["index"], use_guards=True,
                                    threshold=threshold)
    without_guards = dedupe.find_pairs(state["index"], use_guards=False,
                                       threshold=threshold)
    results = with_guards if use_guards else without_guards

    with store.connect() as conn:
        decided = store.current_decisions(conn)

    by_id = {r["id"]: r for r in state["rows"]}
    pending = []
    for p in results:
        key = store.pair_key(p["a"], p["b"])
        if key in decided:
            if not show_reviewed:
                continue
            p = {**p, "decision": decided[key]}
        pending.append(p)

    pairs_out = [
        DuplicatePairOut(
            a=_question_brief(by_id[p["a"]]), b=_question_brief(by_id[p["b"]]),
            score=p["score"], raw_score=p["raw_score"], penalty=p["penalty"],
            verdict=p["verdict"], reasons=p["reasons"],
            decision=_decision_out(p["decision"]) if p.get("decision") else None,
        )
        for p in pending[:limit]
    ]
    return DuplicateSweepOut(
        threshold=threshold, use_guards=use_guards,
        pairs_with_guards=len(with_guards), pairs_without_guards=len(without_guards),
        pending=len(pending), pairs=pairs_out,
    )


@app.get("/within-paper-duplicates", response_model=WithinPaperSweepOut,
        dependencies=[Depends(require_api_key), Depends(EMPLOYEE)])
def within_paper_duplicates(
    grade: Optional[str] = None, subject: Optional[str] = None,
    title_contains: Optional[str] = None, assessment_id: Optional[str] = None,
    use_guards: bool = True, threshold: float = 0.95,
    show_reviewed: bool = False, limit: int = 50,
):
    """The same question appearing twice on one paper - app.py's
    "Duplicates within a paper" page. Filters narrow WHICH questions are
    compared (a row that fails a filter is dropped from its paper's
    group before find_pairs ever sees it), not just which results are
    shown - a group left with one question after filtering is correctly
    skipped, same as app.py's own _matches().
    """
    if state["assessment_groups"] is None:
        raise HTTPException(503, "Still loading the model - try again shortly.")

    def matches(r):
        if grade and r.get("grade") != grade:
            return False
        if subject and r.get("subject") != subject:
            return False
        if title_contains and title_contains.lower() not in (r.get("title") or "").lower():
            return False
        return True

    groups = {}
    for aid, rs in state["assessment_groups"].items():
        if assessment_id is not None and str(aid) != assessment_id:
            continue
        kept = [r for r in rs if matches(r)]
        if len(kept) > 1:
            groups[aid] = kept

    results = dedupe.find_within_assessment_duplicates(
        groups, use_guards=use_guards, threshold=threshold)
    papers_hit = {p["assessment_id"] for p in results}
    passage_groups = {aid: dedupe.find_shared_passage_groups(groups[aid])
                      for aid in papers_hit}

    def shared_with(aid, qid):
        for g in passage_groups.get(aid, []):
            if qid in g["question_ids"]:
                return list(g["question_numbers"])
        return []

    with store.connect() as conn:
        decided = store.current_decisions(conn)

    pending = []
    for p in results:
        key = store.pair_key(p["a"], p["b"])
        if key in decided:
            if not show_reviewed:
                continue
            p = {**p, "decision": decided[key]}
        pending.append(p)

    pairs_out = []
    for p in pending[:limit]:
        by_id = {r["id"]: r for r in groups[p["assessment_id"]]}
        pairs_out.append(DuplicatePairOut(
            a=_question_brief(by_id[p["a"]]), b=_question_brief(by_id[p["b"]]),
            score=p["score"], raw_score=p["raw_score"], penalty=p["penalty"],
            verdict=p["verdict"], reasons=p["reasons"],
            assessment_id=p["assessment_id"],
            shared_passage_with=shared_with(p["assessment_id"], p["a"]),
            decision=_decision_out(p["decision"]) if p.get("decision") else None,
        ))

    return WithinPaperSweepOut(
        threshold=threshold, use_guards=use_guards,
        papers_checked=len(groups), repeats_found=len(results),
        pending=len(pending), pairs=pairs_out,
    )


@app.get("/assessments", dependencies=[Depends(require_api_key), Depends(EMPLOYEE)])
def list_assessment_ids() -> List[str]:
    """Every assessment with more than one comparable question - fills
    the "Assessment #" filter dropdown on the within-paper duplicate
    check, same sort as app.py's own assessment_options."""
    if state["assessment_groups"] is None:
        raise HTTPException(503, "Still loading the model - try again shortly.")
    return sorted(
        (str(a) for a in state["assessment_groups"]),
        key=lambda x: (0, int(x)) if x.isdigit() else (1, x),
    )


@app.get("/decisions", response_model=List[DecisionOut],
        dependencies=[Depends(require_api_key), Depends(EMPLOYEE)])
def list_decisions(verdict: Optional[Literal["duplicate", "distinct"]] = None):
    """Every current verdict, newest first, with both questions' text -
    app.py's "Reviewed decisions" page."""
    with store.connect() as conn:
        decisions = store.list_decisions(conn, verdict=verdict)
    return [_decision_out(d) for d in decisions]


@app.get("/decisions/schema", dependencies=[Depends(require_api_key), Depends(EMPLOYEE)])
def decisions_schema() -> Dict[str, Optional[str]]:
    """The decisions table's real CREATE TABLE, migrated columns
    included - the "How this table is built" expander."""
    with store.connect() as conn:
        return {"schema": store.table_schema(conn, "decisions")}


@app.post("/decisions", response_model=DecisionOut,
         dependencies=[Depends(require_api_key), Depends(EMPLOYEE)])
def create_decision(body: DecisionIn, identity: str = Depends(current_identity)):
    """Confirm duplicate / Not a duplicate. "Skip" has no route - it
    saves nothing even in app.py, it only hides a pair for the current
    session, which is purely a frontend concern.

    When the verdict is "distinct", the contrasting vocabulary between
    the two questions is captured automatically (guards.
    candidate_contrast_terms) - nothing reads it back automatically
    (see CLAUDE.md, "Known limitations"), but it is there for a person
    to later promote into the guard layer's contrast list by hand.
    `decided_by` always comes from the caller's own identity header,
    never the request body.
    """
    contrast = None
    if body.verdict == "distinct":
        by_id = {r["id"]: r for r in state["rows"]} if state["rows"] else {}
        qa, qb = by_id.get(body.a), by_id.get(body.b)
        if qa is None or qb is None:
            with store.connect() as conn:
                qa = qa or store.get(conn, body.a)
                qb = qb or store.get(conn, body.b)
        if qa is not None and qb is not None:
            contrast = guards.candidate_contrast_terms(qa["text"], qb["text"])

    with store.connect() as conn:
        decision_id = store.record_decision(
            conn, body.a, body.b, body.verdict, identity,
            body.similarity, body.guard_flags,
            candidate_contrast_terms=contrast,
        )
        d = store.get_decision(conn, decision_id)
    out = _decision_out(d)
    if contrast:
        out.only_in_a, out.only_in_b = contrast
    return out


@app.delete("/decisions", status_code=204, dependencies=[Depends(require_api_key), Depends(EMPLOYEE)])
def undo_decision(a: str, b: str):
    """Send a pair back for review - deletes every verdict on it, in
    either order, same as app.py's "Undo" button."""
    with store.connect() as conn:
        store.undo_decision(conn, a, b)


@app.get("/search", response_model=List[SearchHitOut],
        dependencies=[Depends(require_api_key), Depends(EMPLOYEE)])
def search_by_meaning(q: str, top_k: int = 5):
    """Finds questions by what they ask about, not by matching words -
    app.py's "Search by meaning" page. Embedded as a `query:`, not a
    `passage:` - a short query meeting the bank's longer passage
    vectors, the opposite of check_one's own reasoning (see
    CLAUDE.md, "The one-question check")."""
    _require_bank()
    if state["embedder"] is None:
        raise HTTPException(503, "Still loading the model - try again shortly.")
    if not q.strip():
        raise HTTPException(400, "Query text is required.")
    qvec = state["embedder"].encode(q, is_query=True)[0]
    hits = dedupe.search(qvec, state["index"], top_k=top_k)
    return [SearchHitOut(score=h["score"], question=_question_brief(h["question"]))
           for h in hits]


@app.get("/questions", response_model=List[QuestionBrief],
        dependencies=[Depends(require_api_key), Depends(EMPLOYEE)])
def list_bank_questions(grade: Optional[str] = None, subject: Optional[str] = None,
                        limit: int = 200, offset: int = 0):
    """Browse the bank - app.py's "Question bank" page. Comparable,
    fingerprinted rows only, same set every other page compares against
    - not every row EdSider has (see store.COMPARABLE)."""
    _require_bank()
    rows = state["rows"]
    if grade:
        rows = [r for r in rows if r.get("grade") == grade]
    if subject:
        rows = [r for r in rows if r.get("subject") == subject]
    return [_question_brief(r) for r in rows[offset:offset + limit]]


@app.get("/questions/facets", dependencies=[Depends(require_api_key), Depends(EMPLOYEE)])
def question_facets() -> Dict[str, List[str]]:
    """Distinct grades and subjects in the comparable bank - fills the
    Question bank page's filter dropdowns without shipping every row to
    compute them client-side."""
    _require_bank()
    return {
        "grades": sorted({r["grade"] for r in state["rows"] if r.get("grade")}),
        "subjects": sorted({r["subject"] for r in state["rows"] if r.get("subject")}),
    }


# ------------------------------------------------------------ the page
# Registered last, so every API route above is matched first.

@app.get("/{full_path:path}", include_in_schema=False)
def frontend(full_path: str):
    """The built React app (see FRONTEND_DIR). A real file under dist is
    sent as it is; one of the app's own page addresses gets index.html,
    and the app's router takes it from there. Anything else is a plain
    404 - not the app's shell, so a mistyped API path still says so."""
    if not _serving_frontend():
        raise HTTPException(404, "Not Found")
    path = "/" + full_path
    candidate = (FRONTEND_DIR / full_path).resolve()
    # parents check: "../" in a path must never reach outside dist.
    if full_path and candidate.is_file() and FRONTEND_DIR in candidate.parents:
        return FileResponse(candidate)
    if _is_page_route(path):
        # Never cached: it names the current build's hashed assets.
        return FileResponse(FRONTEND_DIR / "index.html",
                            headers={"Cache-Control": "no-cache"})
    raise HTTPException(404, "Not Found")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
