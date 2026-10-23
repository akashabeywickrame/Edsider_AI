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
#
# answer / is_answer_blob - confirmed against questionModel.py:58.
# `answer` is not one uniform shape: for an MCQ it is a comma-separated
# list of per-option percentage weights ("100,0,0,0"), index-aligned
# with the options embedded in `content`; for an essay/submission
# question it is a free-text model answer a teacher can reference, not
# used for auto-grading. `is_answer_blob` marks the third case, a blob
# reference rather than literal text, so it is never mistaken for one.
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
           q.answer,
           q.is_answer_blob,
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

SOURCE_LINKS = "edsider_assessment_links"

# Which assessment(s) each question belongs to is a SEPARATE table -
# assessment.assessment_question - not a column on question.question, so
# it needs its own query, not a JOIN bolted onto QUERY above. A JOIN
# there would fan a question out into one row per assessment it belongs
# to, breaking the one-row-per-question assumption fetch_new_rows() and
# to_question() both rely on.
#
# Confirmed directly against a screenshot of the real table, not
# guessed:
#   assessment_question_id  PK, integer   - our watermark column
#   assessment_id            integer
#   question_id               integer
#   added_by                  varchar(15)
#   mark_allocation            double precision
#   question_number            integer
#   section_id                 integer, nullable
#   is_optional                boolean    (not pulled - not needed yet)
LINKS_QUERY = """
    SELECT assessment_question_id, assessment_id, question_id,
           question_number, mark_allocation, section_id, added_by
    FROM assessment.assessment_question
    WHERE assessment_question_id > %(watermark)s
    ORDER BY assessment_question_id
"""


# EdSider's teachers and Data Entry Operators, for the assignment dropdowns
# and sign-in. Every table and column here was confirmed against the live
# schema, not guessed: a staff member's role rows live in staff_user_role
# (role_code 'TEACH' is "Teacher", 'DEOP' "Data Entry Operator" in
# user_management.role); staff_user.staff_id points at
# registration.teacher.user_id, which holds the name - operators' too.
# Measured: 133 distinct teacher emails, 30 operators (29 of them also
# Assessors), one person both; names carry stray spaces. One row per email
# (two people are listed twice). user_status_id 1 is "Active".
TEACHERS_QUERY = """
    SELECT lower(trim(su.user_email)) AS email,
           max(NULLIF(trim(concat_ws(' ', trim(t.first_name), trim(t.last_name))), '')) AS name,
           array_agg(DISTINCT r.role_code) AS roles,
           array_agg(DISTINCT r.role_code || '|' || trim(ro.role_name)) AS positions,
           min(su.user_id) AS user_id,
           min(su.staff_id) AS staff_id
    FROM user_management.staff_user su
    JOIN user_management.staff_user_role r ON r.user_id = su.user_id
         AND (r.to_date IS NULL OR r.to_date >= now())
    JOIN user_management.role ro ON ro.role_code = r.role_code
    LEFT JOIN registration.teacher t ON t.user_id = su.staff_id
    WHERE (t.user_status_id IS NULL OR t.user_status_id = 1)
      AND coalesce(trim(su.user_email), '') <> ''
    GROUP BY lower(trim(su.user_email))
    -- Teachers and Data Entry Operators are the people this app has work
    -- for; every other role they hold (most operators are also Assessors)
    -- comes along as a position.
    HAVING bool_or(r.role_code = 'TEACH' OR r.role_code = 'DEOP')
    ORDER BY lower(trim(su.user_email))
"""


