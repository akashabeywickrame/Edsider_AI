"""Tests for the "rest" of app.py's own pages, now wired up as API
routes in service.py: whole-bank duplicates, within-paper duplicates,
reviewed decisions, search by meaning, and the question bank browser.

Same pattern as test_service.py: route functions are called directly
(no running server, no real model) and a FakeEmbedder stands in for
the real one. `loaded` populates service.state the way load_state()
would for the pre-existing /check tests; `with_assessment_groups`
below sets state["assessment_groups"] directly rather than seeding
real question_assessments rows, because the within-paper routes only
ever read that in-memory structure - proving the route logic does not
need a real sync to have happened first.
"""

import sys
from pathlib import Path

import numpy as np
import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import dedupe, store
from src.normalize import content_hash
import service
from service import (
    DecisionIn, create_decision, decisions_schema, find_duplicates,
    list_assessment_ids, list_bank_questions, list_decisions,
    question_facets, search_by_meaning, undo_decision,
    within_paper_duplicates,
)


def unit(v):
    v = np.array(v, dtype=float)
    return v / np.linalg.norm(v)


class FakeEmbedder:
    def __init__(self, vectors):
        self.vectors = vectors

    def encode(self, texts, is_query=False):
        if isinstance(texts, str):
            texts = [texts]
        return [self.vectors[t] for t in texts]


# Q001/Q002: genuinely the same question, reworded - no numbers
# conflict, so guards leave this pair alone. Q004/Q005: the project's
# own running example (CLAUDE.md) - raw embeddings score them close,
# but "area" vs "perimeter" is a real CONTRAST_GROUPS entry (guards.py),
# so the guard layer demotes this one. Q003 is orthogonal to everything
# - never pairs with anything.
BANK = [
    {"id": "Q001", "text": "Calculate the area of a triangle with base 10 and height 4",
     "grade": "8", "subject": "Mathematics",
     "vector": list(unit([1.0, 0.0, 0.0, 0.0]))},
    {"id": "Q002", "text": "Determine the area of a triangle with base 10 and height 4",
     "grade": "8", "subject": "Mathematics",
     "vector": list(unit([0.99, 0.05, 0.0, 0.0]))},
    {"id": "Q003", "text": "Name the capital city of Sri Lanka",
     "grade": "8", "subject": "Social Studies",
     "vector": list(unit([0.0, 1.0, 0.0, 0.0]))},
    {"id": "Q004", "text": "Calculate the area of a rectangle 12 cm by 5 cm",
     "grade": "8", "subject": "Mathematics",
     "vector": list(unit([0.0, 0.0, 1.0, 0.0]))},
    {"id": "Q005", "text": "Calculate the perimeter of a rectangle 12 cm by 5 cm",
     "grade": "8", "subject": "Mathematics",
     "vector": list(unit([0.0, 0.0, 0.99, 0.05]))},
]


@pytest.fixture
def sample_db(local_db):
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
    with store.connect() as conn:
        rows = [dict(r) for r in
                store.comparable_questions(conn, service.DEFAULT_MODEL)]
    vectors = {r["text"]: np.asarray(r["vector"]) for r in rows}
    service.state["embedder"] = FakeEmbedder(vectors)
    service.state["rows"] = rows
    service.state["index"] = dedupe.VectorIndex(rows)
    yield
    service.state["embedder"] = None
    service.state["rows"] = None
    service.state["index"] = None
    service.state["assessment_groups"] = None


@pytest.fixture
def with_assessment_groups(loaded):
    """Q001 and Q002 both sitting on assessment #11 - set directly on
    state, since within_paper_duplicates() and list_assessment_ids()
    never re-query the database for grouping, only for decisions."""
    by_id = {r["id"]: r for r in service.state["rows"]}
    service.state["assessment_groups"] = {11: [by_id["Q001"], by_id["Q002"]]}
    yield


# ------------------------------------------------------- find duplicates

def test_find_duplicates_before_the_bank_loads_is_503():
    service.state["rows"] = None
    service.state["index"] = None
    with pytest.raises(HTTPException) as err:
        find_duplicates()
    assert err.value.status_code == 503


def test_find_duplicates_counts_pairs_with_and_without_guards(loaded):
    out = find_duplicates(threshold=0.90, use_guards=True)
    assert out.pairs_with_guards == 1
    assert out.pairs_without_guards == 2
    assert {p.a.id for p in out.pairs} | {p.b.id for p in out.pairs} == {"Q001", "Q002"}


