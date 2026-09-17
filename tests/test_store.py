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


def seed(conn, qid, text, language):
    store.save(conn, {"id": qid, "text": text, "language": language,
                      "content_hash": content_hash(text)})


# ------------------------------------------ no database configured yet

def test_connect_refuses_to_run_with_no_database_configured():
    """There is no sample bank to fall back to any more - silently
    creating a brand new empty database at a guessed path would be
    worse than the old fallback, not safer, so connect() must refuse."""
    original, schema = store.DSN, store.SCHEMA_NAME
    try:
        store.use_database(None)
        with pytest.raises(RuntimeError, match="EDSIDER_DB"):
            store.connect()
    finally:
        store.use_database(original, schema=schema)


def test_use_database_none_resets_cleanly(local_db):
    """A caller that saves DSN before switching, then restores it,
    must be able to pass None straight back through - the state DSN
    starts in when EDSIDER_DB was never set."""
    assert store.use_database(None) is None
    assert store.DSN is None


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


def test_record_decision_leaves_contrast_terms_null_when_both_lists_are_empty(local_db):
    """Two identical texts give ([], []) - a non-empty tuple, but no
    words were captured, so nothing should be stored."""
    with store.connect() as conn:
        store.record_decision(conn, "Q1", "Q2", "distinct", "reviewer",
                              candidate_contrast_terms=([], []))
        row = conn.execute(
            "SELECT candidate_contrast_terms FROM decisions").fetchone()
    assert row["candidate_contrast_terms"] is None


def test_current_decisions_keeps_the_latest_verdict_per_pair_in_either_order(local_db):
    with store.connect() as conn:
        store.record_decision(conn, "Q1", "Q2", "duplicate", "reviewer")
        store.record_decision(conn, "Q2", "Q1", "distinct", "reviewer")
        store.record_decision(conn, "Q3", "Q4", "skipped", "reviewer")
        decided = store.current_decisions(conn)
    assert set(decided) == {("Q1", "Q2")}
    assert decided[store.pair_key("Q2", "Q1")]["verdict"] == "distinct"


def test_list_decisions_carries_question_text_and_contrast_words(local_db):
    with store.connect() as conn:
        seed(conn, "Q1", "Calculate the area of a rectangle", "English")
        seed(conn, "Q2", "Calculate the perimeter of a rectangle", "English")
        seed(conn, "Q3", "Name a planet", "English")
        store.record_decision(conn, "Q1", "Q2", "distinct", "reviewer", 0.97,
                              candidate_contrast_terms=(["area"], ["perimeter"]))
        store.record_decision(conn, "Q1", "Q3", "duplicate", "reviewer", 0.96)
        everything = store.list_decisions(conn)
        distinct = store.list_decisions(conn, verdict="distinct")
    assert [d["question_b"] for d in everything] == ["Q3", "Q2"]  # newest first
    assert len(distinct) == 1
    d = distinct[0]
    assert d["text_a"] == "Calculate the area of a rectangle"
    assert d["text_b"] == "Calculate the perimeter of a rectangle"
    assert (d["only_in_a"], d["only_in_b"]) == (["area"], ["perimeter"])


def test_undo_decision_sends_the_pair_back_for_review(local_db):
    with store.connect() as conn:
        store.record_decision(conn, "Q1", "Q2", "duplicate", "reviewer")
        store.record_decision(conn, "Q2", "Q1", "distinct", "reviewer")
        store.record_decision(conn, "Q3", "Q4", "duplicate", "reviewer")
        removed = store.undo_decision(conn, "Q2", "Q1")
        decided = store.current_decisions(conn)
    assert removed == 2
    assert set(decided) == {("Q3", "Q4")}


def test_table_schema_shows_migrated_columns(local_db):
    with store.connect() as conn:
        sql = store.table_schema(conn, "decisions")
        missing = store.table_schema(conn, "no_such_table")
    assert sql.startswith("CREATE TABLE decisions")
    assert "candidate_contrast_terms" in sql
    assert missing is None


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


# ------------------------------------------- pool, cache and batch reads
#
# Written after the move to a remote server, where opening a connection
# costs seconds and fingerprints download at about 1 MB/s. Each of these
# proves a speed-up did not change what comes back.

def _pid(conn):
    return conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]


def test_a_returned_connection_is_reused(local_db):
    with store.connect() as conn:
        first = _pid(conn)
    with store.connect() as conn:
        assert _pid(conn) == first


