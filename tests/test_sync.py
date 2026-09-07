"""Tests for scripts/sync.py.

No real Postgres is available here, so psycopg2 is faked at the
connection/cursor level - the same reasoning as everywhere else in this
suite (test_similarity.py, test_check_new.py, test_service.py): prove
the logic - watermark handling, read-only session, upsert safety,
crash safety - independent of whether a real server is reachable.
"""

import sys
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import store
from scripts import sync


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.itersize = None
        self.executed_with = None
        self.closed = False

    def execute(self, query, params):
        self.executed_with = (query, params)

    def __iter__(self):
        return iter(self._rows)

    def close(self):
        self.closed = True


class FakePgConn:
    """Stands in for a psycopg2 connection. Records everything a test
    needs to assert on: was the session opened read-only, was a named
    (server-side, streaming) cursor used, was rollback (not commit)
    called on the way out."""

    def __init__(self, rows):
        self.rows = rows
        self.cursor_names = []
        self.session_calls = []
        self.rolled_back = False
        self.committed = False
        self.closed = False
        self._cursor = FakeCursor(rows)

    def cursor(self, name=None, cursor_factory=None):
        self.cursor_names.append(name)
        return self._cursor

    def set_session(self, readonly=None, autocommit=None):
        self.session_calls.append({"readonly": readonly, "autocommit": autocommit})

    def rollback(self):
        self.rolled_back = True

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def row(question_id, content, title="", grade_name=None, subject_name=None,
       is_subquestion=None, sub_questions=None):
    return {
        "question_id": question_id, "title": title, "content": content,
        "grade_name": grade_name, "subject_name": subject_name,
        "language_name": "English", "is_subquestion": is_subquestion,
        "sub_questions": sub_questions, "created_by": "1",
        "created_at": "2024-01-01",
    }


@pytest.fixture
def local_db(tmp_path):
    original = store.DB_PATH
    store.use_database(tmp_path / "sync_test.db")
    yield
    store.use_database(original)


def run_sync(monkeypatch, fake_pg, argv=()):
    # Patched at the psycopg2.connect level, not sync._pg_connect - so
    # _pg_connect()'s own logic (the read-only session, the required-env
    # check) actually runs and is what these tests are exercising.
    for var in ("PG_HOST", "PG_PORT", "PG_DB", "PG_USER", "PG_PASSWORD"):
        monkeypatch.setenv(var, "test")
    monkeypatch.setattr(psycopg2, "connect", lambda **kw: fake_pg)
    monkeypatch.setattr(sys, "argv", ["sync.py", *argv])
    sync.main()


# -------------------------------------------------------- configuration

