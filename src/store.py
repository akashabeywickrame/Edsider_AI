"""Database layer.

Everything that touches storage lives here. This is the seam the project
was built around, and this is the change it was for: the bank, the intake
drafts and the reviewers' decisions live in PostgreSQL, not SQLite.

EdSider already runs PostgreSQL, and one application should not carry two
database technologies. Ours is a database of its own (edsider_ai) on
their server - beside their data, never inside it: scripts/sync.py still
reads their tables through its own read-only account, and nothing here
writes to them.

Configured with EDSIDER_DB, a libpq connection string:

    $env:EDSIDER_DB = "postgresql://edsider_ai:PASSWORD@host:5432/edsider_ai"

EDSIDER_SCHEMA picks a schema other than `public` - how the tests give
each test its own isolated copy of the tables on one server.
"""

import array
import json
import os
import re
import threading
import time
from pathlib import Path

import numpy as np
import psycopg2
import psycopg2.extensions
import psycopg2.extras

from .normalize import content_hash

ROOT = Path(__file__).resolve().parent.parent

# A connection string is a credential, so it lives in .env (gitignored),
# not in a shell that has to be set up again in every new terminal - and
# "it works in my terminal but the app says no database" was exactly the
# confusion that followed the move off SQLite. A variable already set in
# the environment still wins; load_dotenv does not override one.
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # python-dotenv is optional; the env var still works
    pass

# No default database. There used to be one - a 30-question sample bank -
# and silently falling back to it when EDSIDER_DB was forgotten was
# already dangerous: it opened with the sample's planted duplicates
# showing as though they were EdSider's own. connect() refuses to guess
# instead.
#
# None here is a real value, not a bug - it means "not configured yet",
# checked explicitly by connect(). Tests never rely on this default;
# every fixture calls use_database() before connect().
DSN = os.environ.get("EDSIDER_DB") or None
SCHEMA_NAME = os.environ.get("EDSIDER_SCHEMA") or "public"

# How the fingerprints are stored, decided once when the schema is
# applied: "vector" where the pgvector extension is available (the real
# server), "bytea" where it is not (a plain PostgreSQL, which is what a
# developer laptop usually has). Both hold the same float32 numbers and
# both are read back the same way by _decode_vector() - pgvector adds the
# indexes that will eventually end the O(n^2) sweep, which bytea cannot,
# so it is preferred wherever it can be turned on.
VECTOR_STORAGE = None

# Where fingerprints are kept on local disk between runs - see
# _cached_vectors(). None turns the disk copy off (the tests do, so a
# test's made-up vectors can never land in the real app's cache).
CACHE_DIR = Path(os.environ.get("EDSIDER_CACHE_DIR") or ROOT / "data" / "cache")

# Measured from this laptop to EdSider's Azure server: opening a
# connection (TCP, TLS, authentication) costs 2.5 seconds, and every
# statement after that about 0.35. The app was written against SQLite on
# local disk, where both cost nothing, and opened a fresh connection for
# every read on every click. Keepalives stop an idle connection in the
# pool from being silently dropped by a firewall between uses.
_CONNECT_OPTIONS = {
    "connect_timeout": 15,
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
}

# How many idle connections to keep, and how long one may sit idle
# before it is checked with a round trip before being handed out again.
_POOL_MAX_IDLE = 8
_IDLE_CHECK_SECONDS = 60


def _schema_sql(vector_type):
    return SCHEMA_TEMPLATE.replace("{{vector}}", vector_type)


SCHEMA_TEMPLATE = """
CREATE TABLE IF NOT EXISTS questions (
    id            TEXT PRIMARY KEY,
    text          TEXT NOT NULL,
    title         TEXT,
    grade         TEXT,
    subject       TEXT,
    topic         TEXT,
    marks         INTEGER,
    answer        TEXT,
    language      TEXT DEFAULT 'en',
    contributor   TEXT,
    source_file   TEXT,
    content_hash  TEXT NOT NULL,
    has_figure    INTEGER DEFAULT 0,
    -- 'standalone' | 'sub-question' | 'parent'.
    -- EdSider stores a multi-part question as a parent row holding the
    -- shared stimulus ("...using below food chain: Grass -> Frog") plus
    -- child rows holding the parts ("How many links of the above food
    -- chain"). A child is meaningless without its parent, and a parent
    -- is an instruction rather than a question, so only standalone rows
    -- can be compared on their text alone.
    shape         TEXT NOT NULL DEFAULT 'standalone',
    status        TEXT NOT NULL DEFAULT 'APPROVED',
    approved_by   TEXT,
    created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_hash    ON questions(content_hash);
CREATE INDEX IF NOT EXISTS idx_subject ON questions(subject, grade);

-- Meaning fingerprints, keyed by content_hash rather than question id.
-- Two questions with identical text share a hash, so identical text is
-- never embedded twice. That caching matters: embedding is the slowest
-- step in the pipeline.
CREATE TABLE IF NOT EXISTS embeddings (
    content_hash TEXT PRIMARY KEY,
    model        TEXT NOT NULL,
    vector       {{vector}} NOT NULL,
    created_at   TIMESTAMPTZ DEFAULT now()
);

-- Every human verdict on a flagged pair is recorded. This becomes the
-- evaluation set, the threshold tuner, and later, training data.
CREATE TABLE IF NOT EXISTS decisions (
    id          INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    question_a  TEXT NOT NULL,
    question_b  TEXT NOT NULL,
    similarity  DOUBLE PRECISION,
    guard_flags TEXT,
    verdict     TEXT NOT NULL CHECK (verdict IN ('duplicate','distinct','skipped')),
    decided_by  TEXT NOT NULL,
    decided_at  TIMESTAMPTZ DEFAULT now()
);

-- Bookkeeping for scripts/sync.py: how far the last pull from EdSider's
-- own database got, per source, so the next run asks for only what is
-- new rather than re-reading everything. This is OUR state about a
-- sync, not a copy of anything from their side.
CREATE TABLE IF NOT EXISTS sync_state (
    source          TEXT PRIMARY KEY,
    last_id         BIGINT NOT NULL DEFAULT 0,
    last_synced_at  TIMESTAMPTZ
);

-- Which assessments (exam papers) each question appears on. Mirrors
-- EdSider's own assessment.assessment_question junction table directly -
-- a question can legitimately appear on more than one assessment (the
-- same item reused across two Grade 10 Term 1 papers is normal, not a
-- bug), so this is a many-to-many link, not a column on questions.
-- Keyed on link_id - their own assessment_question_id - so a re-synced
-- row replaces itself instead of duplicating.
CREATE TABLE IF NOT EXISTS question_assessments (
    link_id          BIGINT PRIMARY KEY,
    assessment_id    TEXT NOT NULL,
    question_id      TEXT NOT NULL,
    question_number  INTEGER,
    mark_allocation  DOUBLE PRECISION,
    section_id       TEXT,
    added_by         TEXT
);

CREATE INDEX IF NOT EXISTS idx_qa_assessment ON question_assessments(assessment_id);
CREATE INDEX IF NOT EXISTS idx_qa_question   ON question_assessments(question_id);

-- Paper intake (src/intake/). One row per uploaded file (a PDF, Word
-- doc or image of a question paper). The extracted questions live in
-- intake_questions, still as DRAFTS - nothing here has been through the
-- bank's own checks, and none of it is in `questions` yet. This is a
-- staging area a reviewer works through before any of it is promoted.
CREATE TABLE IF NOT EXISTS intake_documents (
    id            INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    filename      TEXT NOT NULL,
    file_type     TEXT,               -- 'pdf' | 'docx' | 'image' | 'other'
    extractor     TEXT,               -- which path produced the text, or why none did
    uploaded_by   TEXT,
    paper_grade   TEXT,               -- paper-level, set once, inherited by each draft
    paper_subject TEXT,
    notes         TEXT,
    -- The full text the extractor read, kept verbatim. Lets a document be
    -- re-split later - with a different split style, or by a fixed
    -- extractor - without asking for the original file again, and lets a
    -- reviewer see what was actually read when a split goes wrong.
    raw_text      TEXT,
    split_style   TEXT,
    -- 'extracted'      first pass done, nobody has looked yet
    -- 'in_review'      a reviewer has started editing
    -- 'checked'        within-batch duplicate check has been run -
    --                  ready for the assigning administrator to hand
    --                  to a teacher
    -- 'teacher_review' assigned (see assigned_to) - the teacher has not
    --                  finished their pass yet
    -- 'refer_back'     the teacher found something wrong - back with
    --                  data-entry; re-running the duplicate check
    --                  above returns it to 'checked' for reassignment
    -- 'review_done'    the teacher signed off - ready to promote
    -- 'published'      promoted into the real bank (see
    --                  promote_document()) - terminal, nothing here
    --                  changes it further
    status        TEXT NOT NULL DEFAULT 'extracted',
    -- Which teacher this paper is with, once the administrator has
    -- assigned it - see assign_document() and the `teachers` table
    -- below. NULL before assignment.
    assigned_to   TEXT,
    uploaded_at   TIMESTAMPTZ DEFAULT now()
);

-- Every distinct identity that has opened the teacher review page,
-- captured automatically (see upsert_teacher()) rather than maintained
-- by hand - the same Easy Auth header current_reviewer() already reads
-- elsewhere in this app. This is what fills the assigning
-- administrator's "which teacher" dropdown, and guarantees whatever it
-- writes into intake_documents.assigned_to is a real identity Easy Auth
-- will actually report back, rather than a hand-typed name or email
-- that might not match.
CREATE TABLE IF NOT EXISTS teachers (
    identity      TEXT PRIMARY KEY,
    display_name  TEXT,
    first_seen_at TIMESTAMPTZ DEFAULT now(),
    last_seen_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS intake_questions (
    id               INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    document_id      INTEGER NOT NULL REFERENCES intake_documents(id),
    seq              INTEGER NOT NULL,   -- order within the document
    question_number  TEXT,               -- as printed on the paper ("31", "12(a)")
    text             TEXT,
    grade            TEXT,
    subject          TEXT,
    marks            INTEGER,
    topic            TEXT,
    answer           TEXT,
    -- 'ok' | 'low'. 'low' means the extractor is least sure here (no
    -- marks found, or almost no text) - where a reviewer should look first.
    extraction_confidence TEXT DEFAULT 'ok',
    reviewed         INTEGER NOT NULL DEFAULT 0,  -- a person has confirmed/edited this row
    created_at       TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_intake_q_doc ON intake_questions(document_id);

-- A teacher's note on one question during their own review pass -
-- "Refer Back" / "Review Done" (intake_documents.status) is the paper's
-- overall verdict; a comment is what a specific question needed it for.
-- Kept as its own table, not a column on intake_questions, because a
-- question can pick up more than one comment over time and a comment
-- outlives whatever edit follows it - the same reasoning `decisions`
-- already uses for duplicate verdicts.
CREATE TABLE IF NOT EXISTS intake_question_comments (
    id            INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    question_id   INTEGER NOT NULL REFERENCES intake_questions(id),
    author        TEXT,
    comment       TEXT NOT NULL,
    created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_intake_qc_question
    ON intake_question_comments(question_id);

-- A reviewer's own attached image for a question, its answer, or one
-- MCQ option - for content that is fundamentally a picture (a diagram,
-- a photograph, a table too complex to read well as flattened text)
-- that extraction could not turn into meaningful text on its own.
-- `slot` is 'question', 'answer', or 'option:<label>' (an option's own
-- printed label from intake.extract.split_mcq_options(), e.g.
-- 'option:I') - options are never their own row (see CLAUDE.md, "MCQ
-- options live inside content"), so a label is the only thing to key
-- an option's own image on.
CREATE TABLE IF NOT EXISTS intake_question_images (
    id            INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    question_id   INTEGER NOT NULL REFERENCES intake_questions(id),
    slot          TEXT NOT NULL,
    image_blob    BYTEA NOT NULL,
    content_type  TEXT,
    created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_intake_qi_question
    ON intake_question_images(question_id);
"""


