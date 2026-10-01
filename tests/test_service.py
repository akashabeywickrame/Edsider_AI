"""Tests for the live service.

Route functions are called directly rather than through a running
server or FastAPI's TestClient - that would trigger the lifespan hook
and load the real ~500MB model, which is slow and exactly what the
existing test suite avoids everywhere else (test_similarity.py,
test_guards.py, test_check_new.py all use hand-made vectors instead of
the real embedder). A fake embedder does the same job here: it proves
the request/response wiring and the pass-1/pass-2/pass-3 sequencing,
independent of whether the model is downloaded.
"""

import sys
from pathlib import Path

import json

import numpy as np
import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import dedupe, store
from src.normalize import content_hash
import service
from service import CheckRequest, check, health, reload_index


def unit(v):
    v = np.array(v, dtype=float)
    return v / np.linalg.norm(v)


class FakeEmbedder:
    """Returns whatever vector was registered for a given text, so a
    test can control the "meaning" of each question exactly."""

    def __init__(self, vectors):
        self.vectors = vectors

    def encode(self, texts, is_query=False):
        if isinstance(texts, str):
            texts = [texts]
        return [self.vectors[t] for t in texts]


BANK = [
    {"id": "Q001", "text": "Calculate the area of a rectangle 12 cm by 5 cm",
     "grade": "8", "subject": "Mathematics", "vector": list(unit([1.0, 0.0, 0.0]))},
    {"id": "Q002", "text": "Name the largest planet in the solar system",
     "grade": "8", "subject": "Science", "vector": list(unit([0.0, 0.0, 1.0]))},
]


def test_no_api_key_configured_leaves_the_service_open(monkeypatch):
    """A laptop or the test suite sets no key - nothing changes there."""
    monkeypatch.delenv("EDSIDER_API_KEY", raising=False)
    service.require_api_key(None)


def test_a_configured_api_key_must_be_sent_and_match(monkeypatch):
    monkeypatch.setenv("EDSIDER_API_KEY", "s3cret-key")
    for sent in (None, "", "wrong-key", "s3cret-key-and-more"):
        with pytest.raises(HTTPException) as err:
            service.require_api_key(sent)
        assert err.value.status_code == 401
    service.require_api_key("s3cret-key")


def test_every_data_route_needs_the_key_except_health_and_docs():
    """The host's uptime probe cannot send a header, so /health stays
    open, and so does FastAPI's own generated documentation - but every
    route that reads or writes the database (the duplicate check, and
    the whole paper-intake/teacher/admin API) must require it once a
    key is configured. Written as "everything except this short list",
    not an enumeration of every route, so a newly added route defaults
    to gated and this test catches the day someone forgets the
    dependency rather than needing to be remembered itself."""
    ungated_allowed = {"/health", "/docs", "/docs/oauth2-redirect",
                      "/redoc", "/openapi.json"}
    ungated = set()
    for route in service.app.routes:
        path = getattr(route, "path", None)
        if path is None or path in ungated_allowed:
            continue
        dependant = getattr(route, "dependant", None)
        if not (dependant and any(d.call is service.require_api_key
                                  for d in dependant.dependencies)):
            ungated.add(path)
    assert not ungated


@pytest.fixture
def sample_db(local_db):
    """The shared throwaway schema (see conftest.py), seeded - so these
    tests never touch the project's real bank."""
    with store.connect() as conn:
        for row in BANK:
            store.save(conn, {
                "id": row["id"], "text": row["text"], "grade": row["grade"],
                "subject": row["subject"], "content_hash": content_hash(row["text"]),
            })
            store.save_embedding(conn, content_hash(row["text"]),
                                 service.DEFAULT_MODEL, row["vector"])
    yield


@pytest.fixture
def loaded(sample_db):
    """service.state populated the way load_state() would, but with a
    fake embedder so no real model is touched."""
    with store.connect() as conn:
        rows = [dict(r) for r in
                store.comparable_questions(conn, service.DEFAULT_MODEL)]
    # store hands back a list of floats now, whichever way the database
    # stores them (pgvector or bytea) - no JSON to parse any more.
    vectors = {r["text"]: np.asarray(r["vector"]) for r in rows}
    service.state["embedder"] = FakeEmbedder(vectors)
    service.state["rows"] = rows
    service.state["index"] = dedupe.VectorIndex(rows)
    yield
    service.state["embedder"] = None
    service.state["rows"] = None
    service.state["index"] = None


