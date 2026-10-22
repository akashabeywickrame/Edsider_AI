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
    called on the way out.

    Two separate cursors, keyed by name, because sync.py now opens two
    of them per run - one for questions, one for assessment links - and
    they must not share rows or an executed_with record."""

    def __init__(self, rows, link_rows=None, teacher_rows=None, lookup_rows=None):
        self.rows = rows
        self.link_rows = link_rows if link_rows is not None else []
        # run_sync() also refreshes the teacher list, which refuses an
        # empty result - so a fake EdSider has at least one teacher.
        self.teacher_rows = (teacher_rows if teacher_rows is not None
                             else [{"email": "t@example.com", "name": "T"}])
        # Likewise EdSider's reference lists, which also refuse an empty pull.
        self.lookup_rows = (lookup_rows if lookup_rows is not None
                            else [lookup("grade", 12, "12", "G12")])
        self.cursor_names = []
        self.session_calls = []
        self.rolled_back = False
        self.committed = False
        self.closed = False
        self._cursor = FakeCursor(rows)
        self._links_cursor = FakeCursor(self.link_rows)
        self._teachers_cursor = FakeCursor(self.teacher_rows)
        self._lookups_cursor = FakeCursor(self.lookup_rows)

    def cursor(self, name=None, cursor_factory=None):
        self.cursor_names.append(name)
        if name == "edsider_sync_links":
            return self._links_cursor
        if name == "edsider_sync_teachers":
            return self._teachers_cursor
        if name == "edsider_sync_lookups":
            return self._lookups_cursor
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


def lookup(kind, id, name, short_code=None, extra=None):
    """Shaped like a row of sync.LOOKUPS_QUERY."""
    return {"kind": kind, "id": id, "name": name, "short_code": short_code, "extra": extra}


def link_row(assessment_question_id, assessment_id, question_id,
            question_number=1, mark_allocation=25, section_id=None,
            added_by="CASS2"):
    """Shaped like a row from assessment.assessment_question, confirmed
    against the real table's columns."""
    return {
        "assessment_question_id": assessment_question_id,
        "assessment_id": assessment_id, "question_id": question_id,
        "question_number": question_number,
        "mark_allocation": mark_allocation,
        "section_id": section_id, "added_by": added_by,
    }


def run_sync(monkeypatch, fake_pg, argv=()):
    # Patched at the psycopg2.connect level, not sync._pg_connect - so
    # _pg_connect()'s own logic (the read-only session, the required-env
    # check) actually runs and is what these tests are exercising.
    for var in ("PG_HOST", "PG_PORT", "PG_DB", "PG_USER", "PG_PASSWORD"):
        monkeypatch.setenv(var, "test")

    real_connect = psycopg2.connect

    def fake_connect(*args, **kwargs):
        """Only EdSider's database is faked.

        Our own store is PostgreSQL now and goes through the same
        psycopg2.connect, so a blanket replacement cut the test off from
        the database it is supposed to be writing into. The two calls are
        told apart by their shape: sync.py passes EdSider's credentials
        as keyword arguments, store.py passes one connection string.
        """
        if args:
            return real_connect(*args, **kwargs)
        return fake_pg

    monkeypatch.setattr(psycopg2, "connect", fake_connect)
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
    must stream, not load it all into memory. One cursor per query -
    questions, assessment links, teachers, then EdSider's lists."""
    fake_pg = FakePgConn([])
    run_sync(monkeypatch, fake_pg)
    assert fake_pg.cursor_names == [
        "edsider_sync", "edsider_sync_links", "edsider_sync_teachers", "edsider_sync_lookups"]
    assert fake_pg._cursor.itersize and fake_pg._cursor.itersize > 1
    assert fake_pg._links_cursor.itersize and fake_pg._links_cursor.itersize > 1


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


# ------------------------------------------------------ assessment links

def test_links_query_uses_its_own_stored_watermark(monkeypatch, local_db):
    """The link sync has a separate watermark from the question sync -
    they can be at different points, e.g. after a crash between the
    two."""
    with store.connect() as conn:
        store.set_watermark(conn, sync.SOURCE, 900)
        store.set_watermark(conn, sync.SOURCE_LINKS, 30)

    fake_pg = FakePgConn([])
    run_sync(monkeypatch, fake_pg)

    _, q_params = fake_pg._cursor.executed_with
    _, link_params = fake_pg._links_cursor.executed_with
    assert q_params == {"watermark": 900}
    assert link_params == {"watermark": 30}