# Schema work runs once per process rather than once per connection.
# Reading scripts do not call init(), so without this an older database
# reaches a query that names a column it does not yet have.
_schema_ready = False


def _quietly_close(raw):
    try:
        if not raw.closed:
            raw.close()
    except psycopg2.Error:
        pass


class _Pool:
    """Idle connections, kept for the next caller instead of closed.

    psycopg2 ships a pool, but it only keeps a returned connection while
    fewer than `minconn` are idle, and opens `minconn` of them up front -
    so reuse costs a burst of 2.5-second connections at startup, and
    without it every returned connection is closed. This one opens
    lazily and keeps up to _POOL_MAX_IDLE.

    get() also bounds how many connections this process can have
    CHECKED OUT at once to _POOL_MAX_IDLE, not only how many sit idle -
    without that, enough concurrent load makes this process open
    connections without any limit at all, and "remaining connection
    slots are reserved for azure replication users" is what a shared
    Postgres server says once every ordinary slot is gone, possibly to
    this process alone. Blocking one caller briefly is a far smaller
    cost than that.
    """

    def __init__(self, dsn, options):
        self._dsn = dsn
        self._options = options
        self._idle = []
        self._lock = threading.Lock()
        self.closed = False
        self._checked_out = threading.Semaphore(_POOL_MAX_IDLE)

    def _acquire_raw(self):
        while True:
            with self._lock:
                item = self._idle.pop() if self._idle else None
            if item is None:
                return psycopg2.connect(self._dsn, **self._options)
            raw, released_at = item
            if raw.closed:
                continue
            if time.monotonic() - released_at > _IDLE_CHECK_SECONDS:
                # A server or firewall may have dropped it while it sat
                # idle; better one round trip now than an error in the
                # middle of a reviewer's click.
                try:
                    with raw.cursor() as cur:
                        cur.execute("SELECT 1")
                    raw.rollback()
                except psycopg2.Error:
                    _quietly_close(raw)
                    continue
            return raw

    def get(self):
        """One checked-out connection, waiting for one to free up if
        _POOL_MAX_IDLE are already out."""
        self._checked_out.acquire()
        try:
            return self._acquire_raw()
        except Exception:
            self._checked_out.release()
            raise

    def reconnect(self):
        """Swap a connection Connection._run() found dead mid-use for a
        working one - not a new checkout. The caller already holds the
        one permit its original get() acquired; calling get() again
        here would acquire a second permit that nothing would ever
        release, since only one put() follows for the whole borrow."""
        return self._acquire_raw()

    def put(self, raw, broken=False):
        try:
            if broken or raw.closed or self.closed:
                _quietly_close(raw)
                return
            try:
                status = raw.info.transaction_status
                if status == psycopg2.extensions.TRANSACTION_STATUS_UNKNOWN:
                    _quietly_close(raw)
                    return
                if status != psycopg2.extensions.TRANSACTION_STATUS_IDLE:
                    raw.rollback()
            except psycopg2.Error:
                _quietly_close(raw)
                return
            with self._lock:
                if len(self._idle) < _POOL_MAX_IDLE:
                    self._idle.append((raw, time.monotonic()))
                    return
            _quietly_close(raw)
        finally:
            self._checked_out.release()

    def discard_idle(self):
        """Close every idle connection. Used when one turns out to have
        been dropped by the server: whatever cut it - a network blip, a
        server restart - almost always cut the others sitting beside it."""
        with self._lock:
            idle, self._idle = self._idle, []
        for raw, _ in idle:
            _quietly_close(raw)

    def close(self):
        with self._lock:
            self.closed = True
        self.discard_idle()


_POOL = None
_POOL_LOCK = threading.Lock()


def _pool():
    global _POOL
    with _POOL_LOCK:
        if _POOL is None or _POOL.closed:
            # The schema is set per connection at the protocol level, so
            # a pooled connection can never be handed out still pointing
            # at whatever schema it was last used with.
            options = dict(_CONNECT_OPTIONS,
                           options=f"-c search_path={SCHEMA_NAME}")
            _POOL = _Pool(DSN, options)
        return _POOL


def _close_pool():
    global _POOL
    with _POOL_LOCK:
        if _POOL is not None:
            _POOL.close()
        _POOL = None


def _placeholders(sql):
    """`?` is SQLite's placeholder; psycopg2 wants `%s`.

    Done here rather than by rewriting every query so the SQL in this
    module stays readable, and because no statement in it contains a
    literal `?` or `%` - checked, and worth re-checking before adding a
    LIKE pattern."""
    return sql.replace("?", "%s")