def test_missing_credentials_fail_with_a_clear_message(monkeypatch):
    for var in ("PG_HOST", "PG_PORT", "PG_DB", "PG_USER", "PG_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    # Isolate from a real .env - if the developer running this suite has
    # one on disk, load_dotenv would refill the vars this test just
    # deleted and the "missing" case it is testing would never occur.
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    with pytest.raises(SystemExit, match="PG_HOST"):
        sync._pg_connect()


# ------------------------------------------------------------- the query

def test_query_uses_the_stored_watermark(monkeypatch, local_db):
    with store.connect() as conn:
        store.set_watermark(conn, sync.SOURCE, 500)

    fake_pg = FakePgConn([])
    run_sync(monkeypatch, fake_pg)

    _, params = fake_pg._cursor.executed_with
    assert params == {"watermark": 500}


def test_full_flag_ignores_the_stored_watermark(monkeypatch, local_db):
    with store.connect() as conn:
        store.set_watermark(conn, sync.SOURCE, 500)

    fake_pg = FakePgConn([])
    run_sync(monkeypatch, fake_pg, argv=["--full"])

    _, params = fake_pg._cursor.executed_with
    assert params == {"watermark": 0}


def test_uses_a_named_server_side_cursor(monkeypatch, local_db):
    """A plain cursor buffers the whole result client-side. The first
    sync could be the full real bank (40,000+ rows, some 6.5MB) - this
    must stream, not load it all into memory."""
    fake_pg = FakePgConn([])
    run_sync(monkeypatch, fake_pg)
    assert fake_pg.cursor_names == ["edsider_sync"]
    assert fake_pg._cursor.itersize and fake_pg._cursor.itersize > 1


# --------------------------------------------------------------- safety

def test_session_is_opened_read_only(monkeypatch, local_db):
    fake_pg = FakePgConn([])
    run_sync(monkeypatch, fake_pg)
    assert {"readonly": True, "autocommit": False} in fake_pg.session_calls


def test_rolls_back_rather_than_commits(monkeypatch, local_db):
    """Nothing is ever written to EdSider's database - rollback says so,
    a commit would (wrongly) imply a write transaction happened."""
    fake_pg = FakePgConn([])
    run_sync(monkeypatch, fake_pg)
    assert fake_pg.rolled_back is True
    assert fake_pg.committed is False
    assert fake_pg.closed is True


# ------------------------------------------------------------ importing

def test_new_rows_are_saved_into_our_own_database(monkeypatch, local_db):
    rows = [
        row(101, "Explain photosynthesis in green plants.",
            grade_name="8", subject_name="Science"),
        row(102, "Calculate the area of a rectangle 12 cm by 5 cm.",
            grade_name="8", subject_name="Mathematics"),
    ]
    run_sync(monkeypatch, FakePgConn(rows))

    with store.connect() as conn:
        assert store.count(conn) == 2
        q = store.get(conn, "101")
        assert "photosynthesis" in q["text"]
        assert q["grade"] == "8"
        assert q["subject"] == "Science"


def test_rows_with_no_text_are_skipped_not_crashed_on(monkeypatch, local_db):
    rows = [row(101, ""), row(102, "A real question with actual content here.")]
    run_sync(monkeypatch, FakePgConn(rows))
    with store.connect() as conn:
        assert store.count(conn) == 1


def test_parent_and_subquestion_rows_get_the_right_shape(monkeypatch, local_db):
    """Same row_shape() rule as the CSV path - a row is only 'standalone'
    (and therefore comparable) when it is neither a parent nor a child."""
    rows = [
        row(1, "Write the answer using the food chain below.",
            sub_questions="2,3"),
        row(2, "How many links does the food chain have?",
            is_subquestion="true"),
        row(3, "Explain photosynthesis in green plants.",
            grade_name="8", subject_name="Science"),
    ]
    run_sync(monkeypatch, FakePgConn(rows))
    with store.connect() as conn:
        assert store.get(conn, "1")["shape"] == "parent"
        assert store.get(conn, "2")["shape"] == "sub-question"
        assert store.get(conn, "3")["shape"] == "standalone"


# -------------------------------------------------------------- watermark

def test_watermark_advances_to_the_highest_id_pulled(monkeypatch, local_db):
    rows = [row(101, "First question here with real text."),
            row(205, "Second question here with real text.")]
    run_sync(monkeypatch, FakePgConn(rows))
    with store.connect() as conn:
        assert store.get_watermark(conn, sync.SOURCE) == 205


def test_second_run_only_asks_for_rows_after_the_new_watermark(monkeypatch, local_db):
    run_sync(monkeypatch, FakePgConn([row(101, "First question here with real text.")]))

    fake_pg_2 = FakePgConn([])
    run_sync(monkeypatch, fake_pg_2)
    _, params = fake_pg_2._cursor.executed_with
    assert params == {"watermark": 101}


def test_a_run_with_nothing_new_does_not_move_the_watermark_backwards(monkeypatch, local_db):
    run_sync(monkeypatch, FakePgConn([row(101, "First question here with real text.")]))
    run_sync(monkeypatch, FakePgConn([]))  # nothing new this time
    with store.connect() as conn:
        assert store.get_watermark(conn, sync.SOURCE) == 101


def test_rerunning_the_same_rows_is_safe(monkeypatch, local_db):
    """store.save() upserts, so re-pulling a row already imported (the
    crash-recovery path - the old watermark, re-run) must not duplicate
    it or error."""
    rows = [row(101, "First question here with real text.")]
    run_sync(monkeypatch, FakePgConn(rows))
    run_sync(monkeypatch, FakePgConn(rows), argv=["--full"])
    with store.connect() as conn:
        assert store.count(conn) == 1