def test_a_pooled_connection_the_server_dropped_is_replaced(local_db):
    """A connection can sit in the pool looking open after the server has
    closed it. The next caller must get a working one, not an error."""
    import psycopg2

    with store.connect() as conn:
        dead = _pid(conn)
    killer = psycopg2.connect(store.DSN)
    killer.autocommit = True
    try:
        killer.cursor().execute("SELECT pg_terminate_backend(%s)", (dead,))
    finally:
        killer.close()

    with store.connect() as conn:
        assert conn.execute("SELECT 1 AS one").fetchone()["one"] == 1
        assert _pid(conn) != dead


def test_each_pooled_connection_uses_the_configured_schema(local_db):
    with store.connect() as conn:
        assert conn.execute(
            "SELECT current_schema() AS s").fetchone()["s"] == store.SCHEMA_NAME


def _seed_embedded(conn, qid, text, vector):
    seed(conn, qid, text, "English")
    store.save_embedding(conn, content_hash(text), "fake-model", vector)


def test_fingerprints_are_served_from_the_disk_cache(local_db, tmp_path):
    """Loaded once, a fingerprint comes from local disk after that - even
    in a new process with nothing in memory."""
    store.CACHE_DIR = tmp_path
    text = "Describe the function of the mitochondria."
    with store.connect() as conn:
        _seed_embedded(conn, "C1", text, [0.25, 0.5, 0.75])
        first = store.comparable_questions(conn, "fake-model")
    assert list((tmp_path).glob("vectors-*.npz"))

    # Change the stored numbers behind the cache's back, then start as a
    # new process would - memory empty, disk file present.
    with store.connect() as conn:
        conn.execute("UPDATE embeddings SET vector = ? WHERE content_hash = ?",
                     (store.encode_vector([9.0, 9.0, 9.0]), content_hash(text)))
    store.forget_cached_vectors()
    with store.connect() as conn:
        again = store.comparable_questions(conn, "fake-model")

    assert list(first[0]["vector"]) == [0.25, 0.5, 0.75]
    assert list(again[0]["vector"]) == [0.25, 0.5, 0.75]


def test_re_embedding_a_text_replaces_its_cached_fingerprint(local_db, tmp_path):
    store.CACHE_DIR = tmp_path
    text = "Name the largest planet in the solar system."
    with store.connect() as conn:
        _seed_embedded(conn, "C2", text, [1.0, 0.0])
        store.comparable_questions(conn, "fake-model")
        store.save_embedding(conn, content_hash(text), "fake-model", [0.0, 1.0])
        after = store.comparable_questions(conn, "fake-model")
    store.forget_cached_vectors()
    with store.connect() as conn:
        from_disk = store.comparable_questions(conn, "fake-model")

    assert list(after[0]["vector"]) == [0.0, 1.0]
    assert list(from_disk[0]["vector"]) == [0.0, 1.0]


def test_all_papers_in_one_query_match_one_paper_at_a_time(local_db):
    with store.connect() as conn:
        for i, text in enumerate(["What is photosynthesis?",
                                  "Define respiration in plants.",
                                  "What is a food chain?"]):
            _seed_embedded(conn, f"P{i}", text, [float(i), 1.0])
        store.save_assessment_link(conn, 1, "A1", "P0", question_number=2)
        store.save_assessment_link(conn, 2, "A1", "P1", question_number=1)
        store.save_assessment_link(conn, 3, "A2", "P1", question_number=1)
        store.save_assessment_link(conn, 4, "A2", "P2", question_number=2)

        together = store.questions_in_assessments(conn, "fake-model")
        one_by_one = {a: store.questions_in_assessment(conn, a, "fake-model")
                      for a in ("A1", "A2")}

    for paper in ("A1", "A2"):
        assert ([(r["id"], r["question_number"]) for r in together[paper]]
                == [(r["id"], r["question_number"]) for r in one_by_one[paper]])
        assert all("assessment_id" not in r for r in together[paper])
    assert [r["id"] for r in together["A1"]] == ["P1", "P0"]  # question order


def test_assessment_links_cover_every_question_at_once(local_db):
    with store.connect() as conn:
        store.save_assessment_link(conn, 1, "A1", "Q1", question_number=3)
        store.save_assessment_link(conn, 2, "A2", "Q1", question_number=7)
        store.save_assessment_link(conn, 3, "A2", "Q2", question_number=1)
        links = store.assessment_links(conn)
        single = [dict(r) for r in store.assessments_for_question(conn, "Q1")]

    assert links["Q1"] == single
    assert links["Q2"] == [{"assessment_id": "A2", "question_number": 1}]