class Connection:
    """psycopg2 behind sqlite3's `conn.execute(...)` shape.

    Every call site in this project was written against sqlite3's
    Connection, which runs a statement directly and hands back a cursor.
    psycopg2 has no such method - it is cursors all the way down - so
    this keeps the shape, and the rest of the module (and the callers
    outside it) did not have to change.
    """

    def __init__(self, raw, pool=None):
        self._raw = raw
        self._pool = pool
        self._released = False
        # Nothing has run on this connection yet in this unit of work, so
        # if its first statement finds it dead, replacing it and trying
        # again cannot repeat or lose anything.
        self._fresh = True
        self._autocommit = False

    def set_autocommit(self, on):
        self._autocommit = on
        self._raw.autocommit = on

    def _run(self, action):
        """Run `action(raw)`, replacing a dropped pooled connection once.

        A connection can sit in the pool looking open while the server or
        the network has already closed it - one did, half an hour into a
        test run against the Azure server. Without this the first click
        after it shows a reviewer "server closed the connection"; with it,
        the click just takes a moment longer.
        """
        try:
            result = action(self._raw)
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            if not self._fresh or self._pool is None:
                raise
            _quietly_close(self._raw)
            self._pool.discard_idle()
            self._raw = self._pool.reconnect()
            self._raw.autocommit = self._autocommit
            result = action(self._raw)
        self._fresh = False
        return result

    def execute(self, sql, params=()):
        def run(raw):
            cur = raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(_placeholders(sql), tuple(params))
            return cur
        return self._run(run)

    def executemany(self, sql, seq_of_params):
        rows = [tuple(p) for p in seq_of_params]

        def run(raw):
            cur = raw.cursor()
            cur.executemany(_placeholders(sql), rows)
            return cur
        return self._run(run)

    def raw_cursor(self):
        """A plain psycopg2 cursor, for the few places that need one -
        bulk_insert()'s execute_values, and nothing else so far."""
        self._fresh = False
        return self._raw.cursor()

    def executescript(self, sql):
        """A block of DDL, run as one statement - psycopg2 allows it, and
        it is what sqlite3's executescript() did for the schema."""
        def run(raw):
            cur = raw.cursor()
            cur.execute(sql)
            return cur
        return self._run(run)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self, broken=False):
        """Hand the connection back to the pool for the next caller."""
        if self._released:
            return
        self._released = True
        if self._pool is not None:
            self._pool.put(self._raw, broken=broken)
        else:
            _quietly_close(self._raw)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # Commit or roll back, then return the connection to the pool
        # rather than closing it: reopening one to a remote server is the
        # single most expensive thing this module does.
        broken = exc_type is not None and issubclass(
            exc_type, (psycopg2.OperationalError, psycopg2.InterfaceError))
        try:
            if exc_type is None:
                self._raw.commit()
            elif not broken:
                self._raw.rollback()
        except psycopg2.Error:
            broken = True
            raise
        finally:
            self.close(broken=broken)
        return False


def connect():
    global _schema_ready, VECTOR_STORAGE
    if DSN is None:
        raise RuntimeError(
            "No database configured. Set EDSIDER_DB to a PostgreSQL "
            "connection string before running anything, e.g.:\n\n"
            '    $env:EDSIDER_DB = '
            '"postgresql://edsider_ai:PASSWORD@localhost:5432/edsider_ai"\n\n'
            "or call store.use_database(dsn) directly."
        )
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", SCHEMA_NAME):
        raise RuntimeError(f"not a usable schema name: {SCHEMA_NAME!r}")

    pool = _pool()
    conn = Connection(pool.get(), pool)
    if _schema_ready:
        return conn

    # Once per process from here on. Setting up is done outside a
    # transaction, deliberately: PostgreSQL rolls back DDL on failure, so
    # probing for pgvector (_vector_storage, which expects to fail where
    # the extension is not allowed) took CREATE SCHEMA down with it, and
    # the next statement reported "no schema has been selected".
    conn.set_autocommit(True)
    try:
        if SCHEMA_NAME != "public":
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA_NAME}"')
        if VECTOR_STORAGE is None:
            VECTOR_STORAGE = _vector_storage(conn)
    finally:
        conn.set_autocommit(False)

    # Checking costs one catalogue query; re-running the DDL against the
    # real bank on Azure took 58 seconds - every CREATE TABLE and ALTER
    # TABLE takes a lock, and each is a round trip - on every start of
    # the app, for a schema that had not changed.
    if not _schema_is_current(conn):
        _apply_schema(conn)
    conn.commit()
    _schema_ready = True
    return conn


def _schema_is_current(conn):
    """Whether every table and every migrated column already exists."""
    have = {(r["table_name"], r["column_name"]) for r in conn.execute(
        """SELECT table_name, column_name FROM information_schema.columns
            WHERE table_schema = current_schema()""")}
    tables = {t for t, _ in have}
    wanted = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)",
                            SCHEMA_TEMPLATE))
    return wanted <= tables and all(
        (table, column) in have for table, column, _ in MIGRATIONS)


# Columns added after a database already existed. CREATE TABLE IF NOT
# EXISTS silently does nothing to a table that is already there, so an
# older database would keep the old shape and every insert would fail.
MIGRATIONS = [
    ("questions", "title", "TEXT"),
    ("questions", "shape", "TEXT NOT NULL DEFAULT 'standalone'"),
    # Which parent (passage/stimulus) a sub-question belongs to - set on
    # the CHILD row. Nothing at import time knows this about a child in
    # isolation (a row only lists its OWN id); it is filled in after the
    # fact by link_children_to_parents(), reading it off the parent's own
    # children_raw.
    ("questions", "parent_id", "TEXT"),
    # The parent's raw, EdSider-supplied comma list of its own children's
    # ids ("837,838,839"), stored verbatim on the PARENT row. Kept around
    # (rather than only used transiently at import time) so
    # link_children_to_parents() can be re-run at any time, in any order
    # relative to when the children themselves arrive, and always recover
    # the same links - a child synced before its parent, or re-synced
    # later, is repaired (or stays linked - see save()) the next time it
    # runs.
    ("questions", "children_raw", "TEXT"),
    # A JSON-encoded (only_in_a, only_in_b) word list - see
    # guards.candidate_contrast_terms(). Populated only when a reviewer
    # rejects a pair the system flagged ("Not a duplicate"), as raw
    # material for later promoting real entries into
    # guards.CONTRAST_GROUPS. Never read automatically by anything -
    # a person reviews it first.
    ("decisions", "candidate_contrast_terms", "TEXT"),
    # Added once papers uploaded under an earlier extractor needed
    # re-splitting without being re-uploaded (see intake_documents).
    ("intake_documents", "raw_text", "TEXT"),
    ("intake_documents", "split_style", "TEXT"),
    # A paper in parts - Part I MCQ, Part II structured - see
    # intake.extract.structure(). `sections` is the JSON list of parts
    # (label, heading, kind, instructions), `section` the label of the
    # part each draft belongs to.
    ("intake_documents", "paper_title", "TEXT"),
    ("intake_documents", "sections", "TEXT"),
    ("intake_questions", "section", "TEXT"),
    # A structured question is a stem plus its parts, and each part is
    # what a teacher marks and answers - see intake.extract._split_parts().
    # `parent_seq` is the parent draft's seq within the same document (not
    # a row id), `depth` 0 for the question itself, 1 for its parts, 2 for
    # their own sub-parts. This mirrors EdSider's own parent/child rows.
    ("intake_questions", "parent_seq", "INTEGER"),
    ("intake_questions", "depth", "INTEGER NOT NULL DEFAULT 0"),
    # question.answer on EdSider's side is not always plain text: for an
    # MCQ it is a comma-separated list of per-option percentage weights
    # ("100,0,0,0"), index-aligned with the options embedded in `content`;
    # for an essay/submission question it is a free-text model answer. A
    # blob reference is the third case - is_answer_blob marks it, so a
    # reader never mistakes an opaque reference for literal answer text.
    ("questions", "is_answer_blob", "INTEGER DEFAULT 0"),
    # The uploaded file's own bytes, for the paper-preview pane - see
    # create_intake_document(). NULL for anything uploaded before this
    # existed.
    ("intake_documents", "file_blob", "BYTEA"),
    # A teacher's own tick, separate from `reviewed` (the data-entry
    # person's) - the two roles check a question for different things,
    # so one ticking their box must not silently tick the other's.
    ("intake_questions", "teacher_reviewed", "INTEGER NOT NULL DEFAULT 0"),
    # Which teacher a checked paper has been handed to - see
    # assign_document() and the `teachers` table.
    ("intake_documents", "assigned_to", "TEXT"),
]

# A question can only be compared on its text when the text IS the whole
# question. That rules out two groups, both measured on the real bank:
# child rows that depend on a parent for their context, and rows whose
# content is an image or equation - "Simplify" appears 110 times, each
# time against a different expression that lives in an attachment.
COMPARABLE = "shape = 'standalone' AND has_figure = 0"