# ---------------------------------------------------------------- health

def test_health_before_startup_reports_loading():
    service.state["embedder"] = None
    service.state["rows"] = None
    body = health()
    assert body["status"] == "loading"
    assert body["comparable_questions"] == 0


def test_health_after_load_reports_the_bank_size(loaded):
    body = health()
    assert body["status"] == "ok"
    assert body["comparable_questions"] == len(BANK)
    assert body["database"] == store.display_name()


# ------------------------------------------------------------- pass 1

def test_exact_text_is_caught_before_any_embedding(loaded):
    """The embedder must never be called for an exact match - if it
    were, this would raise, since this poisoned embedder always does."""
    class PoisonedEmbedder:
        def encode(self, *a, **k):
            raise AssertionError("embedder was called - Pass 1 should have short-circuited")
    service.state["embedder"] = PoisonedEmbedder()
    req = CheckRequest(text="Calculate the area of a rectangle 12 cm by 5 cm")
    resp = check(req)
    assert resp.verdict == "EXACT DUPLICATE"
    assert resp.exact_duplicate_of == ["Q001"]
    assert resp.matches == []


def test_rechecking_a_question_by_its_own_id_excludes_itself(loaded):
    """Re-submitting Q001's own text under its own id must not report
    Q001 as a duplicate of itself."""
    req = CheckRequest(text="Calculate the area of a rectangle 12 cm by 5 cm",
                       id="Q001")
    resp = check(req)
    assert resp.verdict != "EXACT DUPLICATE"


# --------------------------------------------------------- passes 2 + 3

def test_reworded_question_is_flagged_by_meaning(loaded):
    vec = list(unit([0.99, 0.05, 0.0]))
    service.state["embedder"].vectors["Work out the area, 12 by 5 cm"] = np.asarray(vec)
    req = CheckRequest(text="Work out the area, 12 by 5 cm", threshold=0.8)
    resp = check(req)
    assert resp.verdict != "EXACT DUPLICATE"
    assert resp.matches
    assert resp.matches[0].id == "Q001"


def test_unrelated_question_is_distinct(loaded):
    vec = list(unit([0.0, 1.0, 0.0]))
    service.state["embedder"].vectors["What is the capital of France?"] = np.asarray(vec)
    req = CheckRequest(text="What is the capital of France?", threshold=0.8)
    resp = check(req)
    assert resp.verdict == "DISTINCT"


def test_response_reports_elapsed_time(loaded):
    resp = check(CheckRequest(text="Calculate the area of a rectangle 12 cm by 5 cm"))
    assert resp.elapsed_ms >= 0


def test_check_before_startup_returns_503():
    service.state["embedder"] = None
    service.state["index"] = None
    with pytest.raises(HTTPException) as exc:
        check(CheckRequest(text="anything"))
    assert exc.value.status_code == 503


def test_blank_text_is_rejected(loaded):
    with pytest.raises(HTTPException) as exc:
        check(CheckRequest(text="   "))
    assert exc.value.status_code == 400


# -------------------------------------------------------------- request

def test_empty_text_is_rejected_by_the_schema():
    """min_length=1 is enforced by pydantic before the route even runs."""
    with pytest.raises(Exception):
        CheckRequest(text="")


def test_threshold_out_of_range_is_rejected():
    with pytest.raises(Exception):
        CheckRequest(text="x", threshold=1.5)


# ------------------------------------------------------------------ reload

def test_reload_picks_up_a_newly_saved_question(loaded):
    from src.normalize import content_hash as ch
    new_text = "State two uses of a magnet"
    with store.connect() as conn:
        store.save(conn, {"id": "Q003", "text": new_text,
                          "content_hash": ch(new_text)})
        store.save_embedding(conn, ch(new_text), service.DEFAULT_MODEL,
                             list(unit([0.5, 0.5, 0.5])))

    service.state["embedder"] = FakeEmbedder({**service.state["embedder"].vectors})
    body = reload_index()
    assert body["comparable_questions"] == len(BANK) + 1
    assert body["added"] == 1