def test_find_duplicates_without_guards_shows_the_demoted_pair_too(loaded):
    out = find_duplicates(threshold=0.90, use_guards=False)
    pairs = {frozenset((p.a.id, p.b.id)) for p in out.pairs}
    assert frozenset(("Q001", "Q002")) in pairs
    assert frozenset(("Q004", "Q005")) in pairs


def test_find_duplicates_hides_reviewed_pairs_unless_asked(loaded):
    create_decision(DecisionIn(a="Q001", b="Q002", verdict="duplicate"),
                    identity="reviewer")
    hidden = find_duplicates(threshold=0.90, use_guards=True)
    assert hidden.pending == 0
    assert hidden.pairs == []

    shown = find_duplicates(threshold=0.90, use_guards=True, show_reviewed=True)
    assert shown.pending == 1
    assert shown.pairs[0].decision is not None
    assert shown.pairs[0].decision.verdict == "duplicate"


# ------------------------------------------------------------- decisions

def test_create_decision_as_distinct_captures_contrast_terms(loaded):
    d = create_decision(
        DecisionIn(a="Q001", b="Q003", verdict="distinct", similarity=0.5),
        identity="N. Perera",
    )
    assert d.verdict == "distinct"
    assert d.decided_by == "N. Perera"
    assert d.only_in_a and d.only_in_b


def test_create_decision_as_duplicate_has_no_contrast_terms(loaded):
    d = create_decision(DecisionIn(a="Q001", b="Q002", verdict="duplicate"),
                        identity="reviewer")
    assert d.only_in_a == []
    assert d.only_in_b == []


def test_list_and_undo_a_decision(loaded):
    create_decision(DecisionIn(a="Q001", b="Q002", verdict="duplicate"),
                    identity="reviewer")
    listed = list_decisions()
    assert any(d.question_a == "Q001" and d.question_b == "Q002" for d in listed)
    assert listed[0].text_a and listed[0].text_b

    only_dup = list_decisions(verdict="duplicate")
    assert all(d.verdict == "duplicate" for d in only_dup)

    undo_decision(a="Q001", b="Q002")
    assert list_decisions() == []


def test_decisions_schema_describes_the_real_table(local_db):
    out = decisions_schema()
    assert out["schema"].startswith("CREATE TABLE decisions (")
    assert "question_a" in out["schema"]
    assert "verdict" in out["schema"]


# ---------------------------------------------------- within a paper

def test_within_paper_duplicates_finds_the_repeat(with_assessment_groups):
    out = within_paper_duplicates(threshold=0.90, use_guards=True)
    assert out.papers_checked == 1
    assert out.repeats_found == 1
    assert out.pairs[0].assessment_id == 11
    assert {out.pairs[0].a.id, out.pairs[0].b.id} == {"Q001", "Q002"}


def test_within_paper_duplicates_filters_by_subject(with_assessment_groups):
    out = within_paper_duplicates(subject="Social Studies")
    assert out.papers_checked == 0
    assert out.pairs == []


def test_within_paper_duplicates_filters_by_assessment_id(with_assessment_groups):
    out = within_paper_duplicates(assessment_id="999")
    assert out.papers_checked == 0


def test_list_assessment_ids(with_assessment_groups):
    assert list_assessment_ids() == ["11"]


def test_within_paper_before_the_bank_loads_is_503():
    service.state["assessment_groups"] = None
    with pytest.raises(HTTPException) as err:
        within_paper_duplicates()
    assert err.value.status_code == 503


# ------------------------------------------------------- search by meaning

def test_search_orders_hits_by_score(loaded):
    service.state["embedder"].vectors["about triangles"] = np.asarray(
        unit([1.0, 0.0, 0.0, 0.0]))
    hits = search_by_meaning(q="about triangles", top_k=3)
    assert [h.question.id for h in hits[:2]] == ["Q001", "Q002"]
    assert hits[0].score >= hits[1].score


def test_search_rejects_a_blank_query(loaded):
    with pytest.raises(HTTPException) as err:
        search_by_meaning(q="   ")
    assert err.value.status_code == 400


# --------------------------------------------------------- question bank

def test_list_bank_questions_filters_by_grade_and_subject(loaded):
    all_qs = list_bank_questions()
    assert len(all_qs) == len(BANK)

    maths_only = list_bank_questions(subject="Mathematics")
    assert {q.id for q in maths_only} == {"Q001", "Q002", "Q004", "Q005"}

    paged = list_bank_questions(limit=2, offset=0)
    assert len(paged) == 2


def test_question_facets_lists_distinct_values(loaded):
    facets = question_facets()
    assert facets["subjects"] == ["Mathematics", "Social Studies"]
    assert facets["grades"] == ["8"]
