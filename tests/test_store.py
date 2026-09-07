"""Tests for the language-scoped embedding query.

Added when the real bank turned out to be 60%+ Sinhala/Tamil and the
decision was made to embed English first, defer the rest - this is the
one piece of new logic that split relies on, so it gets a direct test
rather than only being exercised indirectly through embed.py.
"""

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