def test_link_rows_are_saved_into_our_own_database(monkeypatch, local_db):
    links = [
        link_row(13, assessment_id=5, question_id=8, question_number=1),
        link_row(14, assessment_id=5, question_id=9, question_number=2),
    ]
    run_sync(monkeypatch, FakePgConn([], link_rows=links))

    with store.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM question_assessments ORDER BY link_id"
        ).fetchall()
    assert len(rows) == 2
    assert rows[0]["assessment_id"] == "5"
    assert rows[0]["question_id"] == "8"
    assert rows[0]["question_number"] == 1
    assert rows[0]["mark_allocation"] == 25
    assert rows[0]["added_by"] == "CASS2"


def test_link_watermark_advances_to_the_highest_link_id_pulled(monkeypatch, local_db):
    links = [link_row(13, 5, 8), link_row(25, 8, 17)]
    run_sync(monkeypatch, FakePgConn([], link_rows=links))
    with store.connect() as conn:
        assert store.get_watermark(conn, sync.SOURCE_LINKS) == 25


def test_rerunning_the_same_links_is_safe(monkeypatch, local_db):
    """save_assessment_link() upserts on link_id, same crash-recovery
    reasoning as the question sync - re-pulling an already-saved link
    must not duplicate it."""
    links = [link_row(13, 5, 8)]
    run_sync(monkeypatch, FakePgConn([], link_rows=links))
    run_sync(monkeypatch, FakePgConn([], link_rows=links), argv=["--full"])
    with store.connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM question_assessments"
        ).fetchone()["n"]
    assert n == 1


def test_a_question_can_have_more_than_one_assessment_link(monkeypatch, local_db):
    """The whole reason this is a separate table rather than a column:
    the same question reused across two papers is normal, not an error."""
    links = [link_row(13, assessment_id=5, question_id=8),
            link_row(14, assessment_id=6, question_id=8)]
    run_sync(monkeypatch, FakePgConn([], link_rows=links))
    with store.connect() as conn:
        linked = store.assessments_for_question(conn, "8")
    assert {r["assessment_id"] for r in linked} == {"5", "6"}


# --------------------------------------------------- EdSider's own lists

def test_edsiders_lists_are_copied_with_their_ids(monkeypatch, local_db):
    fake_pg = FakePgConn([], lookup_rows=[
        lookup("grade", 12, "12", "G12"),
        lookup("subject", 14, "General Information Technology", "GIT"),
        lookup("part", 7, "I", extra={"notation_id": 3}),
    ])
    run_sync(monkeypatch, fake_pg)
    with store.connect() as conn:
        lists = store.edsider_lookups(conn)
    assert lists["grade"] == [{"id": 12, "name": "12", "short_code": "G12", "extra": None}]
    assert lists["subject"][0]["id"] == 14
    assert lists["part"][0]["extra"] == {"notation_id": 3}
    # Read through its own cursor, in the same read-only session.
    assert fake_pg.session_calls[0]["readonly"] is True
    assert fake_pg.rolled_back and not fake_pg.committed


def test_an_entry_edsider_drops_goes_inactive_and_other_lists_are_kept(local_db):
    with store.connect() as conn:
        store.sync_edsider_lookups(conn, [lookup("term", 1, "First Term Test", "T1"),
                                          lookup("term", 4, "Fourth Term", "T4"),
                                          lookup("year", 7, "2026")])
    with store.connect() as conn:
        # EdSider removed term 4; the year list was not part of this pull.
        store.sync_edsider_lookups(conn, [lookup("term", 1, "First Term Test", "T1")])
        lists = store.edsider_lookups(conn)
        dropped = conn.execute("SELECT active FROM common.term WHERE id = 4").fetchone()
    assert [t["id"] for t in lists["term"]] == [1]
    assert dropped["active"] is False          # kept, not deleted - a sent paper may name it
    assert [y["name"] for y in lists["year"]] == ["2026"]


def test_names_are_trimmed_and_an_empty_pull_is_refused(local_db):
    with store.connect() as conn:
        store.sync_edsider_lookups(conn, [lookup("subject", 18, "Chemistry ", "Chemistry ")])
        assert store.edsider_lookups(conn)["subject"][0]["name"] == "Chemistry"
        with pytest.raises(ValueError):
            store.sync_edsider_lookups(conn, [])
