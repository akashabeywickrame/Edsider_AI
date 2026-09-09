"""Tests for the language-scoped embedding query.

Added when the real bank turned out to be 60%+ Sinhala/Tamil and the
decision was made to embed English first, defer the rest - this is the
one piece of new logic that split relies on, so it gets a direct test
rather than only being exercised indirectly through embed.py.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import store
from src.normalize import content_hash


@pytest.fixture
def local_db(tmp_path):
    original = store.DB_PATH
    store.use_database(tmp_path / "store_test.db")
    yield
    store.use_database(original)


def seed(conn, qid, text, language):
    store.save(conn, {"id": qid, "text": text, "language": language,
                      "content_hash": content_hash(text)})


# ------------------------------------------ no database configured yet

def test_connect_refuses_to_run_with_no_database_configured():
    """There is no sample bank to fall back to any more - silently
    creating a brand new empty database at a guessed path would be
    worse than the old fallback, not safer, so connect() must refuse."""
    original = store.DB_PATH
    try:
        store.use_database(None)
        with pytest.raises(RuntimeError, match="EDSIDER_DB"):
            store.connect()
    finally:
        store.use_database(original)


def test_use_database_none_resets_cleanly(local_db):
    """A caller that saves DB_PATH before switching, then restores it,
    must be able to pass None straight back through - the state DB_PATH
    starts in when EDSIDER_DB was never set."""
    assert store.use_database(None) is None
    assert store.DB_PATH is None


def test_language_filter_only_returns_that_language(local_db):
    with store.connect() as conn:
        seed(conn, "E1", "Explain photosynthesis in plants.", "English")
        seed(conn, "S1", "සිංහල පාචා", "සිංහල")

        pending_en = store.questions_needing_embedding(
            conn, "fake-model", language="English")
        pending_all = store.questions_needing_embedding(conn, "fake-model")

    assert len(pending_en) == 1
    assert pending_en[0]["text"] == "Explain photosynthesis in plants."
    assert len(pending_all) == 2


def test_no_language_filter_returns_every_comparable_row(local_db):
    with store.connect() as conn:
        seed(conn, "E1", "One question here with real text.", "English")
        seed(conn, "T1", "Another question with different text.", "Tamil")

    with store.connect() as conn:
        pending = store.questions_needing_embedding(conn, "fake-model")
    assert {r["text"] for r in pending} == {
        "One question here with real text.",
        "Another question with different text.",
    }


def test_already_embedded_rows_are_not_returned_again(local_db):
    with store.connect() as conn:
        seed(conn, "E1", "A question that will get embedded.", "English")
        store.save_embedding(conn, content_hash("A question that will get embedded."),
                             "fake-model", [0.1, 0.2, 0.3])
        pending = store.questions_needing_embedding(
            conn, "fake-model", language="English")
    assert pending == []


# --------------------------------------------------------- decisions

def test_record_decision_stores_a_confirmed_duplicate(local_db):
    with store.connect() as conn:
        store.record_decision(conn, "Q1", "Q2", "duplicate", "reviewer",
                              0.97, "")
        row = conn.execute(
            "SELECT * FROM decisions WHERE question_a='Q1' AND question_b='Q2'"
        ).fetchone()
    assert row["verdict"] == "duplicate"
    assert row["similarity"] == 0.97
    assert row["candidate_contrast_terms"] is None


def test_record_decision_stores_candidate_contrast_terms_as_json(local_db):
    with store.connect() as conn:
        store.record_decision(
            conn, "Q1", "Q2", "distinct", "reviewer", 0.90, "",
            candidate_contrast_terms=(["area"], ["perimeter"]),
        )
        row = conn.execute(
            "SELECT candidate_contrast_terms FROM decisions "
            "WHERE question_a='Q1' AND question_b='Q2'"
        ).fetchone()
    assert json.loads(row["candidate_contrast_terms"]) == [["area"], ["perimeter"]]


def test_record_decision_leaves_contrast_terms_null_when_not_given(local_db):
    """A confirmed duplicate has nothing to contrast - the field should
    stay empty, not an empty pair of lists."""
    with store.connect() as conn:
        store.record_decision(conn, "Q1", "Q2", "duplicate", "reviewer")
        row = conn.execute(
            "SELECT candidate_contrast_terms FROM decisions "
            "WHERE question_a='Q1' AND question_b='Q2'"
        ).fetchone()
    assert row["candidate_contrast_terms"] is None


# ----------------------------------------- question <-> assessment links

def test_a_question_can_belong_to_more_than_one_assessment(local_db):
    """The whole reason this is a separate table rather than a column on
    questions - the junction table in EdSider's own schema is
    many-to-many, and the same question legitimately reused across two
    papers must not be forced into a single slot."""
    with store.connect() as conn:
        store.save_assessment_link(conn, 13, assessment_id=5, question_id="Q8")
        store.save_assessment_link(conn, 14, assessment_id=6, question_id="Q8")
        linked = store.assessments_for_question(conn, "Q8")
    assert {r["assessment_id"] for r in linked} == {"5", "6"}


def test_saving_the_same_link_id_twice_updates_rather_than_duplicates(local_db):
    with store.connect() as conn:
        store.save_assessment_link(conn, 13, assessment_id=5, question_id="Q8",
                                   question_number=1)
        store.save_assessment_link(conn, 13, assessment_id=5, question_id="Q8",
                                   question_number=2)
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM question_assessments"
        ).fetchone()["n"]
        row = conn.execute(
            "SELECT question_number FROM question_assessments WHERE link_id = 13"
        ).fetchone()
    assert n == 1
    assert row["question_number"] == 2


def test_questions_in_assessment_returns_only_that_papers_comparable_rows(local_db):
    with store.connect() as conn:
        seed(conn, "Q1", "Explain photosynthesis in plants.", "English")
        seed(conn, "Q2", "Name the largest ocean on Earth.", "English")
        store.save_embedding(conn, content_hash("Explain photosynthesis in plants."),
                             "fake-model", [0.1, 0.2, 0.3])
        store.save_embedding(conn, content_hash("Name the largest ocean on Earth."),
                             "fake-model", [0.4, 0.5, 0.6])
        store.save_assessment_link(conn, 1, assessment_id="9", question_id="Q1",
                                   question_number=1)
        # Q2 belongs to a different paper - must not show up under "9".
        store.save_assessment_link(conn, 2, assessment_id="10", question_id="Q2",
                                   question_number=1)

        in_paper_9 = store.questions_in_assessment(conn, "9", "fake-model")
    assert [r["id"] for r in in_paper_9] == ["Q1"]


def test_assessment_ids_lists_every_paper_with_a_link(local_db):
    with store.connect() as conn:
        store.save_assessment_link(conn, 1, assessment_id="9", question_id="Q1")
        store.save_assessment_link(conn, 2, assessment_id="10", question_id="Q2")
        ids = store.assessment_ids(conn)
    assert set(ids) == {"9", "10"}


# ------------------------------------------- linking children to parents

def test_link_children_to_parents_sets_parent_id(local_db):
    with store.connect() as conn:
        store.save(conn, {"id": "842", "text": "Grass -> Frog -> Snake.",
                          "content_hash": content_hash("Grass -> Frog -> Snake."),
                          "shape": "parent", "children_raw": "837,838"})
        store.save(conn, {"id": "837", "text": "How many links?",
                          "content_hash": content_hash("How many links?"),
                          "shape": "sub-question"})
        store.save(conn, {"id": "838", "text": "Name the top predator.",
                          "content_hash": content_hash("Name the top predator."),
                          "shape": "sub-question"})

        linked = store.link_children_to_parents(conn)
        assert linked == 2
        assert store.get(conn, "837")["parent_id"] == "842"
        assert store.get(conn, "838")["parent_id"] == "842"


def test_linking_works_regardless_of_arrival_order(local_db):
    """A child can be synced before its parent - a later run of
    link_children_to_parents() must still catch it up."""
    with store.connect() as conn:
        store.save(conn, {"id": "837", "text": "How many links?",
                          "content_hash": content_hash("How many links?"),
                          "shape": "sub-question"})
        store.link_children_to_parents(conn)
        assert store.get(conn, "837")["parent_id"] is None

        store.save(conn, {"id": "842", "text": "Grass -> Frog -> Snake.",
                          "content_hash": content_hash("Grass -> Frog -> Snake."),
                          "shape": "parent", "children_raw": "837"})
        store.link_children_to_parents(conn)
        assert store.get(conn, "837")["parent_id"] == "842"


def test_resaving_a_child_does_not_wipe_its_parent_link(local_db):
    """save() is an upsert - a re-synced child must not lose a link a
    previous run already established."""
    with store.connect() as conn:
        store.save(conn, {"id": "842", "text": "Grass -> Frog -> Snake.",
                          "content_hash": content_hash("Grass -> Frog -> Snake."),
                          "shape": "parent", "children_raw": "837"})
        store.save(conn, {"id": "837", "text": "How many links?",
                          "content_hash": content_hash("How many links?"),
                          "shape": "sub-question"})
        store.link_children_to_parents(conn)
        assert store.get(conn, "837")["parent_id"] == "842"

        # Re-sync the same child - a plain INSERT OR REPLACE would have
        # wiped parent_id here.
        store.save(conn, {"id": "837", "text": "How many links?",
                          "content_hash": content_hash("How many links?"),
                          "shape": "sub-question"})
        assert store.get(conn, "837")["parent_id"] == "842"


def test_children_needing_embedding_only_returns_linked_children(local_db):
    with store.connect() as conn:
        store.save(conn, {"id": "842", "text": "Grass -> Frog -> Snake.",
                          "content_hash": content_hash("Grass -> Frog -> Snake."),
                          "shape": "parent", "children_raw": "837"})
        # Linked.
        store.save(conn, {"id": "837", "text": "How many links?",
                          "content_hash": content_hash("How many links?"),
                          "shape": "sub-question"})
        # Not linked to anything.
        store.save(conn, {"id": "999", "text": "Orphan sub-question here.",
                          "content_hash": content_hash("Orphan sub-question here."),
                          "shape": "sub-question"})
        store.link_children_to_parents(conn)

        pending = store.children_needing_embedding(conn, "fake-model")
    assert [r["text"] for r in pending] == ["How many links?"]


def test_linked_children_with_embeddings_carries_parent_id(local_db):
    with store.connect() as conn:
        store.save(conn, {"id": "842", "text": "Grass -> Frog -> Snake.",
                          "content_hash": content_hash("Grass -> Frog -> Snake."),
                          "shape": "parent", "children_raw": "837"})
        store.save(conn, {"id": "837", "text": "How many links?",
                          "content_hash": content_hash("How many links?"),
                          "shape": "sub-question"})
        store.link_children_to_parents(conn)
        store.save_embedding(conn, content_hash("How many links?"),
                             "fake-model", [0.1, 0.2, 0.3])

        rows = store.linked_children_with_embeddings(conn, "fake-model")
    assert len(rows) == 1
    assert rows[0]["id"] == "837"
    assert rows[0]["parent_id"] == "842"


def test_embedding_english_leaves_other_languages_uncomparable(local_db):
    """The point of the split: comparable_questions() joins on
    embeddings, so an un-embedded language is invisible to a duplicate
    check without any separate filtering logic needed anywhere else."""
    with store.connect() as conn:
        seed(conn, "E1", "An English question with real content.", "English")
        seed(conn, "S1", "A Sinhala question, not yet embedded.", "Sinhala")
        store.save_embedding(conn, content_hash("An English question with real content."),
                             "fake-model", [0.1, 0.2, 0.3])

        comparable = store.comparable_questions(conn, "fake-model")
    ids = {r["id"] for r in comparable}
    assert ids == {"E1"}
