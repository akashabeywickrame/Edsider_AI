"""Shared test fixtures.

The project stores its data in PostgreSQL, so the tests do too - a test
that proves the code against a different database proves less than it
looks like it does. What used to be a throwaway file in tmp_path is now
a throwaway SCHEMA: one connection string, one test database, and every
test gets its own set of tables inside it, created and dropped around it.

Point EDSIDER_TEST_DB at a database nobody minds losing (see
.env.example); the tests create it if it does not exist yet, and skip
themselves with a clear message if no server is reachable at all.
"""

import json
import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import store  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# A copy of EdSider's real lists (ids as EdSider has them), from the sync.
EDSIDER_LISTS = json.loads((ROOT / "tests" / "edsider_lists.json").read_text(encoding="utf-8"))


def _test_dsn():
    """The test database's connection string, from the environment or
    .env - never store.DSN, which may well be the real bank."""
    dsn = os.environ.get("EDSIDER_TEST_DB")
    if not dsn:
        try:
            from dotenv import dotenv_values
            dsn = dotenv_values(ROOT / ".env").get("EDSIDER_TEST_DB")
        except ImportError:
            dsn = None
    return dsn


def _ensure_database(dsn):
    """Create the test database if the server is up but it is missing.

    CREATE DATABASE cannot run inside a transaction, hence autocommit on
    a connection to the server's own `postgres` database.
    """
    import psycopg2
    from psycopg2 import extensions

    info = extensions.parse_dsn(dsn)
    wanted = info["dbname"]
    try:
        psycopg2.connect(dsn).close()
        return
    except psycopg2.OperationalError as missing:
        if "does not exist" not in str(missing):
            raise

    admin = psycopg2.connect(**{**info, "dbname": "postgres"})
    admin.set_isolation_level(extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    try:
        admin.cursor().execute(f'CREATE DATABASE "{wanted}"')
    finally:
        admin.close()


@pytest.fixture
def local_db():
    """A schema of this test's own, dropped afterwards.

    Named `local_db` because that is what every test module already asks
    for; what changed underneath is the storage, not the contract - the
    test still gets an empty database it cannot leak out of.
    """
    dsn = _test_dsn()
    if not dsn:
        pytest.skip("EDSIDER_TEST_DB is not set - see .env.example")

    import psycopg2
    try:
        _ensure_database(dsn)
    except psycopg2.OperationalError as down:
        pytest.skip(f"no PostgreSQL to test against: {down}")

    previous_dsn, previous_schema = store.DSN, store.SCHEMA_NAME
    previous_cache = store.CACHE_DIR
    schema = f"t_{uuid.uuid4().hex[:12]}"
    # No disk cache in tests: a test's made-up 8-number fingerprints must
    # never land in the file the real app reads.
    store.CACHE_DIR = None
    store.use_database(dsn, schema=schema)
    # EdSider's own grades, subjects, mediums and the rest, as the sync
    # copies them - a paper's grade and subject are ids on these lists.
    with store.connect() as conn:
        store.sync_edsider_lookups(conn, EDSIDER_LISTS)
    # The API holds the lists for a few minutes; a previous test's must
    # not answer for this one.
    if "service" in sys.modules:
        sys.modules["service"]._EDSIDER_LISTS["lists"] = None
    try:
        yield
    finally:
        # The pool keeps connections open between tests' own calls; close
        # them first so nothing still attached to the schema can hold up
        # dropping it.
        store._close_pool()
        # Drop the schema through a connection of its own: a failed test
        # may have left its own transaction aborted.
        raw = psycopg2.connect(dsn)
        raw.autocommit = True
        try:
            for name in (schema, *store.schema_names()):
                raw.cursor().execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        finally:
            raw.close()
        store.use_database(previous_dsn, schema=previous_schema)
        store.CACHE_DIR = previous_cache
