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
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src import store, dedupe
from src.dedupe.embeddings import Embedder, DEFAULT_MODEL
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


@app.get("/health")
def health():
    """Is it up, and what is it actually serving against.

    Reachable before startup finishes loading, and even before EDSIDER_DB
    was set at all - store.DB_PATH is None until then, not a crash.
    """
    return {
        "status": "ok" if state["embedder"] is not None else "loading",
        "database": store.DB_PATH.name if store.DB_PATH else None,
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