# A sub-question becomes safe to compare once it is LINKED to its parent -
# not on its own text (meaningless alone - "how many links" fits six
# different food chains) but as a way to tell WHICH sub-questions must
# never be compared to each other (same parent_id - a comprehension set
# is supposed to differ) from the one thing actually worth checking:
# the same sub-question reused under a DIFFERENT parent_id, on a
# different paper. See dedupe.find_cross_passage_duplicates().
COMPARABLE_CHILD = ("shape = 'sub-question' AND has_figure = 0 "
                    "AND parent_id IS NOT NULL")


def use_database(dsn, schema=None):
    """Point every subsequent connection at a different database.

    Different databases exist for different purposes (the real bank, a
    staging copy, the throwaway schema a test works in), and the UI names
    the one it has open. Keeping that here means callers never touch DSN
    or the schema flag themselves.

    `dsn=None` resets to "not configured" - the same state DSN starts in
    when EDSIDER_DB was never set, so a test can put back whatever it
    found, None included.
    """
    global DSN, SCHEMA_NAME, _schema_ready, VECTOR_STORAGE
    _close_pool()
    DSN = str(dsn) if dsn is not None else None
    if schema is not None:
        SCHEMA_NAME = schema
    # A different database may store its fingerprints differently, and
    # certainly needs its schema checked again.
    _schema_ready = False
    VECTOR_STORAGE = None
    _VECTORS.clear()
    return DSN


def display_name():
    """What the UI's sidebar and the service's /health show - the
    database, and the schema when it is not the default one."""
    if DSN is None:
        return None
    try:
        name = psycopg2.extensions.parse_dsn(DSN).get("dbname") or DSN
    except psycopg2.Error:
        name = DSN
    return name if SCHEMA_NAME == "public" else f"{name} ({SCHEMA_NAME})"


def _vector_storage(conn):
    """Whether this database stores fingerprints as pgvector or bytea.

    An existing embeddings table decides it - changing the column type
    under rows that are already there is a migration, not a startup
    guess. Otherwise pgvector is used where the extension can be enabled
    (it brings the indexes that will eventually replace the O(n^2)
    sweep), and bytea where it cannot, which is the usual case on a
    developer machine.
    """
    existing = conn.execute(
        """SELECT format_type(a.atttypid, a.atttypmod) AS type
             FROM pg_attribute a
             JOIN pg_class c     ON c.oid = a.attrelid
             JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relname = 'embeddings' AND a.attname = 'vector'
              AND n.nspname = current_schema()""").fetchone()
    if existing:
        return "vector" if existing["type"].startswith("vector") else "bytea"

    try:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        return "vector"
    except psycopg2.Error:
        # No extension, or no rights to create one. Not fatal: bytea
        # holds the same numbers in the same space, it just cannot be
        # indexed for nearest-neighbour search.
        return "bytea"


def _apply_schema(conn):
    # connect() has already settled this, outside any transaction.
    conn.executescript(_schema_sql(VECTOR_STORAGE or "bytea"))
    for table, column, decl in MIGRATIONS:
        # PostgreSQL can say "add it if it is missing" itself, so this no
        # longer has to read the table's columns first.
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {decl}")


def _encode_vector(values):
    """A list of floats, in whichever form this database stores them."""
    numbers = [float(v) for v in values]
    if VECTOR_STORAGE == "vector":
        return "[" + ",".join(repr(v) for v in numbers) + "]"
    return psycopg2.Binary(array.array("f", numbers).tobytes())


def _decode_vector(raw):
    """Back to a list of floats, whichever way it was stored - pgvector
    hands back its own text form, bytea hands back bytes, and a database
    written before either still holds JSON text."""
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray, memoryview)):
        out = array.array("f")
        out.frombytes(bytes(raw))
        return list(out)
    if isinstance(raw, str):
        return json.loads(raw)
    return list(raw)


def _with_vectors(cursor):
    """Rows from a query that selects `vector`, decoded and plain."""
    rows = []
    for row in cursor:
        row = dict(row)
        row["vector"] = _decode_vector(row.get("vector"))
        rows.append(row)
    return rows


# ------------------------------------------------------ fingerprint cache
#
# The bank's fingerprints are 49 MB for the 16,084 comparable questions,
# and from this laptop to EdSider's Azure server that downloaded at about
# 1 MB/s - 55 of the 95 seconds the app spent loading, on every start.
# The query itself took 0.7 s.
#
# A fingerprint never changes for a given text and model: it is keyed by
# content_hash, and the model is deterministic. So each one is downloaded
# once, kept in memory, and kept on local disk under CACHE_DIR; a later
# load fetches only the fingerprints it has not seen. The database stays
# the only source of truth - the cache holds nothing the database does
# not, and deleting it costs one slow start, nothing more.

_VECTORS = {}               # model -> {content_hash: float32 array}
_VECTORS_DIRTY = set()      # models whose disk copy holds a stale entry
_VECTORS_LOCK = threading.Lock()


def _as_float32(raw):
    if isinstance(raw, (bytes, bytearray, memoryview)):
        return np.frombuffer(bytes(raw), dtype=np.float32)
    if isinstance(raw, str):
        return np.asarray(json.loads(raw), dtype=np.float32)
    return np.asarray(raw, dtype=np.float32)


def _cache_file(model):
    if CACHE_DIR is None:
        return None
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", model)
    return Path(CACHE_DIR) / f"vectors-{safe}.npz"


def _load_vector_cache(model):
    """This model's cached fingerprints - from memory, else from disk."""
    if model in _VECTORS:
        return _VECTORS[model]
    cache = {}
    path = _cache_file(model)
    if path is not None and path.is_file():
        try:
            with np.load(path) as data:
                hashes, matrix = data["hashes"], data["matrix"]
                cache = {str(h): matrix[i] for i, h in enumerate(hashes)}
        except (OSError, ValueError, KeyError):
            # A half-written or foreign file is not worth failing over:
            # everything in it can be fetched again.
            cache = {}
    _VECTORS[model] = cache
    return cache


def _save_vector_cache(model, cache):
    path = _cache_file(model)
    if path is None or not cache:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        hashes = np.array(list(cache.keys()))
        matrix = np.stack([cache[h] for h in cache.keys()]).astype(np.float32)
        # Written beside the real file and swapped in, so the app and the
        # API reading the same cache never see half a file.
        partial = path.parent / (path.stem + ".partial.npz")
        np.savez(partial, hashes=hashes, matrix=matrix)
        os.replace(partial, path)
    except (OSError, ValueError):
        # A read-only disk, or fingerprints of differing lengths from a
        # changed model: the in-memory cache still works for this run.
        pass


def _cached_vectors(conn, model, wanted):
    """{content_hash: float32 array} for every wanted hash that has a
    fingerprint, fetching from the database only what is not cached."""
    with _VECTORS_LOCK:
        cache = _load_vector_cache(model)
        missing = [h for h in wanted if h not in cache]
        fetched = {}
        # In chunks, so one query never carries a 30,000-item parameter.
        for start in range(0, len(missing), 2000):
            chunk = missing[start:start + 2000]
            for r in conn.execute(
                    """SELECT content_hash, vector FROM embeddings
                        WHERE model = ? AND content_hash = ANY(?)""",
                    (model, chunk)):
                fetched[r["content_hash"]] = _as_float32(r["vector"])
        if fetched:
            cache.update(fetched)
        if fetched or model in _VECTORS_DIRTY:
            _save_vector_cache(model, cache)
            _VECTORS_DIRTY.discard(model)
        return {h: cache[h] for h in wanted if h in cache}


def _attach_vectors(conn, rows, model):
    """Give each row its fingerprint, from the cache where possible.
    A row whose fingerprint has gone since the query ran is dropped
    rather than handed on without one."""
    rows = [dict(r) for r in rows]
    found = _cached_vectors(conn, model, {r["content_hash"] for r in rows})
    out = []
    for r in rows:
        vector = found.get(r["content_hash"])
        if vector is not None:
            r["vector"] = vector
            out.append(r)
    return out


def forget_cached_vectors():
    """Drop the in-memory fingerprint cache - for a caller that knows the
    database behind it has been replaced wholesale."""
    with _VECTORS_LOCK:
        _VECTORS.clear()


def init():
    """Kept for callers that want the schema created explicitly.
    connect() already does this once per process."""
    with connect() as conn:
        _apply_schema(conn)


def encode_vector(values):
    """A fingerprint in this database's own storage form - for the one-off
    migration, which writes embeddings without going through the model."""
    return _encode_vector(values)


def bulk_insert(conn, table, columns, rows, page_size=1000):
    """Insert many rows in one round trip, skipping any that are already
    there.

    For the migration from the old SQLite bank: 102,000 questions one
    INSERT at a time is minutes of waiting, almost all of it spent on
    round trips rather than on the database.
    """
    names = ", ".join(columns)
    psycopg2.extras.execute_values(
        conn.raw_cursor(),
        f"INSERT INTO {table} ({names}) VALUES %s ON CONFLICT DO NOTHING",
        list(rows), page_size=page_size,
    )