# EdSider's reference lists, so a paper sent there carries EdSider's own
# ids (grade_id, subject_id, term_id, exam_category_id ...). One query,
# not one per list: each round trip to the Azure server costs ~0.35 s.
# Every table and column here was read off the live schema; names are
# trimmed because EdSider stores some with trailing spaces ("Chemistry ").
LOOKUPS_QUERY = """
SELECT 'grade' AS kind, grade_id AS id, grade::text AS name, short_code, NULL::json AS extra FROM settings.grade
UNION ALL SELECT 'subject', subject_id, trim(subject), trim(short_code), NULL FROM settings.subject
UNION ALL SELECT 'language', language_id, language, short_code, NULL FROM settings.language
UNION ALL SELECT 'term', term_id, term, short_code, NULL FROM settings.term
UNION ALL SELECT 'year', year_id, year, NULL, NULL FROM settings.year
UNION ALL SELECT 'exam_category', exam_category_id, name, short_code, NULL
          FROM assessment_subscription.exam_category
UNION ALL SELECT 'difficulty', difficulty_id, difficulty_type, NULL, NULL FROM settings.difficulty
UNION ALL SELECT 'source', source_id, trim(source), NULL, NULL FROM settings.source
UNION ALL SELECT 'author', author_id, trim(author), NULL, json_build_object('type_id', type_id)
          FROM settings.author
UNION ALL SELECT 'part_notation', notation_id, notation_type, notation_value, NULL FROM assessment.notation
UNION ALL SELECT 'part', part_id, name, NULL, json_build_object('notation_id', notation_id) FROM assessment.part
UNION ALL SELECT 'assessment_category', category_id, name, NULL, NULL FROM assessment.category
UNION ALL SELECT 'grading_method', grading_method_id, method_name, NULL, NULL FROM settings.grading_method
UNION ALL SELECT 'privilege', privilege_id, privilege, NULL, NULL FROM settings.privilege
UNION ALL SELECT 'attempt', attempt_no_id, no_of_attempts::text, NULL, NULL FROM assessment.attempt
UNION ALL SELECT 'time_unit', unit_id, unit, NULL, NULL FROM assessment.unit
UNION ALL SELECT 'passage_type', passage_type_id, name, NULL, NULL FROM question.passage_type
UNION ALL SELECT 'question_type', type_id, qtype, NULL, NULL FROM question.type
UNION ALL SELECT 'answer_type', answer_type_id, answer_type, NULL, NULL FROM question.answer_type
UNION ALL SELECT 'assessment_status', status_id, status, NULL, json_build_object('display_order', display_order)
          FROM assessment.status
UNION ALL SELECT 'chapter', chapter_id, chapter, NULL, NULL FROM settings.chapter
"""


def sync_lookups(pg_conn=None):
    """Replace our copy of EdSider's reference lists with the current ones
    - a few hundred rows, re-read whole every time for the same reason as
    the teacher list: only a full read notices an entry EdSider removed.
    Returns {kind: active entries}."""
    own = pg_conn is None
    if own:
        pg_conn = _pg_connect()
    try:
        cur = pg_conn.cursor(name="edsider_sync_lookups")
        cur.execute(LOOKUPS_QUERY, {})
        rows = [dict(r) for r in cur]
        cur.close()
    finally:
        if own:
            pg_conn.rollback()
            pg_conn.close()
    with store.connect() as local_conn:
        return store.sync_edsider_lookups(local_conn, rows)


def sync_teachers(pg_conn=None):
    """Replace our copy of EdSider's teacher list with the current one.
    Small enough (~135 rows) to re-read whole every time, which is also
    the only way to notice a teacher EdSider has removed - a watermark
    only ever sees new rows. Returns how many teachers are now stored."""
    own = pg_conn is None
    if own:
        pg_conn = _pg_connect()
    try:
        # Named, like the question and link cursors - its own cursor, so
        # it never shares state with theirs.
        cur = pg_conn.cursor(name="edsider_sync_teachers")
        cur.execute(TEACHERS_QUERY, {})
        teachers = [dict(r) for r in cur]
        cur.close()
    finally:
        if own:
            pg_conn.rollback()
            pg_conn.close()
    with store.connect() as local_conn:
        return store.sync_edsider_teachers(local_conn, teachers)


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
        # Without these, a flaky path to EdSider's server (seen twice
        # today - "server closed the connection unexpectedly") hangs on
        # the operating system's own default timeout instead of failing
        # fast - minutes of a stuck button with no feedback, rather than
        # a clear error the caller can act on. connect_timeout bounds
        # opening the connection; statement_timeout bounds any one query
        # once connected, in case the hang is mid-fetch rather than at
        # connect time.
        connect_timeout=10,
        options="-c statement_timeout=30000",
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


