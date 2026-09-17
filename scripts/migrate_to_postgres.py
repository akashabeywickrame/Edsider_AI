"""Copy an old SQLite bank into the PostgreSQL database, once.

The project stored everything in SQLite until EdSider asked for one
database technology across the application; the bank, the intake drafts
and the reviewers' decisions now live in PostgreSQL (see src/store.py).
This moves the existing data across. It is a one-off, but safe to re-run:
every table is keyed, and rows already present are left alone rather than
duplicated.

    $env:EDSIDER_DB = "postgresql://edsider_ai:PASSWORD@host:5432/edsider_ai"
    python scripts/migrate_to_postgres.py data/edsider_real.db

Reads the SQLite file read-only and never writes to it, so the old bank
stays exactly as it was until someone deletes it deliberately.

The fingerprints are re-encoded on the way: SQLite held each one as a
JSON string of 768 numbers, about 17 KB per question and 519 MB in total.
PostgreSQL holds the same numbers as pgvector or bytea, about 3 KB each.
"""

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import store  # noqa: E402

# Everything except `embeddings`, which needs its vectors re-encoded and
# so is handled on its own below. Order matters only for intake_questions,
# which references intake_documents.
TABLES = [
    "questions",
    "decisions",
    "sync_state",
    "question_assessments",
    "intake_documents",
    "intake_questions",
]

BATCH = 1000


def sqlite_columns(old, table):
    return [r[1] for r in old.execute(f"PRAGMA table_info({table})")]


def postgres_columns(conn, table):
    return [r["column_name"] for r in conn.execute(
        """SELECT column_name FROM information_schema.columns
            WHERE table_name = ? AND table_schema = current_schema()""",
        (table,))]


def copy_table(old, conn, table):
    """Copy one table, column by column NAME rather than by position -
    the two schemas have the same columns but not always in the same
    order, and a column one side has and the other does not (an older
    SQLite file, a column added since) is simply skipped."""
    if table not in {r["table_name"] for r in conn.execute(
            """SELECT table_name FROM information_schema.tables
                WHERE table_schema = current_schema()""")}:
        return 0, f"{table}: not in the PostgreSQL schema, skipped"

    here = set(postgres_columns(conn, table))
    there = [c for c in sqlite_columns(old, table) if c in here]
    if not there:
        return 0, f"{table}: no columns in common, skipped"

    names = ", ".join(there)
    cursor = old.execute(f"SELECT {names} FROM {table}")
    copied = 0
    while True:
        batch = cursor.fetchmany(BATCH)
        if not batch:
            break
        store.bulk_insert(conn, table, there, [tuple(r) for r in batch])
        copied += len(batch)
    return copied, f"{table}: {copied:,} rows"


def copy_embeddings(old, conn):
    """The fingerprints, re-encoded from JSON text into whichever form
    this database stores (pgvector, or bytea where the extension is not
    available)."""
    cursor = old.execute(
        "SELECT content_hash, model, vector FROM embeddings")
    copied = 0
    while True:
        batch = cursor.fetchmany(BATCH)
        if not batch:
            break
        rows = [(h, model, store.encode_vector(json.loads(vector)))
                for h, model, vector in batch]
        store.bulk_insert(conn, "embeddings",
                          ["content_hash", "model", "vector"], rows)
        copied += len(batch)
    return copied, f"embeddings: {copied:,} rows"


def sequences_after_insert(conn):
    """Move each identity sequence past the ids just copied.

    The rows carry their original ids - a decision keeps its number, an
    intake document keeps the id its drafts point at - so the sequence
    behind each identity column still sits at 1 and the next INSERT would
    collide with row 1. This is the step that is easy to forget and shows
    up later as a duplicate key error.
    """
    for table in ("decisions", "intake_documents", "intake_questions"):
        conn.execute(
            f"""SELECT setval(pg_get_serial_sequence('{table}', 'id'),
                              COALESCE((SELECT MAX(id) FROM {table}), 1))""")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sqlite_file", type=Path,
                        help="the old bank, e.g. data/edsider_real.db")
    args = parser.parse_args(argv)

    if not args.sqlite_file.is_file():
        raise SystemExit(f"No SQLite database at {args.sqlite_file}")

    started = time.perf_counter()
    old = sqlite3.connect(f"file:{args.sqlite_file}?mode=ro", uri=True)
    try:
        with store.connect() as conn:
            print(f"into  {store.display_name()}  "
                  f"(fingerprints as {store.VECTOR_STORAGE})")
            print(f"from  {args.sqlite_file}\n")
            for table in TABLES:
                print("  " + copy_table(old, conn, table)[1])
            print("  " + copy_embeddings(old, conn)[1])
            sequences_after_insert(conn)
    finally:
        old.close()

    with store.connect() as conn:
        print("\nin PostgreSQL now:")
        for table in TABLES + ["embeddings"]:
            n = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            print(f"  {table:<22} {n:>9,}")
    print(f"\ndone in {time.perf_counter() - started:.0f}s")


if __name__ == "__main__":
    main()