def save(conn, q):
    # ON CONFLICT DO UPDATE, not INSERT OR REPLACE - deliberately. A
    # plain REPLACE deletes and re-inserts the whole row, which would
    # wipe parent_id every time a child question is re-synced or
    # re-imported (parent_id is never part of `q`; it comes only from
    # link_children_to_parents()). Naming every updated column instead
    # means parent_id is simply never touched here, so a link once made
    # survives any number of re-saves of either the child or its parent.
    conn.execute(
        """INSERT INTO questions
           (id, text, title, grade, subject, topic, marks, answer,
            is_answer_blob, language, contributor, source_file,
            content_hash, has_figure, shape, children_raw)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
               text=excluded.text, title=excluded.title,
               grade=excluded.grade, subject=excluded.subject,
               topic=excluded.topic, marks=excluded.marks,
               answer=excluded.answer,
               is_answer_blob=excluded.is_answer_blob,
               language=excluded.language,
               contributor=excluded.contributor,
               source_file=excluded.source_file,
               content_hash=excluded.content_hash,
               has_figure=excluded.has_figure, shape=excluded.shape,
               children_raw=excluded.children_raw""",
        (
            q["id"], q["text"], q.get("title"),
            q.get("grade"), q.get("subject"),
            q.get("topic"), q.get("marks"), q.get("answer"),
            1 if q.get("is_answer_blob") else 0,
            q.get("language", "en"), q.get("contributor"),
            q.get("source_file"), q["content_hash"],
            1 if q.get("has_figure") else 0,
            q.get("shape", "standalone"),
            q.get("children_raw"),
        ),
    )


def link_children_to_parents(conn):
    """Recompute parent_id on every child, from every parent's own
    children_raw.

    Order-independent and safe to re-run after any import or sync: a
    child that did not exist yet on an earlier run gets linked the next
    time this runs after it arrives, and save() leaves parent_id alone
    on a re-save (see the comment there), so a link once made survives
    a later re-sync of either row.

    Writes only a child whose parent_id is actually missing or wrong -
    on a steady-state re-run, where every link from last time is still
    correct, that is zero rows rather than one UPDATE per child in the
    WHOLE bank every single call. Measured live: with thousands of
    parent rows across the real bank, the old unconditional version was
    the dominant cost of running sync.py when EdSider had nothing new
    at all - every "nothing to do" sync still rewrote every child.

    Returns how many child rows carry a parent_id after this call.
    """
    parents = conn.execute(
        "SELECT id, children_raw FROM questions "
        "WHERE shape = 'parent' AND children_raw IS NOT NULL"
    ).fetchall()

    wanted = {}
    for parent in parents:
        for cid in (c.strip() for c in parent["children_raw"].split(",")):
            if cid:
                wanted[cid] = parent["id"]

    if wanted:
        child_ids = list(wanted)
        current = {
            r["id"]: r["parent_id"]
            for r in conn.execute(
                "SELECT id, parent_id FROM questions WHERE id = ANY(?)",
                (child_ids,))
        }
        changed = [(pid, cid) for cid, pid in wanted.items()
                  if current.get(cid) != pid]
        if changed:
            conn.executemany(
                "UPDATE questions SET parent_id = ? WHERE id = ?", changed)

    return conn.execute(
        "SELECT COUNT(*) AS n FROM questions WHERE parent_id IS NOT NULL"
    ).fetchone()["n"]


def promote_document(conn, doc_id):
    """Copy a fully reviewed paper's questions into the real bank
    (`questions`) - the step paper intake has stopped short of since it
    was first built (see CLAUDE.md's "Paper intake" section). Only
    allowed once a paper has been through all three people this
    workflow requires: data-entry's own proofread, the administrator's
    assignment, and the teacher's sign-off (`status == 'review_done'`).

    Promoted rows get their own id namespace ("intake-{doc}-{seq}"), so
    they can never collide with a real EdSider id, and keep their
    parent/child shape - link_children_to_parents() does the actual
    linking afterwards, the same as a real sync would. A stem with no
    text of its own is still promoted if it has children (its parent_id
    link would otherwise point nowhere); a genuinely empty leaf is
    skipped, the same as COMPARABLE already excludes text-free rows
    elsewhere in this codebase.

    Marks the document 'published' when done, so this cannot silently
    run twice, and returns how many rows were promoted.
    """
    doc = get_intake_document(conn, doc_id)
    if doc is None:
        raise ValueError(f"no such intake document: {doc_id}")
    if doc["status"] != "review_done":
        raise ValueError(
            f"cannot promote a paper before its review is done "
            f"(status is {doc['status']!r})")

    drafts = intake_questions(conn, doc_id)
    ids = {d["seq"]: f"intake-{doc_id}-{d['seq']}" for d in drafts}
    children_of = {}
    for d in drafts:
        if d["parent_seq"] is not None:
            children_of.setdefault(d["parent_seq"], []).append(d["seq"])

    promoted = 0
    for d in drafts:
        text = (d["text"] or "").strip()
        kids = children_of.get(d["seq"], [])
        if not text and not kids:
            continue
        shape = ("parent" if kids
                else "sub-question" if d["parent_seq"] is not None
                else "standalone")
        save(conn, {
            "id": ids[d["seq"]],
            "text": text,
            "grade": d["grade"],
            "subject": d["subject"],
            "topic": d["topic"],
            "marks": d["marks"],
            "answer": d["answer"],
            "is_answer_blob": False,
            "contributor": doc["uploaded_by"] or "paper intake",
            "source_file": doc["filename"],
            "content_hash": content_hash(text),
            "shape": shape,
            "children_raw": ",".join(ids[k] for k in kids) if kids else None,
        })
        promoted += 1

    if promoted:
        link_children_to_parents(conn)
    update_intake_document(conn, doc_id, status="published")
    return promoted