def fetch_new_links(pg_conn, watermark):
    """Same streaming shape as fetch_new_rows, over the assessment link
    table instead - a separate named cursor, opened after the first one
    has already been closed by exhausting fetch_new_rows()."""
    cur = pg_conn.cursor(name="edsider_sync_links")
    cur.itersize = 500
    cur.execute(LINKS_QUERY, {"watermark": watermark})
    for row in cur:
        yield dict(row)
    cur.close()


def run_sync(full=False):
    """Pull new rows from EdSider's database into our own, and return a
    summary dict of what happened.

    This is the reusable core of `main()` below - also called directly
    from app.py's "Reload from database" button, so pressing it actually
    fetches anything EdSider added since the last sync, rather than just
    re-reading whatever our own copy already happened to hold (see
    CLAUDE.md, "Pulling directly from EdSider's database", for why those
    used to be two separate, easily-confused steps).
    """
    with store.connect() as local_conn:
        watermark = 0 if full else store.get_watermark(local_conn, SOURCE)
        link_watermark = (0 if full
                          else store.get_watermark(local_conn, SOURCE_LINKS))

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
            # store.save() is an upsert (ON CONFLICT DO UPDATE).
            store.set_watermark(local_conn, SOURCE, highest_seen)
            total = store.count(local_conn)

        # Every parent row just synced carries its own children_raw
        # (unchanged from import_edsider.py's to_question()); use it to
        # link each child to its parent_id. Runs over every parent
        # currently in the database, not just this batch's, so a child
        # synced in an earlier run still gets linked once its parent
        # arrives now, and vice versa.
        with store.connect() as local_conn:
            linked = store.link_children_to_parents(local_conn)

        # Which assessments each question belongs to - a second pull,
        # with its own watermark, from assessment.assessment_question.
        # Independent of the question sync above: it can run, fail, or
        # be re-run without touching question rows at all.
        links_pulled = links_saved = 0
        highest_link_seen = link_watermark

        with store.connect() as local_conn:
            for link in fetch_new_links(pg_conn, link_watermark):
                links_pulled += 1
                link_id = link.get("assessment_question_id")
                if link_id is not None:
                    try:
                        highest_link_seen = max(highest_link_seen, int(link_id))
                    except (TypeError, ValueError):
                        pass

                store.save_assessment_link(
                    local_conn, link_id,
                    link.get("assessment_id"), link.get("question_id"),
                    link.get("question_number"), link.get("mark_allocation"),
                    link.get("section_id"), link.get("added_by"),
                )
                links_saved += 1

            store.set_watermark(local_conn, SOURCE_LINKS, highest_link_seen)

        teachers = sync_teachers(pg_conn)
        lookups = sync_lookups(pg_conn)
    finally:
        pg_conn.rollback()  # nothing was written; close the read cleanly
        pg_conn.close()

    return {
        "pulled": pulled, "imported": imported, "skipped": skipped,
        "watermark": highest_seen, "total": total, "linked": linked,
        "links_pulled": links_pulled, "links_saved": links_saved,
        "link_watermark": highest_link_seen,
        "teachers": teachers,
        "lookups": lookups,
    }


def main():
    args = parse_args()
    print(f"\nSyncing from EdSider's database "
          f"({'full pull' if args.full else 'incremental'})...")

    result = run_sync(full=args.full)
    imported = result["imported"]

    print(f"  pulled from EdSider : {result['pulled']:,}")
    print(f"  imported            : {result['imported']:,}")
    print(f"  skipped (no text)   : {result['skipped']:,}")
    print(f"  watermark now at    : {result['watermark']:,}")
    print(f"  our database total  : {result['total']:,}")
    print(f"  sub-questions linked to a parent : {result['linked']:,}")
    print(f"  assessment links    : {result['links_saved']:,} pulled "
          f"(watermark now at {result['link_watermark']:,})")
    print(f"  EdSider teachers    : {result['teachers']:,}")
    print(f"  EdSider lists       : {sum(result['lookups'].values()):,} entries "
          f"in {len(result['lookups'])} lists")

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
