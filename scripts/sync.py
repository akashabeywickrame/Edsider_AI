"""Pull new questions straight from EdSider's own PostgreSQL, instead of
a manually exported CSV.

This is NOT a different importer. It reuses import_edsider.py's row
mapping - COLUMN_MAP, row_shape(), to_question() - unchanged. That code
already treats a row as "any dict-like object with .items()", which is
exactly what a CSV row and a psycopg2 row both are. The only new thing
here is where the rows come from.

Usage:
    python scripts/sync.py            pull everything new since last run
    python scripts/sync.py --full     ignore the watermark, pull everything
    python scripts/sync.py --reload-url http://localhost:8000
                                       tell the running service to pick
                                       up what was just synced

Needs, in the environment or a .env file next to this project (see
.env.example - copy it to .env and fill in the real values; .env is
gitignored, never commit real credentials):

    PG_HOST, PG_PORT, PG_DB, PG_USER, PG_PASSWORD

That account must be READ-ONLY - SELECT on the questions table (and
whatever grade/subject/language lookup tables the query below joins)
and nothing else. This script never writes to EdSider's database - the
session is opened read-only as a second line of defense even if the
account somehow had write access, and every statement below is a
SELECT. What it writes to is OUR OWN database, the same one
import_edsider.py fills from a CSV.

Producing this account is the same conversation as the earlier CSV
export: ask EdSider's backend team for a read-only user, and whether
their database is reachable from wherever this script runs (it almost
certainly is not reachable from a laptop without a tunnel or an
allowed IP - see the project notes for what to ask).
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import store
from scripts.import_edsider import COLUMN_MAP, to_question  # noqa: F401  (COLUMN_MAP kept for reference)

SOURCE = "edsider_postgres"

# ---------------------------------------------------------------------
# Confirmed against the real database via information_schema, not
# guessed - both the table locations and every column name below.
#
#   question.question           the questions themselves, 28 columns,
#                                schema-qualified: it is NOT bare
#                                "questions" in the public schema.
#   settings.grade(grade_id, grade, short_code)      short_code='G11'
#   settings.subject(subject_id, subject, short_code) subject='Maths'
#   settings.language(language_id, language, short_code)
#                                language='English' / 'සිංහල' / 'தமிழ்'
#
# LEFT JOIN, not JOIN: an inner join silently drops every question
# whose grade or subject is unset, which is exactly the data worth
# seeing.
# ---------------------------------------------------------------------
QUERY = """
    SELECT q.question_id,
           q.title,
           q.content,
           g.short_code         AS grade_name,
           s.subject            AS subject_name,
           l.language           AS language_name,
           q.is_subquestion,
           q.sub_questions,
           q.created_by,
           q.created_at
    FROM question.question q
    LEFT JOIN settings.grade    g ON g.grade_id = q.grade_id
    LEFT JOIN settings.subject  s ON s.subject_id = q.subject_id
    LEFT JOIN settings.language l ON l.language_id = q.language_id
    WHERE q.question_id > %(watermark)s
    -- AND q.<column> = <value>   -- a soft-delete filter, once EdSider
    --                               confirms whether one exists at all -
    --                               see CLAUDE.md, "the real EdSider
    --                               bank": no such column was found in
    --                               the export examined so far.
    ORDER BY q.question_id
"""


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--full", action="store_true",
                   help="ignore the stored watermark and pull every row")
    p.add_argument("--reload-url", default=None,
                   help="e.g. http://localhost:8000 - POST /reload there "
                        "after a successful sync that added rows")
    return p.parse_args()


def _pg_connect():
    """One place that knows how to reach EdSider's database, so nothing
    else in this file has to."""
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    import psycopg2
    import psycopg2.extras

    required = ["PG_HOST", "PG_PORT", "PG_DB", "PG_USER", "PG_PASSWORD"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        raise SystemExit(
            f"\nMissing environment variable(s): {', '.join(missing)}\n"
            "Copy .env.example to .env and fill in the real values, or "
            "set them in the shell. See the top of this file for what "
            "to ask EdSider for.\n"
        )

    conn = psycopg2.connect(
        host=os.environ["PG_HOST"],
        port=os.environ["PG_PORT"],
        dbname=os.environ["PG_DB"],
        user=os.environ["PG_USER"],
        password=os.environ["PG_PASSWORD"],
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    # A client-side backstop, not the real safety mechanism - the real
    # one is the account itself having no write grants. Belt and braces.
    conn.set_session(readonly=True, autocommit=False)
    return conn


def fetch_new_rows(pg_conn, watermark):
    """Stream rows with id > watermark, without loading the whole result
    into memory - a named cursor keeps this flat in memory whether it
    is pulling 12 new rows or, on the very first run, all 41,000."""
    cur = pg_conn.cursor(name="edsider_sync")
    cur.itersize = 500
    cur.execute(QUERY, {"watermark": watermark})
    for row in cur:
        yield dict(row)
    cur.close()


def main():
    args = parse_args()

    with store.connect() as local_conn:
        watermark = 0 if args.full else store.get_watermark(local_conn, SOURCE)

    print(f"\nSyncing from EdSider's database "
          f"({'full pull' if args.full else f'since id {watermark}'})...")

    pg_conn = _pg_connect()
    try:
        pulled = imported = skipped = 0
        highest_seen = watermark

        with store.connect() as local_conn:
            for row in fetch_new_rows(pg_conn, watermark):
                pulled += 1
                q = to_question(row, pulled)

                qid = row.get("question_id") or row.get("id")
                if qid is not None:
                    try:
                        highest_seen = max(highest_seen, int(qid))
                    except (TypeError, ValueError):
                        pass

                if q is None:
                    skipped += 1
                    continue

                store.save(local_conn, q)
                imported += 1

            # Only advance the watermark after every row in this batch
            # saved without error. If something crashes mid-loop, the
            # next run re-pulls from the old watermark - safe, because
            # store.save() is an upsert (INSERT OR REPLACE).
            store.set_watermark(local_conn, SOURCE, highest_seen)
            total = store.count(local_conn)
    finally:
        pg_conn.rollback()  # nothing was written; close the read cleanly
        pg_conn.close()

    print(f"  pulled from EdSider : {pulled:,}")
    print(f"  imported            : {imported:,}")
    print(f"  skipped (no text)   : {skipped:,}")
    print(f"  watermark now at    : {highest_seen:,}")
    print(f"  our database total  : {total:,}")

    if imported:
        print("\nNext: python scripts/embed.py   (only new text gets embedded)")

    if args.reload_url and imported:
        import urllib.request
        try:
            req = urllib.request.Request(
                args.reload_url.rstrip("/") + "/reload", method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                print(f"  told the service to reload: {resp.status}")
        except Exception as e:
            print(f"  could not reach {args.reload_url}/reload: {e}")

    print()


if __name__ == "__main__":
    main()