def count(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM questions").fetchone()["n"]


def all_questions(conn):
    return conn.execute("SELECT * FROM questions ORDER BY id").fetchall()


def get(conn, qid):
    return conn.execute(
        "SELECT * FROM questions WHERE id = ?", (qid,)
    ).fetchone()


def get_watermark(conn, source):
    """The highest id already pulled from `source`. 0 if never synced."""
    row = conn.execute(
        "SELECT last_id FROM sync_state WHERE source = ?", (source,)
    ).fetchone()
    return row["last_id"] if row else 0


def set_watermark(conn, source, last_id):
    conn.execute(
        """INSERT INTO sync_state (source, last_id, last_synced_at)
           VALUES (?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(source) DO UPDATE SET
               last_id = excluded.last_id,
               last_synced_at = excluded.last_synced_at""",
        (source, last_id),
    )


def exact_duplicate_groups(conn):
    """Questions sharing a normalised hash - these are exact duplicates.

    Comparable rows only. Counting the rest reports noise as a finding:
    on the real bank the parent rows alone are 43% redundant, because
    "Fill in the blanks" is repeated on every paper that uses it.
    """
    return conn.execute(
        f"""SELECT content_hash,
                   COUNT(*)             AS copies,
                   string_agg(id, ',')  AS ids,
                   MIN(text)            AS sample
            FROM questions
            WHERE {COMPARABLE}
            GROUP BY content_hash
            HAVING COUNT(*) > 1
            ORDER BY copies DESC, ids"""
    ).fetchall()


def find_by_hash(conn, content_hash, exclude_id=None):
    """Questions whose normalised text hashes identically.

    Pass 1 for a single new question. This is an indexed lookup on
    idx_hash rather than a scan, so it costs the same whether the bank
    holds thirty questions or twenty thousand.
    """
    if exclude_id is None:
        return conn.execute(
            "SELECT * FROM questions WHERE content_hash = ? ORDER BY id",
            (content_hash,),
        ).fetchall()
    return conn.execute(
        """SELECT * FROM questions
           WHERE content_hash = ? AND id != ? ORDER BY id""",
        (content_hash, exclude_id),
    ).fetchall()


def save_embedding(conn, content_hash, model, vector):
    """Store a fingerprint. `vector` is any sequence of floats."""
    conn.execute(
        """INSERT INTO embeddings (content_hash, model, vector)
           VALUES (?,?,?)
           ON CONFLICT(content_hash) DO UPDATE SET
               model = excluded.model, vector = excluded.vector""",
        (content_hash, model, _encode_vector(vector)),
    )
    # A re-embedded text must not keep being served its old fingerprint
    # from the cache. New texts - the usual case - are not in it at all.
    with _VECTORS_LOCK:
        cached = _VECTORS.get(model)
        if cached is not None and cached.pop(content_hash, None) is not None:
            _VECTORS_DIRTY.add(model)


def get_embedding(conn, content_hash, model):
    row = conn.execute(
        "SELECT vector FROM embeddings WHERE content_hash = ? AND model = ?",
        (content_hash, model),
    ).fetchone()
    return _decode_vector(row["vector"]) if row else None


def questions_needing_embedding(conn, model, language=None):
    """Distinct texts that have no fingerprint for this model yet.

    Only comparable rows - embedding a parent's "Fill in the blanks" or a
    child that makes no sense alone costs time and buys nothing.

    `language` narrows to one language's rows, e.g. to embed English
    first and defer Sinhala/Tamil - the E5 model claims multilingual
    support but that claim is still unmeasured, so scoping what gets
    embedded is how the two get worked on separately without any other
    code needing to know about it: comparable_questions() joins on
    embeddings, so an un-embedded language simply never appears in a
    duplicate check until this is run again without the filter.
    """
    params = [model]
    lang_filter = ""
    if language is not None:
        lang_filter = "AND q.language = ?"
        params.append(language)
    return conn.execute(
        f"""SELECT DISTINCT q.content_hash, MIN(q.text) AS text
            FROM questions q
            LEFT JOIN embeddings e
              ON e.content_hash = q.content_hash AND e.model = ?
            WHERE e.content_hash IS NULL AND {COMPARABLE} {lang_filter}
            GROUP BY q.content_hash""",
        params,
    ).fetchall()


def children_needing_embedding(conn, model, language=None):
    """Same idea as questions_needing_embedding(), over linked
    sub-questions instead of standalone rows.

    A separate pass, not a broader filter on the existing one: a
    sub-question is only worth fingerprinting once it is linked to its
    parent (see COMPARABLE_CHILD) - fingerprinting one that is not
    linked would let it into a comparison where it has no way to be
    correctly excluded from its own siblings.
    """
    params = [model]
    lang_filter = ""
    if language is not None:
        lang_filter = "AND q.language = ?"
        params.append(language)
    return conn.execute(
        f"""SELECT DISTINCT q.content_hash, MIN(q.text) AS text
            FROM questions q
            LEFT JOIN embeddings e
              ON e.content_hash = q.content_hash AND e.model = ?
            WHERE e.content_hash IS NULL AND {COMPARABLE_CHILD} {lang_filter}
            GROUP BY q.content_hash""",
        params,
    ).fetchall()


def linked_children_with_embeddings(conn, model):
    """Comparable sub-questions, each carrying its parent_id - the pool
    dedupe.find_cross_passage_duplicates() runs over."""
    return _attach_vectors(conn, conn.execute(
        f"""SELECT q.*
            FROM questions q
            JOIN embeddings e
              ON e.content_hash = q.content_hash AND e.model = ?
            WHERE {COMPARABLE_CHILD}
            ORDER BY q.id""",
        (model,),
    ), model)


def questions_with_embeddings(conn, model):
    """Every question joined to its fingerprint, comparable or not."""
    return _attach_vectors(conn, conn.execute(
        """SELECT q.*
           FROM questions q
           JOIN embeddings e
             ON e.content_hash = q.content_hash AND e.model = ?
           ORDER BY q.id""",
        (model,),
    ), model)


def comparable_questions(conn, model):
    """The rows a duplicate check may actually score.

    This is what every caller wants. `questions_with_embeddings` remains
    for the rare case that needs the unfiltered set.
    """
    # The join only filters to fingerprinted rows; the fingerprints
    # themselves come through the cache (see _cached_vectors).
    return _attach_vectors(conn, conn.execute(
        f"""SELECT q.*
            FROM questions q
            JOIN embeddings e
              ON e.content_hash = q.content_hash AND e.model = ?
            WHERE {COMPARABLE}
            ORDER BY q.id""",
        (model,),
    ), model)


def shape_counts(conn):
    """How the bank breaks down, and how much of it is comparable."""
    return conn.execute(
        f"""SELECT shape,
                   COUNT(*)                                   AS total,
                   SUM(has_figure)                            AS figures,
                   SUM(CASE WHEN {COMPARABLE} THEN 1 ELSE 0 END) AS comparable
            FROM questions
            GROUP BY shape
            ORDER BY total DESC"""
    ).fetchall()


def embedding_count(conn):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM embeddings"
    ).fetchone()["n"]


def record_decision(conn, a, b, verdict, decided_by,
                    similarity=None, guard_flags=None,
                    candidate_contrast_terms=None):
    """`candidate_contrast_terms` is (only_in_a, only_in_b) from
    guards.candidate_contrast_terms() - only meaningful, and only ever
    passed by a caller, when verdict is 'distinct': what vocabulary
    might explain why a pair the system flagged turned out not to be a
    duplicate. Stored as JSON for a person to review later; nothing
    reads it back automatically.

    Every click is its own row, never an update: a reviewer who changes
    their mind leaves a history, and current_decisions() reads the
    latest one per pair."""
    # (only_in_a, only_in_b) is a non-empty tuple even when both lists
    # are empty - two identical texts - and stored "[[], []]" would read
    # as captured words that are not there.
    has_terms = candidate_contrast_terms and any(candidate_contrast_terms)
    cur = conn.execute(
        """INSERT INTO decisions
           (question_a, question_b, similarity, guard_flags, verdict,
            decided_by, candidate_contrast_terms)
           VALUES (?,?,?,?,?,?,?)
           RETURNING id""",
        (a, b, similarity, guard_flags, verdict, decided_by,
         json.dumps(candidate_contrast_terms) if has_terms else None),
    )
    return cur.fetchone()["id"]


def pair_key(a, b):
    """One key for a pair whichever side each question was shown on - a
    sweep can list A-B today and B-A after the bank changes."""
    return tuple(sorted((str(a), str(b))))


def current_decisions(conn):
    """The latest verdict per pair, keyed by pair_key().

    'skipped' is left out: a skip is not a verdict, so the pair still
    needs one and must keep coming back for review."""
    latest = {}
    for r in conn.execute(
            "SELECT * FROM decisions WHERE verdict != 'skipped' ORDER BY id"):
        latest[pair_key(r["question_a"], r["question_b"])] = dict(r)
    return latest


def list_decisions(conn, verdict=None):
    """Current decisions, newest first, with both questions' text - what
    a reviewer needs to see to check a verdict without looking the ids
    up. `verdict` narrows to 'duplicate' or 'distinct'."""
    decided = current_decisions(conn)
    ids = {i for key in decided for i in key}
    texts = {}
    if ids:
        marks = ",".join("?" * len(ids))
        for r in conn.execute(
                f"SELECT id, text, grade, subject FROM questions "
                f"WHERE id IN ({marks})", sorted(ids)):
            texts[r["id"]] = dict(r)
    out = []
    for d in decided.values():
        if verdict and d["verdict"] != verdict:
            continue
        qa = texts.get(d["question_a"], {})
        qb = texts.get(d["question_b"], {})
        terms = (json.loads(d["candidate_contrast_terms"])
                 if d["candidate_contrast_terms"] else [[], []])
        out.append({**d,
                    "text_a": qa.get("text"), "text_b": qb.get("text"),
                    "grade": qa.get("grade"), "subject": qa.get("subject"),
                    "only_in_a": terms[0], "only_in_b": terms[1]})
    out.sort(key=lambda d: d["id"], reverse=True)
    return out


def undo_decision(conn, a, b):
    """Send a pair back for review. Removes every verdict on it, in
    either order - leaving an older one would make it resurface under a
    verdict the reviewer just took back. A person clicks this; nothing
    calls it on its own."""
    x, y = pair_key(a, b)
    cur = conn.execute(
        """DELETE FROM decisions
           WHERE (question_a = ? AND question_b = ?)
              OR (question_a = ? AND question_b = ?)""",
        (x, y, y, x))
    return cur.rowcount


def table_schema(conn, table):
    """A table's columns as a CREATE TABLE statement, migrated columns
    included - shown on the decisions page so a reviewer can see exactly
    what is stored.

    PostgreSQL keeps no original DDL text (SQLite did, in sqlite_master),
    so this rebuilds it from the catalogue - which is in fact truer: it
    describes the table as it stands now, not as it was first written.
    """
    columns = conn.execute(
        """SELECT column_name, data_type, character_maximum_length AS len,
                  is_nullable, column_default
             FROM information_schema.columns
            WHERE table_name = ? AND table_schema = current_schema()
            ORDER BY ordinal_position""", (table,)).fetchall()
    if not columns:
        return None

    lines = []
    for c in columns:
        kind = c["data_type"].upper()
        if c["len"]:
            kind += f"({c['len']})"
        line = f"    {c['column_name']:<22} {kind}"
        if c["column_default"]:
            line += f" DEFAULT {c['column_default']}"
        if c["is_nullable"] == "NO":
            line += " NOT NULL"
        lines.append(line)
    return f"CREATE TABLE {table} (\n" + ",\n".join(lines) + "\n);"


def save_assessment_link(conn, link_id, assessment_id, question_id,
                         question_number=None, mark_allocation=None,
                         section_id=None, added_by=None):
    """Upsert one row from EdSider's assessment.assessment_question.

    Keyed on link_id - their own assessment_question_id - so a re-synced
    row replaces itself instead of duplicating. Same idempotency
    reasoning as save()'s content_hash keying and set_watermark()'s
    ON CONFLICT.
    """
    conn.execute(
        """INSERT INTO question_assessments
           (link_id, assessment_id, question_id, question_number,
            mark_allocation, section_id, added_by)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(link_id) DO UPDATE SET
               assessment_id=excluded.assessment_id,
               question_id=excluded.question_id,
               question_number=excluded.question_number,
               mark_allocation=excluded.mark_allocation,
               section_id=excluded.section_id,
               added_by=excluded.added_by""",
        (link_id, assessment_id, question_id, question_number,
         mark_allocation, section_id, added_by),
    )


def assessment_ids(conn):
    """Every assessment with at least one linked, comparable, fingerprinted
    question - what a within-paper sweep iterates over."""
    return [r["assessment_id"] for r in conn.execute(
        "SELECT DISTINCT assessment_id FROM question_assessments"
    ).fetchall()]


def questions_in_assessment(conn, assessment_id, model):
    """Comparable, fingerprinted questions on one paper - the group a
    within-assessment duplicate check runs over.

    Small (tens of rows, not tens of thousands), unlike the whole-bank
    sweep - which is exactly why the guard layer is skippable here: a
    paper repeating the same MCQ is worth a person's eyes even if a
    guard would soften it at bank scale.
    """
    return _attach_vectors(conn, conn.execute(
        f"""SELECT q.*, qa.question_number
            FROM question_assessments qa
            JOIN questions  q ON q.id = qa.question_id
            JOIN embeddings e ON e.content_hash = q.content_hash
                              AND e.model = ?
            WHERE qa.assessment_id = ? AND {COMPARABLE}
            ORDER BY qa.question_number""",
        (model, assessment_id),
    ), model)


def questions_in_assessments(conn, model):
    """Every paper's comparable, fingerprinted questions, in one query -
    {assessment_id: [row, ...]}, each paper's rows in question order.

    The within-paper page used to call questions_in_assessment() once per
    paper. Against a remote server that is 1,960 round trips at about
    0.47 s each - a quarter of an hour - for what one query returns in
    seconds. A question on two papers appears under both, as its own row.
    """
    rows = _attach_vectors(conn, conn.execute(
        f"""SELECT q.*, qa.assessment_id, qa.question_number
            FROM question_assessments qa
            JOIN questions  q ON q.id = qa.question_id
            JOIN embeddings e ON e.content_hash = q.content_hash
                              AND e.model = ?
            WHERE {COMPARABLE}
            ORDER BY qa.assessment_id, qa.question_number""",
        (model,),
    ), model)
    groups = {}
    for r in rows:
        groups.setdefault(r.pop("assessment_id"), []).append(r)
    return groups


def assessments_for_question(conn, question_id):
    """Which papers a question appears on - context for a cross-
    assessment match ("also appears on assessment 6, question 3"),
    where reuse with different values is expected rather than a
    problem."""
    return conn.execute(
        """SELECT assessment_id, question_number FROM question_assessments
           WHERE question_id = ? ORDER BY assessment_id""",
        (question_id,),
    ).fetchall()


def assessment_links(conn):
    """{question_id: [{assessment_id, question_number}, ...]} for the
    whole bank, in one query.

    The review pages note "Also appears on assessment(s)" under every
    question they show, and asked for it one question at a time - 100
    round trips for a page of 50 pairs, over half a minute to Azure. All
    101,236 links come back in about three seconds instead.
    """
    links = {}
    for r in conn.execute(
            """SELECT question_id, assessment_id, question_number
                 FROM question_assessments
                ORDER BY question_id, assessment_id"""):
        links.setdefault(r["question_id"], []).append(
            {"assessment_id": r["assessment_id"],
             "question_number": r["question_number"]})
    return links


# ------------------------------------------------------- paper intake
#
# Draft questions extracted from an uploaded paper, before a person has
# checked them and before anything is promoted into `questions`. Kept
# here because store.py is the only module that touches the database -
# the intake package (src/intake/) calls these, it does not open its
# own connection.

def create_intake_document(conn, filename, file_type=None, extractor=None,
                           uploaded_by=None, paper_grade=None,
                           paper_subject=None, raw_text=None,
                           split_style=None, paper_title=None,
                           sections=None, file_blob=None):
    """Record one uploaded file. Returns its new id.

    `sections` is the paper's list of parts from
    intake.extract.structure(), kept as JSON - read it back with
    intake_sections().

    `file_blob` is the uploaded file's own bytes (PDF/Word/image),
    verbatim. Extraction used to read it from a temp file and delete it
    once done, keeping only the extracted text - which meant a reviewer
    had nowhere to check a suspect OCR read against the original page.
    Kept here, not on disk: the same reasoning as fingerprints living in
    PostgreSQL rather than a local file - one source of truth, and it
    survives an app restart.
    """
    cur = conn.execute(
        """INSERT INTO intake_documents
           (filename, file_type, extractor, uploaded_by, paper_grade,
            paper_subject, raw_text, split_style, paper_title, sections,
            file_blob)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           RETURNING id""",
        (filename, file_type, extractor, uploaded_by, paper_grade,
         paper_subject, raw_text, split_style, paper_title,
         json.dumps(sections) if sections is not None else None,
         psycopg2.Binary(file_blob) if file_blob is not None else None),
    )
    return cur.fetchone()["id"]


def intake_file_blob(conn, doc_id):
    """The uploaded file's own bytes, for the paper-preview pane. None
    for a document uploaded before this existed, or a re-split that has
    since replaced the drafts (the file itself is untouched by that)."""
    row = conn.execute(
        "SELECT file_blob FROM intake_documents WHERE id = ?", (doc_id,)
    ).fetchone()
    if row is None or row["file_blob"] is None:
        return None
    return bytes(row["file_blob"])


def intake_sections(doc):
    """An uploaded paper's parts, or [] for one read before papers were
    split into parts. `doc` is an intake_documents row or a dict of one."""
    raw = doc["sections"] if "sections" in doc.keys() else None
    return json.loads(raw) if raw else []


def add_intake_question(conn, document_id, seq, text, question_number=None,
                        grade=None, subject=None, marks=None, topic=None,
                        answer=None, extraction_confidence="ok",
                        section=None, parent_seq=None, depth=0):
    """`parent_seq` is the seq of the question this is a part of, within
    the same document - not a row id, so a whole paper can be inserted in
    one pass without reading back the ids it just wrote, and a re-split
    rebuilds the same links from the extractor's own output."""
    conn.execute(
        """INSERT INTO intake_questions
           (document_id, seq, question_number, text, grade, subject,
            marks, topic, answer, extraction_confidence, section,
            parent_seq, depth)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (document_id, seq, question_number, text, grade, subject, marks,
         topic, answer, extraction_confidence, section, parent_seq, depth),
    )


def add_extracted_questions(conn, document_id, drafts,
                            fallback_grade=None, fallback_subject=None):
    """Bulk-insert the output of intake.extract()['questions'].

    Each draft's own grade/subject wins; the paper-level values fill in
    where the extractor found nothing.
    """
    for d in drafts:
        add_intake_question(
            conn, document_id, d["seq"], d.get("text", ""),
            question_number=d.get("question_number"),
            grade=d.get("grade") or fallback_grade,
            subject=d.get("subject") or fallback_subject,
            marks=d.get("marks"),
            topic=d.get("topic"),
            answer=d.get("answer"),
            extraction_confidence=d.get("extraction_confidence", "ok"),
            section=d.get("section"),
            parent_seq=d.get("parent_seq"),
            depth=d.get("depth", 0),
        )


def intake_documents(conn):
    return conn.execute(
        "SELECT * FROM intake_documents ORDER BY id DESC"
    ).fetchall()


def get_intake_document(conn, doc_id):
    return conn.execute(
        "SELECT * FROM intake_documents WHERE id = ?", (doc_id,)
    ).fetchone()


def upsert_teacher(conn, identity, display_name=None):
    """Record that this identity has opened the teacher review page -
    called once at the top of that page, every visit. First visit
    creates the row (this is how a name reaches the assigning
    administrator's dropdown at all); every later visit only refreshes
    last_seen_at and display_name, never touching first_seen_at."""
    conn.execute(
        """INSERT INTO teachers (identity, display_name)
           VALUES (?, ?)
           ON CONFLICT(identity) DO UPDATE SET
               display_name = COALESCE(excluded.display_name, teachers.display_name),
               last_seen_at = now()""",
        (identity, display_name),
    )


def list_teachers(conn):
    return conn.execute(
        "SELECT * FROM teachers ORDER BY display_name, identity"
    ).fetchall()


def assign_document(conn, doc_id, teacher_identity):
    """Hand a checked paper to a specific teacher - the administrator's
    action. Moves it straight into 'teacher_review'; there is no
    unassigned-but-in-review state."""
    conn.execute(
        """UPDATE intake_documents SET assigned_to = ?, status = 'teacher_review'
           WHERE id = ?""",
        (teacher_identity, doc_id),
    )


def documents_assigned_to(conn, teacher_identity):
    """Every paper ever assigned to this teacher, newest first - not
    only the ones still awaiting them, so their own list also shows what
    they have already finished (see intake_documents.status)."""
    return conn.execute(
        """SELECT * FROM intake_documents WHERE assigned_to = ?
           ORDER BY id DESC""",
        (teacher_identity,),
    ).fetchall()


def intake_questions(conn, doc_id):
    return conn.execute(
        "SELECT * FROM intake_questions WHERE document_id = ? ORDER BY seq",
        (doc_id,),
    ).fetchall()


def add_question_comment(conn, question_id, author, comment):
    """A teacher's note on one question - see intake_question_comments.
    Never edited or deleted once left: a changed mind is a new comment,
    the same reasoning record_decision() already uses for verdicts."""
    conn.execute(
        """INSERT INTO intake_question_comments (question_id, author, comment)
           VALUES (?, ?, ?)""",
        (question_id, author, comment),
    )


def question_comments_for_document(conn, doc_id):
    """{question_id: [comment row, ...]}, oldest first, for every question
    in one paper - one query rather than one per question card on the
    review page (see CLAUDE.md, "One query instead of one per item")."""
    rows = conn.execute(
        """SELECT c.* FROM intake_question_comments c
           JOIN intake_questions q ON q.id = c.question_id
           WHERE q.document_id = ?
           ORDER BY c.created_at""",
        (doc_id,),
    ).fetchall()
    by_question = {}
    for r in rows:
        by_question.setdefault(r["question_id"], []).append(r)
    return by_question


def add_question_image(conn, question_id, slot, image_bytes, content_type=None):
    """A reviewer's own attached image for a question, its answer, or
    one MCQ option - see intake_question_images. Returns the new
    image's id."""
    cur = conn.execute(
        """INSERT INTO intake_question_images
           (question_id, slot, image_blob, content_type)
           VALUES (?, ?, ?, ?)
           RETURNING id""",
        (question_id, slot, psycopg2.Binary(image_bytes), content_type),
    )
    return cur.fetchone()["id"]


def question_images_for_document(conn, doc_id):
    """{question_id: [image row, ...]} for every question in one paper -
    one query rather than one per question card, same reasoning as
    question_comments_for_document(). Rows carry metadata only, not the
    image bytes themselves - see question_image_blob()."""
    rows = conn.execute(
        """SELECT i.id, i.question_id, i.slot, i.content_type, i.created_at
           FROM intake_question_images i
           JOIN intake_questions q ON q.id = i.question_id
           WHERE q.document_id = ?
           ORDER BY i.created_at""",
        (doc_id,),
    ).fetchall()
    by_question = {}
    for r in rows:
        by_question.setdefault(r["question_id"], []).append(r)
    return by_question


def question_image_blob(conn, image_id):
    """One attached image's own bytes, for display - kept separate from
    question_images_for_document()'s bulk listing so showing a paper's
    worth of image thumbnails never has to pull every image's full
    bytes into memory at once."""
    row = conn.execute(
        "SELECT image_blob FROM intake_question_images WHERE id = ?",
        (image_id,),
    ).fetchone()
    return bytes(row["image_blob"]) if row else None


def delete_question_image(conn, image_id):
    conn.execute("DELETE FROM intake_question_images WHERE id = ?", (image_id,))


_INTAKE_Q_EDITABLE = {
    "question_number", "text", "grade", "subject", "marks", "topic",
    "answer", "reviewed", "extraction_confidence", "section",
    "teacher_reviewed",
}


def update_intake_question(conn, q_id, **fields):
    """Apply a reviewer's edits. Only the columns a reviewer may change
    are accepted; anything else raises rather than being silently dropped."""
    bad = set(fields) - _INTAKE_Q_EDITABLE
    if bad:
        raise ValueError(f"not an editable intake_questions column: {sorted(bad)}")
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE intake_questions SET {cols} WHERE id = ?",
        (*fields.values(), q_id),
    )


def delete_intake_question(conn, q_id):
    """A spurious extraction - a heading read as a question, a page
    number - that a reviewer drops entirely.

    A question's parts go with it: they are parts OF it, and left behind
    they would be orphans the review page could not place under anything
    (see add_intake_question's parent_seq).
    """
    row = conn.execute(
        "SELECT document_id, seq FROM intake_questions WHERE id = ?",
        (q_id,)).fetchone()
    if row is None:
        return
    rows = conn.execute(
        "SELECT id, seq, parent_seq FROM intake_questions WHERE document_id = ?",
        (row["document_id"],)).fetchall()
    below = {}
    for r in rows:
        below.setdefault(r["parent_seq"], []).append(r)

    doomed, stack = [q_id], [row["seq"]]
    while stack:
        for child in below.get(stack.pop(), []):
            doomed.append(child["id"])
            stack.append(child["seq"])

    # Comments and images reference the question they are attached to
    # with no ON DELETE CASCADE - deleting the question first would be a
    # foreign-key violation, not a silent orphan.
    conn.execute("DELETE FROM intake_question_comments WHERE question_id = ANY(?)",
                (doomed,))
    conn.execute("DELETE FROM intake_question_images WHERE question_id = ANY(?)",
                (doomed,))
    conn.executemany("DELETE FROM intake_questions WHERE id = ?",
                     [(i,) for i in doomed])


def replace_intake_questions(conn, doc_id, drafts,
                             fallback_grade=None, fallback_subject=None):
    """Throw away a document's drafts and re-add them from a fresh
    segmentation.

    Used when a paper is re-split - a different split style, or the same
    paper run through a fixed extractor. Destructive by design: the
    previous drafts came from a segmentation the reviewer has just
    rejected, and keeping them would mean reconciling two different sets
    of boundaries over the same text. Any edits already made to those
    drafts go with them, so the UI asks first.
    """
    # Comments and images reference the question they are attached to
    # with no ON DELETE CASCADE - deleting the questions first would be
    # a foreign-key violation, not a silent orphan. A re-split's whole
    # point is that the previous boundaries are rejected, so any
    # comment or image attached to the old drafts has nothing left to
    # attach to and goes with them.
    conn.execute(
        """DELETE FROM intake_question_comments WHERE question_id IN
           (SELECT id FROM intake_questions WHERE document_id = ?)""",
        (doc_id,))
    conn.execute(
        """DELETE FROM intake_question_images WHERE question_id IN
           (SELECT id FROM intake_questions WHERE document_id = ?)""",
        (doc_id,))
    conn.execute("DELETE FROM intake_questions WHERE document_id = ?", (doc_id,))
    add_extracted_questions(conn, doc_id, drafts,
                            fallback_grade=fallback_grade,
                            fallback_subject=fallback_subject)


_INTAKE_DOC_EDITABLE = {"paper_grade", "paper_subject", "notes", "status",
                        "raw_text", "split_style", "paper_title", "sections"}


def update_intake_document(conn, doc_id, **fields):
    bad = set(fields) - _INTAKE_DOC_EDITABLE
    if bad:
        raise ValueError(f"not an editable intake_documents column: {sorted(bad)}")
    if not fields:
        return
    if fields.get("sections") is not None and not isinstance(fields["sections"], str):
        fields["sections"] = json.dumps(fields["sections"])
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE intake_documents SET {cols} WHERE id = ?",
        (*fields.values(), doc_id),
    )
