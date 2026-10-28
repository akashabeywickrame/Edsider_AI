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

Paper intake lives in a schema of its own, `intake`, and EdSider's
reference lists in `common` (see INTAKE_TEMPLATE, COMMON_LISTS); beside a
test's schema they are named after it (schema_names()).
"""

import array
import json
import mimetypes
import os
import re
import threading
import time
from pathlib import Path

import numpy as np
import psycopg2
import psycopg2.extensions
import psycopg2.extras

from . import files
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

-- Pictures of a question in the bank that came from paper intake: where
-- each is in file storage (src/files.py). A synced EdSider question keeps
-- its pictures inside its own text instead. `id` is the picture's id from
-- intake, so a link to it keeps working once the paper is archived.
CREATE TABLE IF NOT EXISTS question_files (
    id            BIGINT PRIMARY KEY,
    question_id   TEXT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    slot          TEXT NOT NULL,
    path          TEXT NOT NULL,
    content_type  TEXT,
    size          BIGINT,
    label         TEXT,
    created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_question_files_question ON question_files(question_id);
CREATE INDEX IF NOT EXISTS idx_qa_question   ON question_assessments(question_id);

-- Everyone this app knows as staff, one row per person, keyed by the
-- email Easy Auth reports at sign-in (lower-cased) - the same key
-- EdSider's own staff_user table uses. Three ways a row gets here, all
-- into this one table so every screen reads the same place:
--   * sync.py's sync_teachers() - EdSider's TEACH-role staff (in_edsider)
--   * a superadmin, on Manage users (role, added_by)
--   * opening the teacher app (first_seen_at/last_seen_at)
-- Only what THIS app needs is stored: role here, active here, subjects
-- for assigning papers. EdSider owns everything else about a person
-- (phone, address...) and is where it gets edited - a copy here would
-- only drift out of date.
--
-- Who may sign in, and as what (see staff_role_sql / lookup_staff()):
-- nobody inactive; otherwise the role a superadmin set, else 'teacher'
-- for anyone EdSider lists as a teacher, else nobody.
CREATE TABLE IF NOT EXISTS staff (
    email         TEXT PRIMARY KEY,
    name          TEXT,
    -- Set by a superadmin: 'superadmin' | 'admin' | 'teacher' |
    -- 'employee'. NULL means "whatever EdSider says" - teacher for a
    -- synced teacher, no access for anyone else.
    role          TEXT,
    in_edsider    BOOLEAN NOT NULL DEFAULT FALSE,
    -- A superadmin's switch-off: blocks sign-in whatever role or EdSider
    -- says, without losing the row's history.
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    -- JSON list of subject names this person reviews - narrows the
    -- assignment dropdown later; empty means "not stated".
    subjects      TEXT,
    added_by      TEXT,
    added_at      TIMESTAMPTZ,
    synced_at     TIMESTAMPTZ,
    first_seen_at TIMESTAMPTZ,
    last_seen_at  TIMESTAMPTZ
);

-- Every EdSider position a synced person holds (EdSider role codes - DEOP,
-- TEACH, ASSR ...), names in common.staff_role. For showing who someone
-- is; access comes from staff (see STAFF_ROLE_SQL).
CREATE TABLE IF NOT EXISTS staff_position (
    email      TEXT NOT NULL REFERENCES staff(email) ON DELETE CASCADE,
    role_code  TEXT NOT NULL,
    PRIMARY KEY (email, role_code)
);
"""


# EdSider's own reference lists - grades, subjects, mediums, terms, years,
# exam categories and the rest - one table per list in the `common` schema,
# keyed by EdSider's own id and copied by scripts/sync.py (see
# sync_edsider_lookups()). Everything else names an entry by that id, so a
# grade or subject name is stored once, here, and a paper sent to EdSider
# already carries EdSider's ids. A list's own extra columns (all EdSider ids
# or numbers) are named beside it. `active` goes false when EdSider drops an
# entry; a row is never deleted, since a paper may still name it.
COMMON_LISTS = {
    "grade": (), "subject": (), "language": (), "term": (), "year": (),
    "exam_category": (), "difficulty": (), "source": (),
    "author": ("type_id",), "part_notation": (), "part": ("notation_id",),
    "assessment_category": (), "grading_method": (), "privilege": (),
    "attempt": (), "time_unit": (), "passage_type": (), "question_type": (),
    "answer_type": (), "assessment_status": ("display_order",), "chapter": (),
}


def _common_sql():
    tables = []
    for kind, extra in COMMON_LISTS.items():
        extra_cols = "".join(f"    {c} INTEGER,\n" for c in extra)
        tables.append(
            f"CREATE TABLE IF NOT EXISTS common.{kind} (\n"
            "    id          INTEGER PRIMARY KEY,\n"
            "    name        TEXT NOT NULL,\n"
            "    short_code  TEXT,\n"
            f"{extra_cols}"
            "    active      BOOLEAN NOT NULL DEFAULT TRUE,\n"
            "    synced_at   TIMESTAMPTZ\n"
            ");\n")
    # EdSider's staff roles are keyed by a code, not a number.
    tables.append("CREATE TABLE IF NOT EXISTS common.staff_role (\n"
                  "    code       TEXT PRIMARY KEY,\n"
                  "    name       TEXT NOT NULL,\n"
                  "    synced_at  TIMESTAMPTZ\n"
                  ");\n")
    return "\n".join(tables)


# The labels a paper carries for EdSider, each an id on one of EdSider's
# lists - the same pairs as src/edsider/labels.LIST_FOR (a test keeps them
# equal), stored as columns on intake.document.
DOCUMENT_LABELS = {"medium_id": "language", "term_id": "term", "year_id": "year",
                   "exam_category_id": "exam_category", "difficulty_id": "difficulty",
                   "source_id": "source", "author_id": "author",
                   "grading_method_id": "grading_method"}

# Where an uploaded paper is in its review - one row per status, so a
# paper's status is a key into this table rather than free text.
INTAKE_STATUSES = [
    ("extracted", "Extracted"),
    ("in_review", "In review"),
    ("checked", "Duplicate check done"),
    ("data_entry_done", "Ready for a teacher"),
    ("teacher_review", "Awaiting teacher review"),
    ("refer_back", "Sent back for changes"),
    ("review_done", "Review done"),
    ("ready_to_publish", "Ready to publish"),
    ("published", "Sent to EdSider"),
    ("archived", "Archived"),
]

# Paper intake (src/intake/), all in the `intake` schema. A paper is
# uploaded by an administrator, assigned to a Data Entry Operator who
# corrects what was extracted and checks it for repeats, copied into the
# question bank when they finish (sync_paper_to_bank()), reviewed by a
# teacher (the bank copy refreshed when they are done), and archived once
# ready to publish (archive_document()).
#
# Normalised: a paper's grade, subject and EdSider labels are ids on the
# common lists, never their names; a question does not repeat its paper's
# grade or subject; a part, a group instruction and the paper's settings
# are rows of their own rather than JSON inside the paper.
INTAKE_TEMPLATE = """
CREATE TABLE IF NOT EXISTS intake.status (
    code           TEXT PRIMARY KEY,
    label          TEXT NOT NULL,
    display_order  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS intake.document (
    id                INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    filename          TEXT NOT NULL,
    file_type         TEXT,               -- 'pdf' | 'docx' | 'image' | 'text' | 'other'
    title             TEXT,
    grade_id          INTEGER REFERENCES common.grade(id),
    subject_id        INTEGER REFERENCES common.subject(id),
    medium_id         INTEGER REFERENCES common.language(id),
    term_id           INTEGER REFERENCES common.term(id),
    year_id           INTEGER REFERENCES common.year(id),
    exam_category_id  INTEGER REFERENCES common.exam_category(id),
    difficulty_id     INTEGER REFERENCES common.difficulty(id),
    source_id         INTEGER REFERENCES common.source(id),
    author_id         INTEGER REFERENCES common.author(id),
    grading_method_id INTEGER REFERENCES common.grading_method(id),
    -- The paper's own general instructions ("Answer all questions").
    notes             TEXT,
    split_style       TEXT,
    status            TEXT NOT NULL DEFAULT 'extracted'
                      REFERENCES intake.status(code),
    -- Set once the paper's questions are in the question bank: its
    -- questions are read from there from then on, and it cannot change.
    archived_at       TIMESTAMPTZ,
    uploaded_by       TEXT,
    uploaded_at       TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_intake_doc_status ON intake.document(status);

-- The uploaded file and the text read from it - the large columns, kept
-- apart so listing or opening a paper never drags them along.
CREATE TABLE IF NOT EXISTS intake.document_file (
    document_id  INTEGER PRIMARY KEY REFERENCES intake.document(id) ON DELETE CASCADE,
    -- Where the uploaded file is in file storage (src/files.py - Azure Blob
    -- Storage), its size and checksum. file_blob held the bytes themselves
    -- before that, and still does for a file not moved yet.
    file_path    TEXT,
    file_size    BIGINT,
    file_sha256  TEXT,
    file_blob    BYTEA,
    -- Verbatim, so a paper can be re-split without the file again.
    raw_text     TEXT,
    extractor    TEXT                -- which path produced the text, or why none did
);

-- The assessment screen's settings for a paper, one row per paper, and
-- the marks plan's paper-wide figures (its per-part figures are on
-- intake.part).
CREATE TABLE IF NOT EXISTS intake.document_settings (
    document_id          INTEGER PRIMARY KEY REFERENCES intake.document(id) ON DELETE CASCADE,
    schedule_at          TIMESTAMP,
    time_limit           BOOLEAN,
    duration_value       DOUBLE PRECISION,
    duration_unit        TEXT,
    access_buffer_value  DOUBLE PRECISION,
    access_buffer_unit   TEXT,
    pause_option         BOOLEAN,
    restart_option       BOOLEAN,
    part_notation        TEXT,
    multiple_papers      BOOLEAN,
    master_paper         BOOLEAN,
    parts                INTEGER,        -- how many parts the paper states
    marking_saved        BOOLEAN NOT NULL DEFAULT FALSE,
    marking_notes        TEXT,
    marking_total        DOUBLE PRECISION,
    marking_target_total DOUBLE PRECISION
);

-- A paper's parts - "Part I" multiple choice, "Part II" structured - in
-- paper order. `label` is NULL for a paper in one piece.
CREATE TABLE IF NOT EXISTS intake.part (
    id                   INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    document_id          INTEGER NOT NULL REFERENCES intake.document(id) ON DELETE CASCADE,
    seq                  INTEGER NOT NULL,
    label                TEXT,
    heading              TEXT,
    kind                 TEXT,          -- 'mcq' | 'structured' | NULL
    instructions         TEXT,
    marks_each           DOUBLE PRECISION,  -- as the paper states it
    -- The marks plan data entry saves: questions in the part, how many a
    -- student answers, and marks for each.
    plan_question_count  INTEGER,
    plan_answer_count    INTEGER,
    plan_marks_each      DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_intake_part_doc ON intake.part(document_id);

-- Instructions for a run of questions ("answer questions 20 to 25 using
-- the chart below"), shown above the first of them.
CREATE TABLE IF NOT EXISTS intake.instruction_group (
    id           INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    document_id  INTEGER NOT NULL REFERENCES intake.document(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    part_id      INTEGER REFERENCES intake.part(id),
    from_number  TEXT NOT NULL,
    to_number    TEXT NOT NULL,
    title        TEXT,
    text         TEXT NOT NULL DEFAULT ''
);

-- Extracted questions and their parts, as drafts. `parent_seq` is the seq
-- of the question this is a part of, within the same paper (how deep a
-- part sits follows from it, so it is not stored). Grade and subject are
-- the paper's.
CREATE TABLE IF NOT EXISTS intake.question (
    id                     INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    document_id            INTEGER NOT NULL REFERENCES intake.document(id) ON DELETE CASCADE,
    part_id                INTEGER REFERENCES intake.part(id),
    seq                    INTEGER NOT NULL,
    parent_seq             INTEGER,
    question_number        TEXT,      -- as printed ("31", "12(a)")
    text                   TEXT,
    marks                  INTEGER,
    topic                  TEXT,
    answer                 TEXT,
    -- A reviewer's correction of the detected type; NULL = as detected.
    question_type          TEXT,
    -- 'low' where the extractor read almost nothing.
    extraction_confidence  TEXT DEFAULT 'ok',
    reviewed               BOOLEAN NOT NULL DEFAULT FALSE,   -- data entry's tick
    teacher_reviewed       BOOLEAN NOT NULL DEFAULT FALSE,   -- the teacher's
    last_edited_by         TEXT,
    last_edited_at         TIMESTAMPTZ,
    created_at             TIMESTAMPTZ DEFAULT now(),
    -- Checked at the end of each statement, so make_room_after() can move
    -- every later question down a place in one UPDATE.
    UNIQUE (document_id, seq) DEFERRABLE
);

-- A teacher's note on one question. Never edited: a changed mind is a
-- new comment.
CREATE TABLE IF NOT EXISTS intake.question_comment (
    id            INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    question_id   INTEGER NOT NULL REFERENCES intake.question(id) ON DELETE CASCADE,
    author        TEXT,
    comment       TEXT NOT NULL,
    created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_intake_qc_question ON intake.question_comment(question_id);

-- A picture data entry attaches to a question, its answer, or one MCQ
-- option. `slot` is 'question', 'answer' or 'option:<label>'; `label` is
-- what the paper prints under it ("A", "B"...), shown with it - set only
-- where a question names its pictures that way.
CREATE TABLE IF NOT EXISTS intake.question_image (
    id            INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    question_id   INTEGER NOT NULL REFERENCES intake.question(id) ON DELETE CASCADE,
    slot          TEXT NOT NULL,
    -- In file storage like a paper's file; image_blob for one not moved yet.
    image_path    TEXT,
    image_size    BIGINT,
    image_blob    BYTEA,
    content_type  TEXT,
    label         TEXT,
    created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_intake_qi_question ON intake.question_image(question_id);

-- Each time a paper was sent to EdSider (or tried to be): where, by whom,
-- and the ids EdSider gave it - so a sent paper can be found there, and a
-- failed attempt is not lost.
CREATE TABLE IF NOT EXISTS intake.edsider_send (
    id             INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    document_id    INTEGER NOT NULL REFERENCES intake.document(id) ON DELETE CASCADE,
    sent_at        TIMESTAMPTZ DEFAULT now(),
    sent_by        TEXT,
    target         TEXT NOT NULL,
    arrival_state  TEXT,
    ok             BOOLEAN NOT NULL,
    assessment_id  INTEGER,
    question_ids   TEXT,
    error          TEXT
);

-- A question data entry's duplicate check found repeated on its own paper
-- ("repeat of" the question it copies), for the teacher to replace with a
-- new question or keep. One open flag a question; a resolved one stays, so
-- what happened is kept. Tied to the paper, not by key to the question:
-- archiving a paper removes its question rows, and the record must stay.
CREATE TABLE IF NOT EXISTS intake.question_flag (
    id               INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    document_id      INTEGER NOT NULL REFERENCES intake.document(id) ON DELETE CASCADE,
    question_id      INTEGER NOT NULL,
    repeat_of        INTEGER,
    note             TEXT,
    flagged_by       TEXT,
    flagged_at       TIMESTAMPTZ DEFAULT now(),
    -- open, replaced (a new question took its place) or kept (not a repeat)
    status           TEXT NOT NULL DEFAULT 'open',
    resolved_by      TEXT,
    resolved_at      TIMESTAMPTZ,
    resolution_note  TEXT,
    revision_id      INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_intake_flag_open
    ON intake.question_flag(question_id) WHERE status = 'open';

-- A question as it was before it was replaced - its text, answer, marks
-- and type - with who replaced it, when and why: the question's history.
-- Tied to the paper, like a flag, so archiving keeps it.
CREATE TABLE IF NOT EXISTS intake.question_revision (
    id              INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    document_id     INTEGER NOT NULL REFERENCES intake.document(id) ON DELETE CASCADE,
    question_id     INTEGER NOT NULL,
    question_number TEXT,
    text            TEXT,
    answer          TEXT,
    marks           INTEGER,
    question_type   TEXT,
    replaced_by     TEXT,
    replaced_at     TIMESTAMPTZ DEFAULT now(),
    reason          TEXT,
    flag_id         INTEGER
);
CREATE INDEX IF NOT EXISTS idx_intake_revision_question
    ON intake.question_revision(question_id);

-- A teacher's tick on a question - each of a paper's two teachers ticks
-- every question for themselves, so one's ticks never stand for the other's.
CREATE TABLE IF NOT EXISTS intake.question_check (
    question_id   INTEGER NOT NULL REFERENCES intake.question(id) ON DELETE CASCADE,
    teacher       TEXT NOT NULL,
    checked_at    TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (question_id, teacher)
);

-- A paper's tables and pictures, each a picture of itself, numbered as the
-- "[Table N]" / "[Picture N]" lines in its read text. Kept for the paper so
-- a re-split can attach them again; each question's own copy is a
-- question_image.
CREATE TABLE IF NOT EXISTS intake.document_table (
    document_id   INTEGER NOT NULL REFERENCES intake.document(id) ON DELETE CASCADE,
    n             INTEGER NOT NULL,
    page          INTEGER,
    image_path    TEXT,
    image_size    BIGINT,
    image_blob    BYTEA,
    created_at    TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (document_id, n)
);

-- Who a paper is with: a Data Entry Operator ('data_entry') or a teacher
-- ('teacher') - EdSider's own assessment_operation, for our two stages.
-- One active row per stage; reassigning closes the old row and opens a
-- new one, so who had it before is kept. `finished_at` is set when that
-- person hands the paper on, and cleared when it comes back to them.
CREATE TABLE IF NOT EXISTS intake.review (
    id           INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    document_id  INTEGER NOT NULL REFERENCES intake.document(id) ON DELETE CASCADE,
    stage        TEXT NOT NULL CHECK (stage IN ('data_entry', 'teacher')),
    assigned_to  TEXT NOT NULL,
    assigned_by  TEXT,
    assigned_at  TIMESTAMPTZ DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    active       BOOLEAN NOT NULL DEFAULT TRUE,
    -- A paper's two teachers do different jobs: 'owner' (the teacher whose
    -- paper it is - corrects it and settles the repeats data entry flagged)
    -- and 'checker' (checks it against the syllabus, comments only).
    duty         TEXT
);

-- One operator a paper, but two teachers: each teacher once.
-- (idx_intake_review_active allowed one active row a stage - one teacher.)
DROP INDEX IF EXISTS intake.idx_intake_review_active;
CREATE UNIQUE INDEX IF NOT EXISTS idx_intake_review_operator
    ON intake.review(document_id) WHERE active AND stage = 'data_entry';
CREATE UNIQUE INDEX IF NOT EXISTS idx_intake_review_teacher
    ON intake.review(document_id, assigned_to) WHERE active AND stage = 'teacher';
CREATE INDEX IF NOT EXISTS idx_intake_review_person
    ON intake.review(assigned_to, stage) WHERE active;

-- Everything that happened to a paper, in order - uploaded, assigned,
-- handed on, sent back, added to the bank - like EdSider's own
-- assessment_operation_log.
CREATE TABLE IF NOT EXISTS intake.review_log (
    id           INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    document_id  INTEGER NOT NULL REFERENCES intake.document(id) ON DELETE CASCADE,
    action       TEXT NOT NULL,
    stage        TEXT,
    actor        TEXT,
    assigned_to  TEXT,
    from_status  TEXT,
    to_status    TEXT,
    at           TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_intake_log_doc ON intake.review_log(document_id);
"""


def _intake_sql():
    statuses = ", ".join(f"('{code}', '{label}', {i})"
                         for i, (code, label) in enumerate(INTAKE_STATUSES, 1))
    return (INTAKE_TEMPLATE + "\nINSERT INTO intake.status (code, label, display_order) "
            f"VALUES {statuses}\nON CONFLICT (code) DO UPDATE SET label = excluded.label, "
            "display_order = excluded.display_order;\n")


def schema_names():
    """(intake, common) - the two schemas beside the main one. On a test's
    own schema they are named after it, so every test still gets tables
    nobody else can see."""
    if SCHEMA_NAME == "public":
        return "intake", "common"
    return f"{SCHEMA_NAME}_intake", f"{SCHEMA_NAME}_common"


# Every query names these schemas as `intake.` and `common.`; _qualify()
# swaps in a test schema's own names (schema_names()).
_SCHEMA_PREFIX = re.compile(r"(?<![\w.\"])(intake|common)\.(?=[a-z_])")


def _qualify(sql):
    if SCHEMA_NAME == "public":
        return sql
    names = dict(zip(("intake", "common"), schema_names()))
    return _SCHEMA_PREFIX.sub(lambda m: names[m.group(1)] + ".", sql)


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
    return _qualify(sql.replace("?", "%s"))


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
            cur.execute(_qualify(sql))
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
        for name in schema_names():
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{name}"')
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
    """Whether every table and every migrated column already exists, in
    the main schema and in `intake` and `common`."""
    intake, common = schema_names()
    have = {(r["table_schema"], r["table_name"], r["column_name"]) for r in conn.execute(
        """SELECT table_schema, table_name, column_name FROM information_schema.columns
            WHERE table_schema = current_schema() OR table_schema = ANY(?)""",
        ([intake, common],))}
    tables = {(s, t) for s, t, _ in have}
    wanted = {(SCHEMA_NAME, t) for t in re.findall(
        r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA_TEMPLATE)}
    wanted |= {(intake, t) for t in re.findall(
        r"CREATE TABLE IF NOT EXISTS intake\.(\w+)", INTAKE_TEMPLATE)}
    wanted |= {(common, t) for t in COMMON_LISTS} | {(common, "staff_role")}
    optional = {(r["table_schema"], r["table_name"], r["column_name"]) for r in conn.execute(
        """SELECT table_schema, table_name, column_name FROM information_schema.columns
            WHERE table_schema = ANY(?) AND is_nullable = 'YES'""", ([intake],))}
    statuses = ({r["code"] for r in conn.execute("SELECT code FROM intake.status")}
                if (intake, "status") in tables else set())
    # An index change ADD COLUMN cannot express: one teacher a paper became two.
    indexes = {r["indexname"] for r in conn.execute(
        "SELECT indexname FROM pg_indexes WHERE schemaname = ? AND indexname IN "
        "('idx_intake_review_active', 'idx_intake_review_teacher')", (intake,))}
    return wanted <= tables and all(
        _migration_key(table, column) in have for table, column, _ in MIGRATIONS) and all(
        _migration_key(table, column) in optional for table, column in MADE_OPTIONAL) and (
        {code for code, _ in INTAKE_STATUSES} <= statuses) and (
        indexes == {"idx_intake_review_teacher"})


# Columns that once had to be filled and no longer do - ALTER ... DROP NOT
# NULL, which ADD COLUMN IF NOT EXISTS cannot express.
MADE_OPTIONAL = [
    # A picture's bytes live in file storage now (src/files.py).
    ("intake.question_image", "image_blob"),
]


def _migration_key(table, column):
    """(schema, table, column) for a MIGRATIONS entry - "intake.document"
    names a table in the intake schema, a bare name one in the main one."""
    if "." in table:
        schema, name = table.split(".", 1)
        return dict(zip(("intake", "common"), schema_names()))[schema], name, column
    return SCHEMA_NAME, table, column


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
    # question.answer on EdSider's side is not always plain text: for an
    # MCQ it is a comma-separated list of per-option percentage weights
    # ("100,0,0,0"), index-aligned with the options embedded in `content`;
    # for an essay/submission question it is a free-text model answer. A
    # blob reference is the third case - is_answer_blob marks it, so a
    # reader never mistakes an opaque reference for literal answer text.
    ("questions", "is_answer_blob", "INTEGER DEFAULT 0"),
    # EdSider's Data Entry Operators sign in as data entry the way its
    # teachers sign in as teachers, and both carry their two EdSider ids
    # (staff_user.user_id, registration.teacher's) for sending papers.
    ("staff", "edsider_operator", "BOOLEAN NOT NULL DEFAULT FALSE"),
    # EdSider's Main Assessment Administrators (MASAD) are this app's admins.
    ("staff", "edsider_admin", "BOOLEAN NOT NULL DEFAULT FALSE"),
    # A paper is archived once it is ready to publish (archive_document()).
    ("intake.document", "archived_at", "TIMESTAMPTZ"),
    # A paper's pictures are kept with its tables, in their own format.
    ("intake.document_table", "content_type", "TEXT"),
    # The number as the paper prints it ("01(1)(a)") on a paper's link -
    # question_number is EdSider's whole number per section.
    ("question_assessments", "printed_number", "TEXT"),
    # Its place on the paper, in order - questions added mid-paper move it.
    ("question_assessments", "position", "INTEGER"),
    # Files live in file storage (src/files.py); the database keeps where.
    ("intake.document_file", "file_path", "TEXT"),
    ("intake.document_file", "file_size", "BIGINT"),
    ("intake.document_file", "file_sha256", "TEXT"),
    ("intake.question_image", "image_path", "TEXT"),
    ("intake.question_image", "image_size", "BIGINT"),
    # EdSider requires a grading method (Auto, Self, or both) on a paper.
    ("intake.document", "grading_method_id",
     "INTEGER REFERENCES common.grading_method(id)"),
    ("staff", "edsider_user_id", "INTEGER"),
    ("staff", "edsider_staff_id", "INTEGER"),
    # A picture's printed label ("A" under a plough), on the paper and in
    # its bank copy.
    ("intake.question_image", "label", "TEXT"),
    ("question_files", "label", "TEXT"),
    # A teacher's job on the paper - owner or syllabus checker.
    ("intake.review", "duty", "TEXT"),
]

# A paper's teachers, owner first. Papers assigned before duties were kept
# have none: the first teacher given the paper is its owner.
_TEACHER_ORDER = "CASE {a}.duty WHEN 'owner' THEN 0 WHEN 'checker' THEN 2 ELSE 1 END, {a}.id"

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
    # Give up rather than queue. An ALTER TABLE waiting for its lock makes
    # every NEW query on that table wait behind it - measured: one reader
    # stuck for 37 minutes on intake_documents left this queued, and with
    # it every page of the live app that reads papers. Failing after 10 s
    # (this transaction then rolls back whole) is the lesser harm; LOCAL
    # ends with the transaction, so normal queries are unaffected.
    conn.execute("SET LOCAL lock_timeout = '10s'")
    # connect() has already settled this, outside any transaction.
    conn.executescript(_schema_sql(VECTOR_STORAGE or "bytea") + _common_sql()
                       + _intake_sql())
    # PostgreSQL can say "add it if it is missing" itself, so this does
    # not read the table's columns first - and all of them go as one
    # script, one round trip rather than one each.
    conn.executescript("".join(
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {decl};\n"
        for table, column, decl in MIGRATIONS) + "".join(
        f"ALTER TABLE {table} ALTER COLUMN {column} DROP NOT NULL;\n"
        for table, column in MADE_OPTIONAL))
    _merge_old_people_tables(conn)


def _merge_old_people_tables(conn):
    """One-off: copy the three tables `staff` replaced into it - inside
    the schema's own transaction, and only ever when `staff` is first
    created (schema apply runs only when a table is missing). Order
    matters: EdSider's list first, then a superadmin's entries (their
    name and role win), then teacher-app visits (only their timestamps,
    and a name if none). A database that never had them skips it all.

    Copied, never dropped: the deployed app and a local run share one
    database, so dropping `teachers` the moment someone ran this version
    locally would break the still-deployed older version's teacher and
    assignment pages. Drop the three by hand once this version is the one
    deployed (see CLAUDE.md, "Staff profiles")."""
    def exists(table):
        return conn.execute("SELECT to_regclass(?) AS r", (table,)).fetchone()["r"]

    if exists("edsider_teachers"):
        conn.execute(
            """INSERT INTO staff (email, name, in_edsider, synced_at)
               SELECT email, name, TRUE, synced_at FROM edsider_teachers
               ON CONFLICT (email) DO UPDATE SET in_edsider = TRUE,
                   synced_at = excluded.synced_at,
                   name = COALESCE(staff.name, excluded.name)""")
    if exists("authorized_users"):
        conn.execute(
            """INSERT INTO staff (email, name, role, added_by, added_at)
               SELECT email, name, role, added_by, added_at FROM authorized_users
               ON CONFLICT (email) DO UPDATE SET name = excluded.name,
                   role = excluded.role, added_by = excluded.added_by,
                   added_at = excluded.added_at""")
    if exists("teachers"):
        conn.execute(
            """INSERT INTO staff (email, name, first_seen_at, last_seen_at)
               SELECT identity, display_name, first_seen_at, last_seen_at
                 FROM teachers
               ON CONFLICT (email) DO UPDATE SET
                   first_seen_at = excluded.first_seen_at,
                   last_seen_at = excluded.last_seen_at,
                   name = COALESCE(staff.name, excluded.name)""")


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


def get_decision(conn, decision_id):
    """One decision row by its own id - the API layer's way to hand
    back the row record_decision() just created without re-reading the
    whole decisions table through current_decisions()."""
    return conn.execute(
        "SELECT * FROM decisions WHERE id = ?", (decision_id,)
    ).fetchone()


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
# A paper and everything extracted from it, in the `intake` schema (see
# INTAKE_TEMPLATE). Rows come back in the shape the API and app.py have
# always read - paper_grade as EdSider's grade code, paper_subject as its
# name, sections / instruction_groups / assessment_settings as a list or a
# dict - assembled here from the normalised tables, so no caller has to
# know about the joins.

def list_id(conn, kind, value, strict=True):
    """The id of `value` on one of EdSider's lists (common.<kind>), matched
    by short code or name, any case - "G12" or "12" for a grade, a subject
    by its name. None for an empty value. A value not on the list raises
    when `strict`, else is None: what the extractor read off a paper may
    be no grade at all, while a person choosing one must choose a real one."""
    if value is None or str(value).strip() == "":
        return None
    v = str(value).strip()
    row = conn.execute(
        f"""SELECT id FROM common.{kind}
             WHERE lower(trim(coalesce(short_code, ''))) = lower(?)
                OR lower(trim(name)) = lower(?)
             ORDER BY active DESC,
                      (lower(trim(coalesce(short_code, ''))) = lower(?)) DESC, id
             LIMIT 1""", (v, v, v)).fetchone()
    if row is None and strict:
        raise ValueError(f"{v!r} is not on EdSider's {kind} list")
    return row["id"] if row else None


def _num(value):
    """A stored float as the number a person typed - 2.0 back as 2."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


_DOCUMENT_SELECT = """
SELECT d.id, d.filename, d.file_type, d.title AS paper_title,
       d.grade_id, d.subject_id, coalesce(g.short_code, g.name) AS paper_grade,
       s.name AS paper_subject, m.name AS medium_name,
       d.medium_id, d.term_id, d.year_id, d.exam_category_id, d.difficulty_id,
       d.source_id, d.author_id, d.grading_method_id,
       d.notes, d.split_style, d.status, d.uploaded_by, d.uploaded_at, d.archived_at,
       f.extractor, {raw_text} AS raw_text,
       t.assigned_to AS assigned_to, t.teachers, t.teachers_done,
       t.teachers[1] AS teacher_owner,
       o.assigned_to AS operator
  FROM intake.document d
  LEFT JOIN common.grade g    ON g.id = d.grade_id
  LEFT JOIN common.subject s  ON s.id = d.subject_id
  LEFT JOIN common.language m ON m.id = d.medium_id
  LEFT JOIN intake.document_file f ON f.document_id = d.id
  -- A paper has two teachers: both, and which have finished their review.
  LEFT JOIN LATERAL (
       SELECT string_agg(r.assigned_to, ', ' ORDER BY CASE r.duty WHEN 'owner' THEN 0 WHEN 'checker' THEN 2 ELSE 1 END, r.id) AS assigned_to,
              array_agg(r.assigned_to ORDER BY CASE r.duty WHEN 'owner' THEN 0 WHEN 'checker' THEN 2 ELSE 1 END, r.id) AS teachers,
              coalesce(array_agg(r.assigned_to ORDER BY CASE r.duty WHEN 'owner' THEN 0 WHEN 'checker' THEN 2 ELSE 1 END, r.id)
                       FILTER (WHERE r.finished_at IS NOT NULL), '{{}}') AS teachers_done
         FROM intake.review r
        WHERE r.document_id = d.id AND r.stage = 'teacher' AND r.active) t ON TRUE
  LEFT JOIN intake.review o ON o.document_id = d.id AND o.stage = 'data_entry' AND o.active
"""

_SETTINGS_COLUMNS = ("schedule_at", "time_limit", "duration_value", "duration_unit",
                     "access_buffer_value", "access_buffer_unit", "pause_option",
                     "restart_option", "part_notation", "multiple_papers",
                     "master_paper", "parts")

_PART_KEYS = {"label", "heading", "kind", "instructions", "marks_each",
              # How many questions the part holds - read back from the
              # questions themselves, so a list sent back may carry it.
              "questions"}


def _part_key(label):
    """How the marks plan names a part - MarksSection.jsx's keyOf()."""
    return "_" if label is None else label


def _settings_out(row, parts):
    """intake.document_settings and the parts' plans as the one object the
    assessment form and the marks section have always read. A column never
    set is left out, so the form falls back to what the paper states."""
    if row is None:
        return {}
    out = {}
    for c in _SETTINGS_COLUMNS:
        if row[c] is None:
            continue
        out[c] = (row[c].strftime("%Y-%m-%dT%H:%M") if c == "schedule_at"
                  else _num(row[c]))
    if row["marking_saved"]:
        total, target = _num(row["marking_total"]), _num(row["marking_target_total"])
        out["marking"] = {
            "parts": {_part_key(p["label"]): {
                "question_count": p["plan_question_count"],
                "answer_count": p["plan_answer_count"],
                "marks_each": _num(p["plan_marks_each"]),
            } for p in parts if any(p[k] is not None for k in (
                "plan_question_count", "plan_answer_count", "plan_marks_each"))},
            "notes": row["marking_notes"] or "",
            "total": total,
            "target_total": target,
            "scale": target / total if target and total else None,
        }
    return out


def _with_details(conn, rows):
    """Paper rows as dicts, each with its parts, group instructions,
    settings and EdSider labels - three queries for any number of papers,
    not three per paper."""
    docs = [dict(r) for r in rows]
    if not docs:
        return docs
    ids = [d["id"] for d in docs]
    parts, groups, settings = {}, {}, {}
    for p in conn.execute(
            """SELECT p.*, (SELECT count(*) FROM intake.question q
                             WHERE q.part_id = p.id AND q.parent_seq IS NULL) AS questions
                 FROM intake.part p WHERE p.document_id = ANY(?)
                ORDER BY p.document_id, p.seq""", (ids,)).fetchall():
        parts.setdefault(p["document_id"], []).append(p)
    for g in conn.execute(
            """SELECT g.*, p.label AS section FROM intake.instruction_group g
                 LEFT JOIN intake.part p ON p.id = g.part_id
                WHERE g.document_id = ANY(?) ORDER BY g.document_id, g.seq""",
            (ids,)).fetchall():
        groups.setdefault(g["document_id"], []).append(
            {"section": g["section"], "from": g["from_number"], "to": g["to_number"],
             "title": g["title"], "text": g["text"]})
    for s in conn.execute(
            "SELECT * FROM intake.document_settings WHERE document_id = ANY(?)",
            (ids,)).fetchall():
        settings[s["document_id"]] = s
    for d in docs:
        own = parts.get(d["id"], [])
        d["sections"] = [{"label": p["label"], "heading": p["heading"], "kind": p["kind"],
                          "instructions": p["instructions"],
                          "marks_each": _num(p["marks_each"]),
                          "questions": p["questions"]} for p in own]
        d["instruction_groups"] = groups.get(d["id"], [])
        d["assessment_settings"] = _settings_out(settings.get(d["id"]), own)
        labels = {k: d[k] for k in DOCUMENT_LABELS}
        d["edsider_labels"] = labels if any(v is not None for v in labels.values()) else {}
    return docs


def _log(conn, doc_id, action, stage=None, actor=None, assigned_to=None,
         from_status=None, to_status=None):
    conn.execute(
        """INSERT INTO intake.review_log
           (document_id, action, stage, actor, assigned_to, from_status, to_status)
           VALUES (?,?,?,?,?,?,?)""",
        (doc_id, action, stage, actor, assigned_to, from_status, to_status))


def create_intake_document(conn, filename, file_type=None, extractor=None,
                           uploaded_by=None, paper_grade=None,
                           paper_subject=None, raw_text=None,
                           split_style=None, paper_title=None,
                           sections=None, file_blob=None,
                           instruction_groups=None, notes=None):
    """Record one uploaded paper - the paper, its file and read text, its
    parts and its group instructions. Returns its new id.

    `paper_grade` / `paper_subject` are what the extractor read (a grade
    code such as "G12", a subject name); one that is not on EdSider's list
    is left unset for data entry to choose, rather than refused.

    `file_blob` is the uploaded file's own bytes, kept so a reviewer can
    check a suspect read against the original page."""
    doc_id = conn.execute(
        """INSERT INTO intake.document
           (filename, file_type, title, grade_id, subject_id, notes,
            split_style, uploaded_by)
           VALUES (?,?,?,?,?,?,?,?) RETURNING id""",
        (filename, file_type, paper_title,
         list_id(conn, "grade", paper_grade, strict=False),
         list_id(conn, "subject", paper_subject, strict=False),
         notes, split_style, uploaded_by)).fetchone()["id"]
    path = size = checksum = in_db = None
    if file_blob is not None:
        size, checksum = len(file_blob), files.checksum(file_blob)
        if files.available():
            # Stored before the row is: if storing fails, the whole upload
            # is rolled back rather than leaving a paper with no file.
            path = files.paper_name(doc_id, filename, file_blob)
            files.put(path, file_blob, mimetypes.guess_type(filename or "")[0])
        else:
            in_db = psycopg2.Binary(file_blob)
    conn.execute(
        """INSERT INTO intake.document_file
           (document_id, file_path, file_size, file_sha256, file_blob, raw_text, extractor)
           VALUES (?,?,?,?,?,?,?)""",
        (doc_id, path, size, checksum, in_db, raw_text, extractor))
    _upsert_parts(conn, doc_id, sections or [])
    if instruction_groups:
        _replace_groups(conn, doc_id, instruction_groups)
    _log(conn, doc_id, "uploaded", actor=uploaded_by, to_status="extracted")
    return doc_id


def intake_file_blob(conn, doc_id):
    """The uploaded file's own bytes, for the paper-preview pane - from file
    storage, or from the database for a file not moved there yet. None for
    pasted text, or a paper uploaded before files were kept."""
    row = conn.execute(
        "SELECT file_path, file_blob FROM intake.document_file WHERE document_id = ?",
        (doc_id,)).fetchone()
    if row is None:
        return None
    if row["file_path"]:
        return files.get(row["file_path"])
    return bytes(row["file_blob"]) if row["file_blob"] is not None else None


def _stored(doc, key, empty):
    value = doc[key] if key in doc.keys() else None
    if isinstance(value, str):
        return json.loads(value) if value else empty
    return value if value is not None else empty


def intake_sections(doc):
    """A paper's parts - [{label, heading, kind, instructions, marks_each,
    questions}] - or [] for a paper in one piece."""
    return _stored(doc, "sections", [])


def intake_instruction_groups(doc):
    """A paper's instructions for groups of questions - [{section, from,
    to, title, text}] - or []."""
    return _stored(doc, "instruction_groups", [])


def intake_edsider_labels(doc):
    """{medium_id, term_id, ...} as saved, or {} for a paper with none yet."""
    return _stored(doc, "edsider_labels", {})


def intake_assessment_settings(doc):
    """The saved assessment settings, or {} for a paper not configured yet."""
    return _stored(doc, "assessment_settings", {})


def _part_ids(conn, doc_id):
    """{label: part id} for one paper, the first part of each label."""
    out = {}
    for r in conn.execute(
            "SELECT id, label FROM intake.part WHERE document_id = ? ORDER BY seq",
            (doc_id,)).fetchall():
        out.setdefault(r["label"], r["id"])
    return out


def _part_id_for(conn, doc_id, label, known):
    """The part a question or group names by label - a part the paper did
    not list yet is added at the end rather than the label being lost."""
    label = label or None
    if label in known:
        return known[label]
    if label is None:
        return None
    part_id = conn.execute(
        """INSERT INTO intake.part (document_id, seq, label)
           VALUES (?, (SELECT coalesce(max(seq), -1) + 1 FROM intake.part
                        WHERE document_id = ?), ?) RETURNING id""",
        (doc_id, doc_id, label)).fetchone()["id"]
    known[label] = part_id
    return part_id


def _upsert_parts(conn, doc_id, sections):
    """Bring a paper's parts in line with `sections`, matched by label: a
    part keeps its row (and the questions pointing at it) when its label is
    still there. Returns the ids of the parts now listed."""
    known = _part_ids(conn, doc_id)
    keep = []
    for i, s in enumerate(sections):
        bad = set(s) - _PART_KEYS
        if bad:
            raise ValueError(f"not a part field: {sorted(bad)}")
        label = s.get("label") or None
        values = (i, s.get("heading"), s.get("kind"), s.get("instructions"),
                  s.get("marks_each"))
        if label in known and known[label] not in keep:
            conn.execute(
                """UPDATE intake.part SET seq = ?, heading = ?, kind = ?,
                          instructions = ?, marks_each = ? WHERE id = ?""",
                (*values, known[label]))
            keep.append(known[label])
        else:
            part_id = conn.execute(
                """INSERT INTO intake.part
                   (document_id, seq, heading, kind, instructions, marks_each, label)
                   VALUES (?,?,?,?,?,?,?) RETURNING id""",
                (doc_id, *values, label)).fetchone()["id"]
            known.setdefault(label, part_id)
            keep.append(part_id)
    return keep


def _drop_unlisted_parts(conn, doc_id, keep):
    """Remove a paper's parts that are no longer listed and that nothing
    points at any more."""
    conn.execute(
        """DELETE FROM intake.part p
            WHERE p.document_id = ? AND NOT (p.id = ANY(?))
              AND NOT EXISTS (SELECT 1 FROM intake.question q WHERE q.part_id = p.id)
              AND NOT EXISTS (SELECT 1 FROM intake.instruction_group g
                               WHERE g.part_id = p.id)""",
        (doc_id, keep))


def _replace_groups(conn, doc_id, groups):
    conn.execute("DELETE FROM intake.instruction_group WHERE document_id = ?", (doc_id,))
    known = _part_ids(conn, doc_id)
    for i, g in enumerate(groups or []):
        conn.execute(
            """INSERT INTO intake.instruction_group
               (document_id, seq, part_id, from_number, to_number, title, text)
               VALUES (?,?,?,?,?,?,?)""",
            (doc_id, i, _part_id_for(conn, doc_id, g.get("section"), known),
             str(g["from"]), str(g["to"]), g.get("title"), g.get("text") or ""))


def _save_settings(conn, doc_id, settings):
    """Replace a paper's assessment settings with `settings` - the whole
    object the assessment form sends, its marks plan under `marking`."""
    settings = dict(settings or {})
    marking = settings.pop("marking", None)
    bad = set(settings) - set(_SETTINGS_COLUMNS)
    if bad:
        raise ValueError(f"not an assessment setting: {sorted(bad)}")
    values = [settings.get(c) for c in _SETTINGS_COLUMNS]
    values = [None if v == "" else v for v in values]
    marking = marking or None
    columns = list(_SETTINGS_COLUMNS) + ["marking_saved", "marking_notes",
                                         "marking_total", "marking_target_total"]
    values += [marking is not None,
               (marking or {}).get("notes"),
               (marking or {}).get("total"),
               (marking or {}).get("target_total")]
    conn.execute(
        f"""INSERT INTO intake.document_settings (document_id, {', '.join(columns)})
            VALUES (?, {', '.join('?' * len(columns))})
            ON CONFLICT (document_id) DO UPDATE SET
            {', '.join(f'{c} = excluded.{c}' for c in columns)}""",
        (doc_id, *values))
    conn.execute(
        """UPDATE intake.part SET plan_question_count = NULL,
                  plan_answer_count = NULL, plan_marks_each = NULL
            WHERE document_id = ?""", (doc_id,))
    for key, plan in ((marking or {}).get("parts") or {}).items():
        conn.execute(
            """UPDATE intake.part SET plan_question_count = ?,
                      plan_answer_count = ?, plan_marks_each = ?
                WHERE document_id = ? AND label IS NOT DISTINCT FROM ?""",
            (plan.get("question_count"), plan.get("answer_count"),
             plan.get("marks_each"), doc_id, None if key == "_" else key))


def _insert_questions(conn, doc_id, drafts):
    """Every draft in one statement per few hundred - a paper's questions
    one INSERT at a time cost a round trip each on the Azure server."""
    known = _part_ids(conn, doc_id)
    rows = []
    for d in drafts:
        rows.append((doc_id, _part_id_for(conn, doc_id, d.get("section"), known),
                     d["seq"], d.get("parent_seq"), d.get("question_number"),
                     d.get("text", ""), d.get("marks"), d.get("topic"),
                     d.get("answer"), d.get("question_type"),
                     d.get("extraction_confidence") or "ok"))
    for start in range(0, len(rows), 300):
        chunk = rows[start:start + 300]
        conn.execute(
            """INSERT INTO intake.question
               (document_id, part_id, seq, parent_seq, question_number, text,
                marks, topic, answer, question_type, extraction_confidence)
               VALUES """ + ", ".join(["(?,?,?,?,?,?,?,?,?,?,?)"] * len(chunk)),
            [v for row in chunk for v in row])


def add_intake_question(conn, document_id, seq, text, question_number=None,
                        grade=None, subject=None, marks=None, topic=None,
                        answer=None, extraction_confidence="ok",
                        section=None, parent_seq=None, depth=0):
    """One question, or (with `parent_seq`, the seq of the question it is a
    part of on the same paper) one part. Returns the new row's id.

    `grade`, `subject` and `depth` are accepted for older callers and not
    stored: a question's grade and subject are its paper's, and its depth
    follows from parent_seq."""
    part_id = _part_id_for(conn, document_id, section, _part_ids(conn, document_id))
    return conn.execute(
        """INSERT INTO intake.question
           (document_id, part_id, seq, parent_seq, question_number, text,
            marks, topic, answer, extraction_confidence)
           VALUES (?,?,?,?,?,?,?,?,?,?) RETURNING id""",
        (document_id, part_id, seq, parent_seq, question_number, text, marks,
         topic, answer, extraction_confidence)).fetchone()["id"]


def make_room_after(conn, document_id, after_seq):
    """Move every question after `after_seq` down one place, so a question
    added by hand can sit where it belongs on the paper. Parts point at
    their question by seq, so those links move with it."""
    conn.execute("UPDATE intake.question SET seq = seq + 1 WHERE document_id = ? AND seq > ?",
                 (document_id, after_seq))
    conn.execute("UPDATE intake.question SET parent_seq = parent_seq + 1 "
                 "WHERE document_id = ? AND parent_seq > ?", (document_id, after_seq))


def add_extracted_questions(conn, document_id, drafts,
                            fallback_grade=None, fallback_subject=None):
    """Store the output of intake.extract()['questions'], with each table
    a draft names attached to it (save_document_tables() first). The
    fallbacks are accepted for older callers: grade and subject are the
    paper's."""
    _insert_questions(conn, document_id, drafts)
    _attach_tables(conn, document_id, drafts)


def set_read_text(conn, doc_id, raw_text, extractor):
    """A paper's file read again: the text and what read it."""
    conn.execute("UPDATE intake.document_file SET raw_text = ?, extractor = ? "
                 "WHERE document_id = ?", (raw_text, extractor, doc_id))


def save_document_tables(conn, doc_id, tables):
    """A paper's tables and pictures as extract() hands them back, {"n",
    "page", "png", "content_type"}, in file storage (or the database where
    there is none). Replaces any the paper had."""
    for row in conn.execute(
            "DELETE FROM intake.document_table WHERE document_id = ? RETURNING image_path",
            (doc_id,)).fetchall():
        if row["image_path"]:
            files.delete(row["image_path"])
    for t in tables or []:
        kind = t.get("content_type") or "image/png"
        path = files.table_name(doc_id, t["n"], kind) if files.available() else None
        if path:
            files.put(path, t["png"], kind)
        conn.execute(
            """INSERT INTO intake.document_table
               (document_id, n, page, image_path, image_size, image_blob, content_type)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (doc_id, t["n"], t.get("page"), path, len(t["png"]),
             None if path else psycopg2.Binary(t["png"]), kind))


def _attach_tables(conn, doc_id, drafts):
    """Each table a draft names ({"n", "slot"}, from extract) becomes a
    picture on that question - in its question or its answer - from the
    paper's own copy. A number the paper has no table for (pasted text
    that happens to say "[Table 2]") is skipped."""
    wanted = [(d["seq"], t) for d in drafts for t in d.get("tables") or []]
    if not wanted:
        return
    stored = {r["n"]: r for r in conn.execute(
        "SELECT n, image_path, image_blob, content_type FROM intake.document_table "
        "WHERE document_id = ?", (doc_id,)).fetchall()}
    ids = {r["seq"]: r["id"] for r in conn.execute(
        "SELECT id, seq FROM intake.question WHERE document_id = ?", (doc_id,)).fetchall()}
    for seq, t in wanted:
        row = stored.get(t["n"])
        if row is None or seq not in ids:
            continue
        data = _image_bytes(row)
        if data:
            add_question_image(conn, ids[seq], t["slot"], data,
                               row["content_type"] or "image/png", label=t.get("label"))


def search_intake_documents(conn, status=None, subject=None, grade=None, search=None,
                            limit=50, offset=0, operator=None, teacher=None):
    """(total, rows) - one page of papers matching every filter given,
    newest first. `search` matches the file name or the paper's title, any
    case; "" / None means no filter. `operator` keeps only the papers that
    Data Entry Operator has now ("none": papers nobody has), `teacher` the
    ones with that teacher. Rows carry no file and no read text."""
    where, params = [], []
    for column, value in (("d.status", status), ("s.name", subject),
                          ("coalesce(g.short_code, g.name)", grade)):
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    if operator == "none":
        where.append("o.id IS NULL")
    elif operator:
        where.append("o.assigned_to = ?")
        params.append(operator)
    if teacher:
        where.append("? = ANY(t.teachers)")
        params.append(teacher)
    if search and search.strip():
        where.append("(d.filename ILIKE ? OR coalesce(d.title, '') ILIKE ?)")
        pattern = f"%{search.strip()}%"
        params += [pattern, pattern]
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    select = _DOCUMENT_SELECT.format(raw_text="NULL")
    total = conn.execute(f"SELECT count(*) AS n FROM ({select} {clause}) x",
                         params).fetchone()["n"]
    rows = conn.execute(f"{select} {clause} ORDER BY d.id DESC LIMIT ? OFFSET ?",
                        params + [limit, offset]).fetchall()
    return total, _with_details(conn, rows)


def intake_documents(conn):
    return search_intake_documents(conn, limit=None)[1]


def intake_document_facets(conn, operator=None):
    """How many papers there are per status, subject, grade and Data Entry
    Operator ("none" for nobody yet) - the paper finder's filter menus,
    each with its count. `operator` counts only that person's papers."""
    select = _DOCUMENT_SELECT.format(raw_text="NULL")
    where, params = "", []
    if operator:
        where, params = "WHERE o.assigned_to = ?", [operator]
    out = {}
    for key, column in (("status", "x.status"), ("subject", "x.paper_subject"),
                        ("grade", "x.paper_grade"),
                        ("operator", "coalesce(x.operator, 'none')")):
        out[key] = {r["v"]: r["n"] for r in conn.execute(
            f"""SELECT {column} AS v, count(*) AS n FROM ({select} {where}) x
                 WHERE {column} IS NOT NULL AND {column} <> ''
                 GROUP BY 1 ORDER BY 1""", params).fetchall()}
    return out


def get_intake_document(conn, doc_id):
    row = conn.execute(
        _DOCUMENT_SELECT.format(raw_text="f.raw_text") + " WHERE d.id = ?", (doc_id,)
    ).fetchone()
    return _with_details(conn, [row])[0] if row else None


_QUESTION_SELECT = """
SELECT q.id, q.document_id, q.seq, q.parent_seq, q.question_number, q.text,
       q.marks, q.topic, q.answer, q.question_type, q.extraction_confidence,
       q.reviewed, q.teacher_reviewed, q.last_edited_by, q.last_edited_at,
       q.created_at, p.label AS section,
       coalesce(g.short_code, g.name) AS grade, s.name AS subject
  FROM intake.question q
  JOIN intake.document d ON d.id = q.document_id
  LEFT JOIN intake.part p ON p.id = q.part_id
  LEFT JOIN common.grade g ON g.id = d.grade_id
  LEFT JOIN common.subject s ON s.id = d.subject_id
"""


def _with_depth(rows, skeleton=None):
    """Rows as dicts with `depth` - 0 for a question, 1 for its parts, 2 for
    theirs - worked out from parent_seq over the paper's questions
    (`skeleton`: (seq, parent_seq) pairs, the rows themselves by default)."""
    parent = {s: p for s, p in (skeleton or [(r["seq"], r["parent_seq"]) for r in rows])}
    out = []
    for r in rows:
        d = dict(r)
        depth, at, seen = 0, d["parent_seq"], set()
        while at is not None and at not in seen and depth < 10:
            seen.add(at)
            depth += 1
            at = parent.get(at)
        d["depth"] = depth
        out.append(d)
    return out


def get_intake_question(conn, q_id):
    """One draft by its own row id."""
    row = conn.execute(_QUESTION_SELECT + " WHERE q.id = ?", (q_id,)).fetchone()
    if row is None:
        return None
    skeleton = [(r["seq"], r["parent_seq"]) for r in conn.execute(
        "SELECT seq, parent_seq FROM intake.question WHERE document_id = ?",
        (row["document_id"],)).fetchall()]
    return _with_depth([row], skeleton)[0]


def intake_questions(conn, doc_id):
    """A paper's questions in order, each with its depth. An archived
    paper's come from the question bank, where they now live."""
    archived = conn.execute("SELECT archived_at FROM intake.document WHERE id = ?",
                            (doc_id,)).fetchone()
    if archived and archived["archived_at"]:
        return bank_paper_questions(conn, doc_id)
    return _with_depth(conn.execute(
        _QUESTION_SELECT + " WHERE q.document_id = ? ORDER BY q.seq", (doc_id,)
    ).fetchall())


def _paper_key(doc_id):
    """An intake paper's assessment_id in question_assessments - beside
    EdSider's own numeric ids, never equal to one."""
    return f"intake-{doc_id}"


def bank_paper_questions(conn, doc_id):
    """An archived paper's questions, read back from the question bank in
    the shape intake_questions() has always returned - the same ids,
    places, numbers, parts and marks the paper had."""
    rows = conn.execute(
        """SELECT qa.link_id, qa.position, qa.printed_number, qa.question_number,
                  qa.section_id, q.id AS bank_id, q.parent_id, q.text, q.marks, q.topic,
                  q.answer, q.grade, q.subject
             FROM question_assessments qa
             JOIN questions q ON q.id = qa.question_id
            WHERE qa.assessment_id = ?""", (_paper_key(doc_id),)).fetchall()
    position = {r["bank_id"]: r["position"] for r in rows}
    out = []
    for r in rows:
        out.append({
            "id": -r["link_id"], "document_id": doc_id, "seq": r["position"],
            "parent_seq": position.get(r["parent_id"]),
            "question_number": r["printed_number"] or (
                str(r["question_number"]) if r["question_number"] is not None else None),
            "text": r["text"], "marks": r["marks"], "topic": r["topic"],
            "answer": r["answer"], "question_type": None, "extraction_confidence": "ok",
            "reviewed": True, "teacher_reviewed": True, "last_edited_by": None,
            "last_edited_at": None, "created_at": None, "section": r["section_id"],
            "grade": r["grade"], "subject": r["subject"]})
    out.sort(key=lambda q: q["seq"])
    return _with_depth(out)

def stamp_question_editor(conn, q_id, identity):
    """Who last touched this question's own fields - called alongside
    update_intake_question() from every PATCH route, never on its own,
    so a read (or a no-op PATCH with no fields set) never overwrites a
    real editor with whoever merely opened the page."""
    conn.execute(
        "UPDATE intake.question SET last_edited_by = ?, last_edited_at = now() "
        "WHERE id = ?",
        (identity, q_id),
    )


def set_document_status(conn, doc_id, status, actor=None, action="status"):
    """Move a paper to `status` (a code in intake.status) and log it."""
    row = conn.execute("SELECT status FROM intake.document WHERE id = ?",
                       (doc_id,)).fetchone()
    if row is None or row["status"] == status:
        return
    conn.execute("UPDATE intake.document SET status = ? WHERE id = ?", (status, doc_id))
    _log(conn, doc_id, action, actor=actor, from_status=row["status"], to_status=status)


def assign_review(conn, doc_id, stage, identity, assigned_by=None):
    """Hand a paper to `identity` for a stage - 'data_entry' (a Data Entry
    Operator) or 'teacher' - closing whoever had it before. `identity`
    None takes it off everyone."""
    conn.execute("UPDATE intake.review SET active = FALSE "
                 "WHERE document_id = ? AND stage = ? AND active", (doc_id, stage))
    if identity:
        conn.execute(
            "INSERT INTO intake.review (document_id, stage, assigned_to, assigned_by) "
            "VALUES (?,?,?,?)", (doc_id, stage, identity, assigned_by))
    _log(conn, doc_id, "assigned" if identity else "unassigned", stage=stage,
         actor=assigned_by, assigned_to=identity)


def finish_stage(conn, doc_id, stage, actor=None, who=None):
    """The person holding a stage has handed the paper on - with `who`,
    only that person's part (one of a paper's two teachers)."""
    if who:
        conn.execute("UPDATE intake.review SET finished_at = now() WHERE document_id = ? "
                     "AND stage = ? AND active AND assigned_to = ?", (doc_id, stage, who))
    else:
        conn.execute("UPDATE intake.review SET finished_at = now() "
                     "WHERE document_id = ? AND stage = ? AND active", (doc_id, stage))
    _log(conn, doc_id, "finished", stage=stage, actor=actor)


def reopen_stage(conn, doc_id, stage, actor=None):
    """The paper has come back to whoever held this stage - every one of
    them, for the teachers."""
    conn.execute("UPDATE intake.review SET finished_at = NULL "
                 "WHERE document_id = ? AND stage = ? AND active", (doc_id, stage))
    _log(conn, doc_id, "reopened", stage=stage, actor=actor)


def assign_document(conn, doc_id, teacher_identity, assigned_by=None):
    """One teacher - kept for older callers; see assign_teachers()."""
    assign_teachers(conn, doc_id, [teacher_identity], assigned_by)


def assign_teachers(conn, doc_id, identities, assigned_by=None):
    """Hand a paper to its teachers - two, as the administrator assigns -
    and move it straight into 'teacher_review'. The first named is the
    paper's owner, the second its syllabus checker. A teacher already on it
    keeps their review (and its ticks), in the job named now; one no longer
    named is taken off, their part kept in the history."""
    wanted = list(dict.fromkeys(i for i in identities if i))
    duty = {who: "owner" if n == 0 else "checker" for n, who in enumerate(wanted)}
    current = {r["assigned_to"] for r in conn.execute(
        "SELECT assigned_to FROM intake.review WHERE document_id = ? AND stage = 'teacher' "
        "AND active", (doc_id,)).fetchall()}
    for gone in current - set(wanted):
        conn.execute("UPDATE intake.review SET active = FALSE WHERE document_id = ? "
                     "AND stage = 'teacher' AND active AND assigned_to = ?", (doc_id, gone))
        _log(conn, doc_id, "unassigned", stage="teacher", actor=assigned_by, assigned_to=gone)
    for who in wanted:
        if who in current:
            conn.execute("UPDATE intake.review SET duty = ? WHERE document_id = ? "
                         "AND stage = 'teacher' AND active AND assigned_to = ?",
                         (duty[who], doc_id, who))
            continue
        conn.execute(
            "INSERT INTO intake.review (document_id, stage, assigned_to, assigned_by, duty) "
            "VALUES (?,?,?,?,?)", (doc_id, "teacher", who, assigned_by, duty[who]))
        _log(conn, doc_id, "assigned", stage="teacher", actor=assigned_by, assigned_to=who)
    set_document_status(conn, doc_id, "teacher_review", actor=assigned_by)


def set_teacher_check(conn, question_id, teacher, checked):
    """One teacher's tick on one question, on or off."""
    if checked:
        conn.execute("INSERT INTO intake.question_check (question_id, teacher) VALUES (?, ?) "
                     "ON CONFLICT DO NOTHING", (question_id, teacher))
    else:
        conn.execute("DELETE FROM intake.question_check WHERE question_id = ? AND teacher = ?",
                     (question_id, teacher))


def teacher_checks(conn, doc_id, teacher):
    """The ids of the paper's questions this teacher has ticked."""
    return {r["question_id"] for r in conn.execute(
        "SELECT c.question_id FROM intake.question_check c JOIN intake.question q "
        "ON q.id = c.question_id WHERE q.document_id = ? AND c.teacher = ?",
        (doc_id, teacher)).fetchall()}


def paper_teachers(conn, doc_id):
    """[{assigned_to, finished_at}] - the paper's teachers now, its owner
    first (see _TEACHER_ORDER)."""
    return conn.execute(
        "SELECT assigned_to, finished_at FROM intake.review r WHERE document_id = ? "
        f"AND stage = 'teacher' AND active ORDER BY {_TEACHER_ORDER.format(a='r')}",
        (doc_id,)).fetchall()


def documents_assigned_to(conn, identity, stage="teacher"):
    """The papers this person has now for a stage, newest first -
    including the ones they have already finished, so their own list also
    shows what they have done."""
    select = _DOCUMENT_SELECT.format(raw_text="NULL")
    holder = "? = ANY(t.teachers)" if stage == "teacher" else "o.assigned_to = ?"
    return _with_details(conn, conn.execute(
        f"{select} WHERE {holder} ORDER BY d.id DESC", (identity,)
    ).fetchall())


def paper_holders(conn, doc_id=None, q_id=None, image_id=None):
    """{document_id, status, operator, teachers, teachers_done, owner} for the paper a request
    names - directly, by one of its questions, or by one of its pictures -
    or None when there is no such paper. One query: every guarded request
    asks it."""
    if doc_id is not None:
        source, value = "SELECT ?::int AS id", doc_id
    elif q_id is not None:
        source, value = "SELECT document_id AS id FROM intake.question WHERE id = ?", q_id
    elif image_id is not None:
        source = ("SELECT q.document_id AS id FROM intake.question_image i "
                  "JOIN intake.question q ON q.id = i.question_id WHERE i.id = ? "
                  "UNION ALL SELECT split_part(qa.assessment_id, '-', 2)::int "
                  "FROM question_files f JOIN question_assessments qa "
                  "ON qa.question_id = f.question_id AND qa.assessment_id LIKE 'intake-%%' "
                  "WHERE f.id = ?")
        return _holders(conn, source, (image_id, image_id))
    else:
        return None
    return _holders(conn, source, (value,))


def _holders(conn, source, params):
    order = _TEACHER_ORDER.format(a="t")
    return conn.execute(
        f"""SELECT d.id AS document_id, d.status, d.archived_at, o.assigned_to AS operator,
                   coalesce((SELECT array_agg(t.assigned_to) FROM intake.review t
                              WHERE t.document_id = d.id AND t.stage = 'teacher' AND t.active),
                            '{{}}') AS teachers,
                   coalesce((SELECT array_agg(t.assigned_to) FROM intake.review t
                              WHERE t.document_id = d.id AND t.stage = 'teacher' AND t.active
                                AND t.finished_at IS NOT NULL), '{{}}') AS teachers_done,
                   (SELECT t.assigned_to FROM intake.review t
                     WHERE t.document_id = d.id AND t.stage = 'teacher' AND t.active
                     ORDER BY {order} LIMIT 1) AS owner
              FROM ({source}) x
              JOIN intake.document d ON d.id = x.id
              LEFT JOIN intake.review o ON o.document_id = d.id
                    AND o.stage = 'data_entry' AND o.active
             LIMIT 1""", params).fetchone()


def document_history(conn, doc_id):
    """Everything logged for a paper, oldest first."""
    return conn.execute(
        "SELECT * FROM intake.review_log WHERE document_id = ? ORDER BY at, id",
        (doc_id,)).fetchall()


def document_people(conn, doc_id):
    """{stage: {...}} - who has each stage of a paper now and who gave it
    to them, with both people's EdSider ids (None for someone EdSider does
    not list) - what EdSider's operation and review rows name."""
    rows = conn.execute(
        """SELECT r.stage, r.assigned_to, r.assigned_by, r.finished_at,
                  a.edsider_user_id AS to_user_id, a.edsider_staff_id AS to_staff_id,
                  b.edsider_user_id AS by_user_id, b.edsider_staff_id AS by_staff_id
             FROM intake.review r
             LEFT JOIN staff a ON a.email = r.assigned_to
             LEFT JOIN staff b ON b.email = r.assigned_by
            WHERE r.document_id = ? AND r.active ORDER BY r.id""", (doc_id,)).fetchall()
    people = {}
    for r in rows:
        people.setdefault(r["stage"], dict(r))
    # A paper has two teachers - "teacher" is the first, "teachers" both.
    people["teachers"] = [dict(r) for r in rows if r["stage"] == "teacher"]
    return people


def add_question_comment(conn, question_id, author, comment):
    """A teacher's note on one question. Never edited or deleted once
    left: a changed mind is a new comment. Returns the new comment's id."""
    return conn.execute(
        """INSERT INTO intake.question_comment (question_id, author, comment)
           VALUES (?, ?, ?) RETURNING id""",
        (question_id, author, comment),
    ).fetchone()["id"]


def get_question_comment(conn, comment_id):
    return conn.execute(
        "SELECT * FROM intake.question_comment WHERE id = ?", (comment_id,)
    ).fetchone()


def question_comments_for_document(conn, doc_id):
    """{question_id: [comment row, ...]}, oldest first, for every question
    in one paper - one query rather than one per question card."""
    rows = conn.execute(
        """SELECT c.* FROM intake.question_comment c
           JOIN intake.question q ON q.id = c.question_id
           WHERE q.document_id = ?
           ORDER BY c.created_at""",
        (doc_id,),
    ).fetchall()
    by_question = {}
    for r in rows:
        by_question.setdefault(r["question_id"], []).append(r)
    return by_question


def add_question_image(conn, question_id, slot, image_bytes, content_type=None, label=None):
    """A picture data entry attaches to a question, its answer, or one MCQ
    option, kept in file storage. Returns the new image's id."""
    in_db = None if files.available() else psycopg2.Binary(image_bytes)
    image_id = conn.execute(
        """INSERT INTO intake.question_image
           (question_id, slot, image_size, image_blob, content_type, label)
           VALUES (?, ?, ?, ?, ?, ?) RETURNING id""",
        (question_id, slot, len(image_bytes), in_db, content_type, _label(label)),
    ).fetchone()["id"]
    if in_db is not None:
        return image_id
    path = files.image_name(question_id, image_id, content_type)
    files.put(path, image_bytes, content_type)
    conn.execute("UPDATE intake.question_image SET image_path = ? WHERE id = ?",
                 (path, image_id))
    return image_id


def _label(label):
    """A picture's label as stored: trimmed, at most 20 characters, None
    for nothing."""
    label = " ".join((label or "").split())[:20]
    return label or None


# Where a picture can be on its question - an earlier version's
# ("earlier:...") is history, never moved.
IMAGE_SLOT = re.compile(r"^(question|answer|option:[^\s:]{1,10})$")


def update_question_image(conn, image_id, fields):
    """Change one picture on a paper: `label` (shown under it; empty clears
    it) and/or `slot` - moved to the question, the answer or one option,
    where the extractor put it somewhere else. Returns {id, slot, label};
    KeyError for no such picture, ValueError for a slot that is not one."""
    sets, params = [], []
    if "label" in fields:
        sets.append("label = ?")
        params.append(_label(fields["label"]))
    if "slot" in fields:
        if not IMAGE_SLOT.match(fields["slot"] or ""):
            raise ValueError(f"{fields['slot']!r} is not a place for a picture")
        sets.append("slot = ?")
        params.append(fields["slot"])
    row = conn.execute(
        f"UPDATE intake.question_image SET {', '.join(sets) or 'slot = slot'} "
        "WHERE id = ? AND slot NOT LIKE 'earlier:%%' RETURNING id, slot, label",
        (*params, image_id)).fetchone()
    if row is None:
        raise KeyError(image_id)
    return dict(row)


def _image_bytes(row):
    if row["image_path"]:
        return files.get(row["image_path"])
    return bytes(row["image_blob"]) if row["image_blob"] is not None else None


def question_images_for_document(conn, doc_id):
    """{question_id: [image row, ...]} for every question in one paper -
    metadata only, never the bytes (see question_image_blob())."""
    rows = conn.execute(
        """SELECT i.id, i.question_id, i.slot, i.content_type, i.created_at, i.label
           FROM intake.question_image i
           JOIN intake.question q ON q.id = i.question_id
           WHERE q.document_id = ?
           UNION ALL
           -- An archived paper's pictures, now with its questions in the bank.
           -- Only once archived: the bank holds a copy from the moment data
           -- entry finishes, and read alongside the paper's own it showed the
           -- teacher every picture twice.
           SELECT f.id, -qa.link_id, f.slot, f.content_type, f.created_at, f.label
             FROM question_files f
             JOIN question_assessments qa ON qa.question_id = f.question_id
            WHERE qa.assessment_id = ?
              AND EXISTS (SELECT 1 FROM intake.document d
                           WHERE d.id = ? AND d.archived_at IS NOT NULL)
           ORDER BY 5, 1""",
        (doc_id, _paper_key(doc_id), doc_id),
    ).fetchall()
    by_question = {}
    for r in rows:
        by_question.setdefault(r["question_id"], []).append(r)
    return by_question


def question_image_blob(conn, image_id):
    """One attached image's own bytes, for display."""
    row = conn.execute(
        "SELECT image_path, image_blob FROM intake.question_image WHERE id = ?", (image_id,),
    ).fetchone()
    return _image_bytes(row) if row else None


def get_question_image(conn, image_id):
    """Bytes and content type together, for an HTTP response."""
    row = conn.execute(
        "SELECT image_path, image_blob, content_type, slot FROM intake.question_image "
        "WHERE id = ?", (image_id,),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT path AS image_path, NULL::bytea AS image_blob, content_type, slot "
            "FROM question_files WHERE id = ?", (image_id,)).fetchone()
    if row is None:
        return None
    return {"image_blob": _image_bytes(row),
            "content_type": row["content_type"], "slot": row["slot"]}


def _forget_images(conn, where, params):
    """Delete picture rows matching `where`, and their stored files."""
    paths = [r["image_path"] for r in conn.execute(
        f"DELETE FROM intake.question_image WHERE {where} RETURNING image_path",
        params).fetchall() if r["image_path"]]
    for path in paths:
        files.delete(path)


def delete_question_image(conn, image_id):
    _forget_images(conn, "id = ?", (image_id,))


_INTAKE_Q_EDITABLE = {
    "question_number", "text", "marks", "topic", "answer", "reviewed",
    "extraction_confidence", "section", "teacher_reviewed", "question_type",
}


def intake_question_has_children(conn, q_id):
    """Whether any other question on this paper names this one as its
    parent - the test for a stem, which is a sub-question if it has parts."""
    row = conn.execute(
        """SELECT 1 FROM intake.question c
           JOIN intake.question p ON p.document_id = c.document_id
                                 AND c.parent_seq = p.seq
           WHERE p.id = ? LIMIT 1""",
        (q_id,)).fetchone()
    return row is not None


def update_intake_question(conn, q_id, **fields):
    """Apply a reviewer's edits. Only the columns a reviewer may change
    are accepted; anything else raises rather than being silently dropped.
    Grade and subject are the paper's (update_intake_document)."""
    bad = set(fields) - _INTAKE_Q_EDITABLE
    if bad:
        raise ValueError(f"not an editable intake question field: {sorted(bad)}")
    if not fields:
        return
    for key in ("reviewed", "teacher_reviewed"):
        if key in fields:
            fields[key] = bool(fields[key])
    if "section" in fields:
        doc_id = conn.execute("SELECT document_id FROM intake.question WHERE id = ?",
                              (q_id,)).fetchone()["document_id"]
        fields["part_id"] = _part_id_for(conn, doc_id, fields.pop("section"),
                                         _part_ids(conn, doc_id))
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE intake.question SET {cols} WHERE id = ?",
                 (*fields.values(), q_id))


def delete_intake_document(conn, doc_id):
    """An upload made by mistake, removed whole - only while it is still
    'extracted': once data entry has checked it, a copy is in the question
    bank, its teachers or EdSider may have it, and its history matters.
    Every row of it goes (each table names the paper ON DELETE CASCADE).
    Returns the stored files it held, for delete_stored_files() once this
    has committed - files first and a failed commit would leave rows
    naming files that are gone."""
    doc = conn.execute("SELECT status, archived_at FROM intake.document WHERE id = ?",
                       (doc_id,)).fetchone()
    if doc is None:
        raise KeyError(doc_id)
    if doc["status"] != "extracted" or doc["archived_at"] or last_send(conn, doc_id):
        raise ValueError("Only a paper still at Extracted can be removed - this one has "
                         "moved on.")
    paths = [r["p"] for r in conn.execute(
        """SELECT file_path AS p FROM intake.document_file WHERE document_id = ?
           UNION ALL SELECT i.image_path FROM intake.question_image i
                       JOIN intake.question q ON q.id = i.question_id WHERE q.document_id = ?
           UNION ALL SELECT image_path FROM intake.document_table WHERE document_id = ?""",
        (doc_id, doc_id, doc_id)).fetchall() if r["p"]]
    conn.execute("DELETE FROM intake.document WHERE id = ?", (doc_id,))
    return paths


def delete_stored_files(paths):
    """Files no row names any more. One that will not go is left behind
    rather than failing a delete that has already happened."""
    for path in paths:
        try:
            files.delete(path)
        except Exception:
            pass


def delete_intake_question(conn, q_id):
    """A spurious extraction - a heading read as a question, a page
    number - that a reviewer drops entirely, with its parts (they are
    parts OF it, and left behind they could not be placed under anything).
    Comments and pictures go with them (ON DELETE CASCADE)."""
    row = conn.execute(
        "SELECT document_id, seq FROM intake.question WHERE id = ?", (q_id,)).fetchone()
    if row is None:
        return
    rows = conn.execute(
        "SELECT id, seq, parent_seq FROM intake.question WHERE document_id = ?",
        (row["document_id"],)).fetchall()
    below = {}
    for r in rows:
        below.setdefault(r["parent_seq"], []).append(r)
    doomed, stack = [q_id], [row["seq"]]
    while stack:
        for child in below.get(stack.pop(), []):
            doomed.append(child["id"])
            stack.append(child["seq"])
    _forget_images(conn, "question_id = ANY(?)", (doomed,))
    conn.execute("DELETE FROM intake.question_flag WHERE status = 'open' "
                 "AND (question_id = ANY(?) OR repeat_of = ANY(?))", (doomed, doomed))
    conn.execute("DELETE FROM intake.question WHERE id = ANY(?)", (doomed,))


def replace_intake_questions(conn, doc_id, drafts, fallback_grade=None,
                             fallback_subject=None, sections=None,
                             instruction_groups=None):
    """Throw away a paper's questions and store a fresh split of it - a
    different split style, or a fixed extractor. Destructive by design:
    the previous boundaries are the ones being rejected, so their edits,
    comments and pictures go with them, and the page asks first.

    With `sections`, the paper's parts (and its group instructions, which
    hang off them) are replaced too, so no part of the old split is left."""
    _forget_images(conn, "question_id IN (SELECT id FROM intake.question "
                         "WHERE document_id = ?)", (doc_id,))
    conn.execute("DELETE FROM intake.question_flag WHERE document_id = ? AND status = 'open'",
                 (doc_id,))
    conn.execute("DELETE FROM intake.question WHERE document_id = ?", (doc_id,))
    if sections is not None:
        conn.execute("DELETE FROM intake.instruction_group WHERE document_id = ?", (doc_id,))
        conn.execute("DELETE FROM intake.part WHERE document_id = ?", (doc_id,))
        _upsert_parts(conn, doc_id, sections)
        _replace_groups(conn, doc_id, instruction_groups or [])
    _insert_questions(conn, doc_id, drafts)
    _attach_tables(conn, doc_id, drafts)


_INTAKE_DOC_EDITABLE = {"paper_grade", "paper_subject", "grade_id", "subject_id",
                        "notes", "status", "raw_text", "split_style", "paper_title",
                        "sections", "assessment_settings", "instruction_groups",
                        "edsider_labels"}


def update_intake_document(conn, doc_id, actor=None, **fields):
    """Change a paper's details. A grade or subject may be given as its
    EdSider id (grade_id, subject_id) or as a code or name (paper_grade,
    paper_subject); either must be on EdSider's list. `sections` and
    `instruction_groups` are each sent whole; `edsider_labels` sets the
    labels it names. `actor` is who did it, for the paper's log."""
    bad = set(fields) - _INTAKE_DOC_EDITABLE
    if bad:
        raise ValueError(f"not an editable intake document field: {sorted(bad)}")
    columns = {}
    if "paper_grade" in fields:
        columns["grade_id"] = list_id(conn, "grade", fields.pop("paper_grade"))
    if "paper_subject" in fields:
        columns["subject_id"] = list_id(conn, "subject", fields.pop("paper_subject"))
    for key in ("grade_id", "subject_id", "notes", "split_style"):
        if key in fields:
            columns[key] = fields.pop(key)
    if "paper_title" in fields:
        columns["title"] = fields.pop("paper_title")
    labels = fields.pop("edsider_labels", None)
    if labels:
        unknown = set(labels) - set(DOCUMENT_LABELS)
        if unknown:
            raise ValueError(f"not an EdSider label: {sorted(unknown)}")
        columns.update(labels)
    if columns:
        conn.execute(
            f"UPDATE intake.document SET {', '.join(f'{k} = ?' for k in columns)} "
            "WHERE id = ?", (*columns.values(), doc_id))
    if "raw_text" in fields:
        conn.execute(
            """INSERT INTO intake.document_file (document_id, raw_text) VALUES (?, ?)
               ON CONFLICT (document_id) DO UPDATE SET raw_text = excluded.raw_text""",
            (doc_id, fields.pop("raw_text")))
    keep = None
    if fields.get("sections") is not None:
        keep = _upsert_parts(conn, doc_id, fields.pop("sections"))
    if fields.get("instruction_groups") is not None:
        _replace_groups(conn, doc_id, fields.pop("instruction_groups"))
    if keep is not None:
        _drop_unlisted_parts(conn, doc_id, keep)
    if "assessment_settings" in fields:
        _save_settings(conn, doc_id, fields.pop("assessment_settings"))
    if fields.get("status"):
        set_document_status(conn, doc_id, fields.pop("status"), actor=actor)


def _bank_id(doc_id, question_id):
    """A paper's question in the bank: "intake-<paper>-<question id>" -
    beside EdSider's numeric ids, never equal to one, and the same however
    often the copy is refreshed, wherever the question moves on the paper."""
    return f"intake-{doc_id}-{question_id}"


def sync_paper_to_bank(conn, doc_id, actor=None):
    """Copy a paper's questions into the question bank (`questions`), or
    refresh a copy made before - when data entry finishes, and again when
    the teacher's review is done, so the bank holds the corrected paper.
    The paper itself stays in intake, where it can still be edited.

    Each question and part is a bank row, parts linked to their question as
    EdSider keeps them; its place on the paper - EdSider's whole number per
    section, the printed number ("01(1)(a)"), its position, its part and its
    marks (0 on a question with parts, as in EdSider) - is a row in
    `question_assessments` under the paper's own assessment id "intake-<id>";
    its pictures in file storage are recorded against the bank row
    (`question_files`, keeping the picture's id). A question removed from
    the paper since the last copy leaves the bank too. A genuinely empty
    question is skipped, as COMPARABLE skips text-free rows. Returns how
    many questions the bank holds for the paper."""
    doc = get_intake_document(conn, doc_id)
    if doc is None:
        raise ValueError(f"no such intake document: {doc_id}")
    if doc.get("archived_at"):
        raise ValueError("this paper is archived; its questions are already in the bank")

    drafts = intake_questions(conn, doc_id)
    ids = {d["seq"]: _bank_id(doc_id, d["id"]) for d in drafts}
    children_of = {}
    for d in drafts:
        if d["parent_seq"] is not None:
            children_of.setdefault(d["parent_seq"], []).append(d["seq"])

    key = _paper_key(doc_id)
    kept = {}
    for d in drafts:
        text = (d["text"] or "").strip()
        kids = children_of.get(d["seq"], [])
        if not text and not kids:
            continue
        shape = ("parent" if kids
                 else "sub-question" if d["parent_seq"] is not None
                 else "standalone")
        row = {
            "id": ids[d["seq"]],
            "text": text,
            "grade": doc["paper_grade"],
            "subject": doc["paper_subject"],
            "topic": d["topic"],
            "marks": d["marks"],
            "answer": d["answer"],
            "is_answer_blob": False,
            "contributor": doc["uploaded_by"] or "paper intake",
            "source_file": doc["filename"],
            "content_hash": content_hash(text),
            "shape": shape,
            "children_raw": ",".join(ids[k] for k in kids) if kids else None,
        }
        if doc["medium_name"]:
            row["language"] = doc["medium_name"]
        save(conn, row)
        number = re.search(r"\d+", d["question_number"] or "")
        save_assessment_link(
            conn, -d["id"], key, ids[d["seq"]],
            question_number=int(number.group()) if number else None,
            mark_allocation=0 if kids else d["marks"],
            section_id=d["section"], added_by=actor)
        conn.execute("UPDATE question_assessments SET printed_number = ?, position = ? "
                     "WHERE link_id = ?", (d["question_number"], d["seq"], -d["id"]))
        kept[d["id"]] = ids[d["seq"]]

    # Whatever left the paper since the last copy leaves the bank too
    # (its pictures' rows go with it - ON DELETE CASCADE).
    gone = [r["question_id"] for r in conn.execute(
        "DELETE FROM question_assessments WHERE assessment_id = ? AND NOT (link_id = ANY(?)) "
        "RETURNING question_id", (key, [-i for i in kept])).fetchall()]
    if gone:
        conn.execute("DELETE FROM questions WHERE id = ANY(?)", (gone,))

    pictures = []
    for image in conn.execute(
            """SELECT i.id, i.question_id, i.slot, i.content_type, i.image_path,
                      i.image_size, i.label, i.image_blob IS NOT NULL AS in_db
                 FROM intake.question_image i JOIN intake.question q ON q.id = i.question_id
                WHERE q.document_id = ?""", (doc_id,)).fetchall():
        if image["question_id"] not in kept:
            continue
        path = image["image_path"]
        if path is None and image["in_db"] and files.available():
            move_file_to_storage(conn, "image", image["id"])
            path = conn.execute("SELECT image_path FROM intake.question_image WHERE id = ?",
                                (image["id"],)).fetchone()["image_path"]
        if path is None:
            continue           # stays with the paper in intake until it is in storage
        conn.execute(
            """INSERT INTO question_files (id, question_id, slot, path, content_type, size, label)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT (id) DO UPDATE SET question_id = excluded.question_id,
                   slot = excluded.slot, path = excluded.path,
                   content_type = excluded.content_type, size = excluded.size,
                   label = excluded.label""",
            (image["id"], kept[image["question_id"]], image["slot"], path,
             image["content_type"], image["image_size"], image["label"]))
        pictures.append(image["id"])
    conn.execute("DELETE FROM question_files WHERE question_id = ANY(?) AND NOT (id = ANY(?))",
                 (list(kept.values()), pictures))

    if kept:
        link_children_to_parents(conn)
    _log(conn, doc_id, "copied to the question bank", actor=actor)
    return len(kept)


def unfinished_pictures(conn, doc_id):
    """Pictures on a paper still held in the database rather than in file
    storage - they cannot go with an archived paper."""
    return conn.execute(
        """SELECT count(*) AS n FROM intake.question_image i
             JOIN intake.question q ON q.id = i.question_id
            WHERE q.document_id = ? AND i.image_path IS NULL""", (doc_id,)).fetchone()["n"]


# ------------------------------------------------ repeated questions
# Data entry's duplicate check finds a question repeated on its own paper;
# data entry flags it; the teacher replaces it with a new question (the
# old one kept in its history) or keeps it, saying why it is not a repeat.

def flag_repeat(conn, question_id, repeat_of, flagged_by=None, note=None):
    """Flag a question as a repeat of another on the same paper, for the
    teacher. Flagging it again changes which question it repeats."""
    rows = {r["id"]: r for r in conn.execute(
        "SELECT id, document_id, question_number FROM intake.question WHERE id = ANY(?)",
        ([question_id, repeat_of],)).fetchall()}
    if question_id == repeat_of or len(rows) != 2:
        raise ValueError("a question is flagged as a repeat of another question")
    if rows[question_id]["document_id"] != rows[repeat_of]["document_id"]:
        raise ValueError("both questions must be on the same paper")
    doc_id = rows[question_id]["document_id"]
    conn.execute("DELETE FROM intake.question_flag WHERE question_id = ? AND status = 'open'",
                 (question_id,))
    flag_id = conn.execute(
        """INSERT INTO intake.question_flag (document_id, question_id, repeat_of, note, flagged_by)
           VALUES (?, ?, ?, ?, ?) RETURNING id""",
        (doc_id, question_id, repeat_of, note, flagged_by)).fetchone()["id"]
    _log(conn, doc_id, f"Q{rows[question_id]['question_number']} flagged as a repeat of "
                       f"Q{rows[repeat_of]['question_number']}", actor=flagged_by)
    return flag_id


def unflag_repeat(conn, question_id, actor=None):
    """Data entry takes back a flag the teacher has not acted on."""
    row = conn.execute(
        "DELETE FROM intake.question_flag WHERE question_id = ? AND status = 'open' "
        "RETURNING document_id", (question_id,)).fetchone()
    if row:
        _log(conn, row["document_id"], "repeat flag taken back", actor=actor)


def question_flags(conn, doc_id):
    """{question_id: its latest flag, with the repeated question's number}."""
    rows = conn.execute(
        """SELECT f.*, r.question_number AS repeat_of_number
             FROM intake.question_flag f
             LEFT JOIN intake.question r ON r.id = f.repeat_of
            WHERE f.document_id = ? ORDER BY f.id""", (doc_id,)).fetchall()
    return {r["question_id"]: dict(r) for r in rows}


def open_flags(conn, doc_id):
    return conn.execute(
        "SELECT count(*) AS n FROM intake.question_flag WHERE document_id = ? AND status = 'open'",
        (doc_id,)).fetchone()["n"]


def revision_counts(conn, doc_id):
    """{question_id: how many earlier versions it has}."""
    return {r["question_id"]: r["n"] for r in conn.execute(
        "SELECT question_id, count(*) AS n FROM intake.question_revision WHERE document_id = ? "
        "GROUP BY question_id", (doc_id,)).fetchall()}


def replace_question(conn, question_id, text, answer=None, marks=None, replaced_by=None,
                     reason=None, keep_pictures=True):
    """Put a new question in this one's place on the paper. What it was -
    text, answer, marks, type - goes into its history first
    (intake.question_revision), and an open repeat flag on it is resolved
    as replaced. Its pictures stay unless `keep_pictures` is False: then
    they belong to the earlier version, kept with it (slot
    "earlier:<revision>:<slot>"), not deleted. Returns the revision id."""
    q = conn.execute("SELECT * FROM intake.question WHERE id = ?", (question_id,)).fetchone()
    if q is None:
        raise ValueError(f"no such question: {question_id}")
    flag = conn.execute("SELECT id FROM intake.question_flag WHERE question_id = ? "
                        "AND status = 'open'", (question_id,)).fetchone()
    revision = conn.execute(
        """INSERT INTO intake.question_revision
           (document_id, question_id, question_number, text, answer, marks, question_type,
            replaced_by, reason, flag_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
        (q["document_id"], question_id, q["question_number"], q["text"], q["answer"], q["marks"],
         q["question_type"], replaced_by, reason, flag["id"] if flag else None)).fetchone()["id"]
    conn.execute(
        """UPDATE intake.question SET text = ?, answer = ?, marks = ?, question_type = NULL
            WHERE id = ?""",
        (text, answer, q["marks"] if marks is None else marks, question_id))
    stamp_question_editor(conn, question_id, replaced_by)
    if not keep_pictures:
        conn.execute("UPDATE intake.question_image SET slot = 'earlier:' || ? || ':' || slot "
                     "WHERE question_id = ? AND slot NOT LIKE 'earlier:%%'",
                     (str(revision), question_id))
    if flag:
        conn.execute(
            """UPDATE intake.question_flag SET status = 'replaced', resolved_by = ?,
                      resolved_at = now(), revision_id = ?, resolution_note = ?
                WHERE id = ?""", (replaced_by, revision, reason, flag["id"]))
    _log(conn, q["document_id"], f"Q{q['question_number']} replaced with a new question",
         actor=replaced_by)
    return revision


def keep_question(conn, question_id, kept_by=None, note=None):
    """The teacher keeps a flagged question - it is not a repeat - saying why."""
    row = conn.execute(
        """UPDATE intake.question_flag SET status = 'kept', resolved_by = ?, resolved_at = now(),
                  resolution_note = ?
            WHERE question_id = ? AND status = 'open' RETURNING document_id""",
        (kept_by, note, question_id)).fetchone()
    if row is None:
        raise ValueError("this question is not flagged as a repeat")
    _log(conn, row["document_id"], "repeat kept", actor=kept_by)


def question_history(conn, question_id):
    """Every earlier version of a question, oldest first, each with the
    pictures that went with it."""
    revisions = [dict(r) for r in conn.execute(
        "SELECT * FROM intake.question_revision WHERE question_id = ? ORDER BY id",
        (question_id,)).fetchall()]
    if not revisions:
        return []
    # On the paper, or - once it is archived - in its bank copy.
    pictures = conn.execute(
        "SELECT id, slot, content_type, label FROM intake.question_image WHERE question_id = ? "
        "AND slot LIKE 'earlier:%%' UNION ALL "
        "SELECT id, slot, content_type, label FROM question_files WHERE question_id = ? "
        "AND slot LIKE 'earlier:%%' AND id NOT IN (SELECT id FROM intake.question_image "
        "WHERE question_id = ?)",
        (question_id, _bank_id(revisions[0]["document_id"], question_id), question_id)).fetchall()
    for rev in revisions:
        mark = f"earlier:{rev['id']}:"
        rev["pictures"] = [dict(p, slot=p["slot"][len(mark):]) for p in pictures
                           if p["slot"].startswith(mark)]
    return revisions


def record_send(conn, doc_id, sent_by, target, arrival_state, ok,
                assessment_id=None, question_ids=None, error=None):
    """One attempt to send a paper to EdSider - see intake.edsider_send."""
    conn.execute(
        """INSERT INTO intake.edsider_send
           (document_id, sent_by, target, arrival_state, ok, assessment_id, question_ids, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (doc_id, sent_by, target, arrival_state, ok, assessment_id,
         json.dumps(question_ids) if question_ids else None, error))
    _log(conn, doc_id, "sent to EdSider" if ok else "sending to EdSider failed", actor=sent_by)


def last_send(conn, doc_id):
    """The paper's latest successful send to EdSider, or None."""
    return conn.execute(
        "SELECT * FROM intake.edsider_send WHERE document_id = ? AND ok "
        "ORDER BY id DESC LIMIT 1", (doc_id,)).fetchone()


def archive_document(conn, doc_id, actor=None, status="archived"):
    """The last step, once the teacher's review is done ('ready_to_publish'):
    refresh the paper's bank copy one final time, then remove its questions
    from intake - from here on they are read from the bank
    (bank_paper_questions()) - and archive the paper. Its record, file,
    parts, settings and history stay; it can no longer change. Sending the
    paper to EdSider belongs to this step once EdSider's route exists."""
    doc = get_intake_document(conn, doc_id)
    if doc is None:
        raise ValueError(f"no such intake document: {doc_id}")
    if doc.get("archived_at"):
        raise ValueError("this paper is already archived")
    if doc["status"] != "ready_to_publish":
        raise ValueError(f"a paper is archived once it is ready to publish "
                         f"(status is {doc['status']!r})")
    sync_paper_to_bank(conn, doc_id, actor=actor)
    if unfinished_pictures(conn, doc_id):
        raise ValueError("some pictures on this paper are still in the database and no file "
                         "storage is set here, so they cannot go with it")
    # The files stay - the bank points at them; the rows here go, comments too.
    conn.execute("DELETE FROM intake.question WHERE document_id = ?", (doc_id,))
    conn.execute("UPDATE intake.document SET archived_at = now() WHERE id = ?", (doc_id,))
    # 'published' once it has been sent to EdSider; 'archived' without.
    set_document_status(conn, doc_id, status, actor=actor, action="archived")


# What a row may sign in as - one rule, used by every reader below so
# sign-in, the Manage users list and the dropdown can never disagree.
STAFF_ROLE_SQL = """CASE WHEN NOT active THEN NULL
                         WHEN role IS NOT NULL THEN role
                         WHEN edsider_admin THEN 'admin'
                         WHEN in_edsider THEN 'teacher'
                         WHEN edsider_operator THEN 'employee' END"""

# A person's EdSider positions ("Data Entry Operator, Assessor"), read off
# staff_position and EdSider's own role names - for showing, never for
# deciding access (that is STAFF_ROLE_SQL).
_POSITIONS_SQL = """(SELECT string_agg(coalesce(r.name, p.role_code), ', '
                                       ORDER BY coalesce(r.name, p.role_code))
                       FROM staff_position p
                       LEFT JOIN common.staff_role r ON r.code = p.role_code
                      WHERE p.email = staff.email)"""

_STAFF_COLUMNS = (f"*, {STAFF_ROLE_SQL} AS effective_role, "
                  f"{_POSITIONS_SQL} AS positions")


def lookup_staff(conn, email):
    """The person's row with their `effective_role` - None there (or no
    row at all) means they may not sign in; current_identity() turns that
    into a 403, EdSider's own "not a user" rejection."""
    return conn.execute(
        f"SELECT {_STAFF_COLUMNS} FROM staff WHERE email = ?", (email,)
    ).fetchone()


def list_staff(conn):
    """Everyone - for Manage users. People who can sign in first, by
    name; then the rest (inactive, or only ever seen in the teacher app)."""
    return conn.execute(
        f"""SELECT {_STAFF_COLUMNS} FROM staff
            ORDER BY ({STAFF_ROLE_SQL}) IS NULL, lower(coalesce(name, email))"""
    ).fetchall()


def set_staff(conn, email, name, role, added_by=None, subjects=None):
    """A superadmin adding someone, or changing their name/role/subjects.
    Re-adding an existing email updates it in place - correcting a typo or
    changing a role is just calling this again. `subjects` None leaves
    whatever is stored; a list replaces it."""
    conn.execute(
        """INSERT INTO staff (email, name, role, added_by, added_at, subjects)
           VALUES (?, ?, ?, ?, now(), ?)
           ON CONFLICT(email) DO UPDATE SET
               name = excluded.name, role = excluded.role,
               added_by = excluded.added_by,
               added_at = COALESCE(staff.added_at, excluded.added_at),
               subjects = COALESCE(excluded.subjects, staff.subjects)""",
        (email, name, role, added_by,
         json.dumps(subjects) if subjects is not None else None),
    )


def update_staff(conn, email, **fields):
    """Edit an existing row - name, role (None clears it back to "whatever
    EdSider says"), subjects (a list), active. Only these; anything else
    raises rather than being silently dropped."""
    allowed = {"name", "role", "subjects", "active"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"not an editable staff column: {sorted(bad)}")
    if "subjects" in fields:
        fields["subjects"] = (json.dumps(fields["subjects"])
                              if fields["subjects"] is not None else None)
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE staff SET {cols} WHERE email = ?", (*fields.values(), email))


def remove_staff_access(conn, email):
    """Take away what a superadmin granted. An EdSider teacher keeps their
    row and goes back to being a teacher (EdSider still lists them -
    `active` is how to stop them signing in); anyone else is removed,
    unless the teacher app has a record of them, which is kept with no
    role."""
    row = conn.execute(
        "SELECT in_edsider, first_seen_at FROM staff WHERE email = ?", (email,)
    ).fetchone()
    if row is None:
        return
    if row["in_edsider"] or row["first_seen_at"] is not None:
        conn.execute(
            "UPDATE staff SET role = NULL, added_by = NULL, added_at = NULL "
            "WHERE email = ?", (email,))
    else:
        conn.execute("DELETE FROM staff WHERE email = ?", (email,))


def sync_edsider_teachers(conn, teachers):
    """Bring the staff table in line with EdSider's current teachers, Data
    Entry Operators and Main Assessment Administrators - [{email, name,
    roles, positions, user_id, staff_id}] - in one transaction. A person
    EdSider lists as a teacher (TEACH) is marked in_edsider, a Data Entry
    Operator (DEOP) edsider_operator, a Main Assessment Administrator
    (MASAD, this app's admin) edsider_admin; anyone no longer listed is
    unmarked, so
    someone EdSider removes stops being able to sign in here through it.
    Rows are never deleted - their history and any superadmin settings
    stay. EdSider's name is taken unless a superadmin entered this person
    by hand (added_by set), whose name then wins.

    `roles` defaults to teacher, for a caller that only lists teachers.
    `positions` ("DEOP|Data Entry Operator", ...) is every EdSider role the
    person holds, Assessor included - kept for showing who someone is.
    `user_id` / `staff_id` are their two EdSider ids (staff_user.user_id,
    and registration.teacher's, which EdSider's operation and review
    tables use) - needed to name them in EdSider when a paper is sent.

    An empty list is refused rather than applied: EdSider has 133
    teachers, and a query that suddenly returns none is far more likely
    broken than true. Returns how many EdSider teachers are now marked."""
    if not teachers:
        raise ValueError("refusing to replace the teacher list with an empty one")
    # One statement, not one per person: each round trip to the Azure
    # server costs ~0.35 s, and row by row this took 44 s for 133
    # teachers - on every open of the assignment page.
    values = ", ".join(["(?, ?, ?, ?, ?, ?, ?, now())"] * len(teachers))
    params = []
    for t in teachers:
        roles = set(t.get("roles") or ["TEACH"])
        params += [t["email"], t.get("name"), "TEACH" in roles, "DEOP" in roles,
                   "MASAD" in roles, t.get("user_id"), t.get("staff_id")]
    conn.execute(
        f"""INSERT INTO staff (email, name, in_edsider, edsider_operator, edsider_admin,
                               edsider_user_id, edsider_staff_id, synced_at)
            VALUES {values}
            ON CONFLICT(email) DO UPDATE SET
                in_edsider = excluded.in_edsider,
                edsider_operator = excluded.edsider_operator,
                edsider_admin = excluded.edsider_admin,
                edsider_user_id = excluded.edsider_user_id,
                edsider_staff_id = excluded.edsider_staff_id,
                synced_at = excluded.synced_at,
                name = CASE WHEN staff.added_by IS NULL THEN excluded.name
                            ELSE staff.name END""",
        params,
    )
    # now() is the transaction's start time, the same value every row
    # above was stamped with - so anything marked but not stamped in this
    # run is no longer on EdSider's list.
    conn.execute(
        """UPDATE staff SET in_edsider = FALSE, edsider_operator = FALSE,
                             edsider_admin = FALSE
            WHERE (in_edsider OR edsider_operator OR edsider_admin)
              AND synced_at < now()""")
    # Positions are re-read whole, like the list itself.
    names = {}
    pairs = []
    for t in teachers:
        for item in t.get("positions") or []:
            code, _, name = str(item).partition("|")
            names[code] = name or code
            pairs.append((t["email"], code))
    conn.execute("DELETE FROM staff_position")
    if pairs:
        conn.execute(
            "INSERT INTO common.staff_role (code, name, synced_at) VALUES "
            + ", ".join(["(?, ?, now())"] * len(names))
            + " ON CONFLICT (code) DO UPDATE SET name = excluded.name, "
              "synced_at = excluded.synced_at",
            [v for code, name in names.items() for v in (code, name)])
        conn.execute(
            "INSERT INTO staff_position (email, role_code) VALUES "
            + ", ".join(["(?, ?)"] * len(pairs)) + " ON CONFLICT DO NOTHING",
            [v for pair in pairs for v in pair])
    return conn.execute(
        "SELECT count(*) AS n FROM staff WHERE in_edsider").fetchone()["n"]


def sync_edsider_lookups(conn, rows):
    """Bring our copy of EdSider's reference lists in line with theirs -
    rows of {kind, id, name, short_code, extra} - in one transaction, one
    table per list (common.<kind>). Every list present in `rows` is
    replaced: listed entries are stored active, entries no longer listed
    go inactive (never deleted - a paper may still name one). A list
    missing from `rows` entirely is left as it was. An empty pull is
    refused: EdSider always has grades and subjects, so none at all means
    a broken query. Returns {kind: active entries}."""
    if not rows:
        raise ValueError("refusing to replace EdSider's lists with nothing")
    by_kind = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r)
    unknown = set(by_kind) - set(COMMON_LISTS)
    if unknown:
        raise ValueError(f"not one of EdSider's lists here: {sorted(unknown)}")
    # One statement for all of them - each round trip to the Azure server
    # costs ~0.35 s (see sync_edsider_teachers).
    statements, params = [], []
    for kind, items in by_kind.items():
        extra = COMMON_LISTS[kind]
        columns = ["id", "name", "short_code", *extra]
        row_sql = "(" + ", ".join(["?"] * len(columns)) + ", TRUE, now())"
        statements.append(
            f"""INSERT INTO common.{kind} ({', '.join(columns)}, active, synced_at)
                VALUES {', '.join([row_sql] * len(items))}
                ON CONFLICT (id) DO UPDATE SET name = excluded.name,
                    short_code = excluded.short_code,
                    {''.join(f'{c} = excluded.{c}, ' for c in extra)}
                    active = TRUE, synced_at = excluded.synced_at""")
        for r in items:
            more = r.get("extra") or {}
            if isinstance(more, str):
                more = json.loads(more)
            short = r.get("short_code")
            params += [int(r["id"]), str(r.get("name") or "").strip(),
                       (str(short).strip() or None) if short is not None else None,
                       *[more.get(c) for c in extra]]
        # now() is this transaction's start - every row above carries it,
        # so an older stamp is an entry EdSider no longer lists.
        statements.append(f"UPDATE common.{kind} SET active = FALSE "
                          "WHERE synced_at IS NULL OR synced_at < now()")
    conn.execute(";\n".join(statements), params)
    return {r["kind"]: r["n"] for r in conn.execute(
        " UNION ALL ".join(f"SELECT '{kind}' AS kind, count(*) AS n FROM common.{kind} "
                           "WHERE active" for kind in COMMON_LISTS)).fetchall() if r["n"]}


def edsider_lookups(conn):
    """{kind: [{id, name, short_code, extra}]} - EdSider's active entries,
    in EdSider's own id order. `extra` holds a list's own columns
    (an author's type_id, ...), or None."""
    selects = []
    for kind, extra in COMMON_LISTS.items():
        more = ("json_build_object(" + ", ".join(f"'{c}', {c}" for c in extra) + ")::text"
                if extra else "NULL::text")
        selects.append(f"SELECT '{kind}' AS kind, id, name, short_code, {more} AS extra "
                       f"FROM common.{kind} WHERE active")
    out = {}
    for r in conn.execute(" UNION ALL ".join(selects) + " ORDER BY kind, id").fetchall():
        out.setdefault(r["kind"], []).append({
            "id": r["id"], "name": r["name"], "short_code": r["short_code"],
            "extra": json.loads(r["extra"]) if r["extra"] else None})
    return out


def edsider_lookups_synced_at(conn):
    row = conn.execute(
        "SELECT max(at) AS at FROM (" + " UNION ALL ".join(
            f"SELECT max(synced_at) AS at FROM common.{kind}" for kind in COMMON_LISTS)
        + ") x").fetchone()
    return row["at"] if row else None


def edsider_teachers_synced_at(conn):
    row = conn.execute(
        "SELECT max(synced_at) AS at FROM staff WHERE in_edsider").fetchone()
    return row["at"] if row else None


def upsert_teacher(conn, identity, display_name=None):
    """Record that this identity has opened the teacher review page -
    called once at the top of that page, every visit. First visit sets
    first_seen_at; every later visit only refreshes last_seen_at. A name
    is only filled in if none is stored - EdSider's or a superadmin's
    always wins over whatever the sign-in reported."""
    conn.execute(
        """INSERT INTO staff (email, name, first_seen_at, last_seen_at)
           VALUES (?, ?, now(), now())
           ON CONFLICT(email) DO UPDATE SET
               name = COALESCE(staff.name, excluded.name),
               first_seen_at = COALESCE(staff.first_seen_at, now()),
               last_seen_at = now()""",
        (identity, display_name),
    )


def assignable_teachers(conn):
    """Who a paper can be handed to: every active EdSider teacher, anyone
    a superadmin made a teacher, and anyone who has opened the teacher
    app (a test identity on a local run, mostly). `source` says which."""
    return conn.execute(
        """SELECT email AS identity, name AS display_name,
                  CASE WHEN in_edsider THEN 'edsider' ELSE 'added_here' END AS source
             FROM staff
            WHERE active
              AND (in_edsider OR role = 'teacher' OR first_seen_at IS NOT NULL)
            ORDER BY lower(coalesce(name, email))"""
    ).fetchall()


def files_in_database(conn):
    """Papers' files and pictures whose bytes are still in the database, not
    in file storage - [("paper", document id, bytes), ("image", image id,
    bytes)] - what scripts/move_files_to_storage.py moves."""
    return [(r["kind"], r["id"], r["size"]) for r in conn.execute(
        """SELECT 'paper' AS kind, document_id AS id, octet_length(file_blob) AS size
             FROM intake.document_file WHERE file_blob IS NOT NULL AND file_path IS NULL
           UNION ALL
           SELECT 'image', id, octet_length(image_blob) FROM intake.question_image
            WHERE image_blob IS NOT NULL AND image_path IS NULL
           ORDER BY 1, 2""").fetchall()]


def move_file_to_storage(conn, kind, item_id):
    """Put one paper's file ("paper", its document id) or one picture
    ("image", its id) into file storage and clear its bytes from the
    database. One at a time, so a stop part-way leaves every file readable
    from one place or the other."""
    if kind == "paper":
        row = conn.execute(
            """SELECT f.file_blob, d.filename FROM intake.document_file f
                 JOIN intake.document d ON d.id = f.document_id
                WHERE f.document_id = ? AND f.file_path IS NULL""", (item_id,)).fetchone()
        if row is None or row["file_blob"] is None:
            return
        data = bytes(row["file_blob"])
        path = files.paper_name(item_id, row["filename"], data)
        files.put(path, data, mimetypes.guess_type(row["filename"] or "")[0])
        if files.get(path) != data:
            raise RuntimeError(f"paper {item_id}: the stored copy does not match")
        conn.execute(
            """UPDATE intake.document_file SET file_path = ?, file_size = ?,
                      file_sha256 = ?, file_blob = NULL WHERE document_id = ?""",
            (path, len(data), files.checksum(data), item_id))
    elif kind == "image":
        row = conn.execute(
            """SELECT question_id, image_blob, content_type FROM intake.question_image
                WHERE id = ? AND image_path IS NULL""", (item_id,)).fetchone()
        if row is None or row["image_blob"] is None:
            return
        data = bytes(row["image_blob"])
        path = files.image_name(row["question_id"], item_id, row["content_type"])
        files.put(path, data, row["content_type"])
        if files.get(path) != data:
            raise RuntimeError(f"picture {item_id}: the stored copy does not match")
        conn.execute(
            """UPDATE intake.question_image SET image_path = ?, image_size = ?,
                      image_blob = NULL WHERE id = ?""", (path, len(data), item_id))
    else:
        raise ValueError(f"not a kind of stored file: {kind!r}")


def assignable_operators(conn):
    """Who a paper can be given to for data entry: everyone who may sign
    in as a Data Entry Operator ('employee') right now - EdSider's own
    operators and anyone a superadmin made one - with their EdSider
    positions, and where they come from."""
    return conn.execute(
        f"""SELECT email AS identity, name AS display_name,
                   {_POSITIONS_SQL} AS positions,
                   CASE WHEN edsider_operator THEN 'edsider' ELSE 'added_here' END AS source
              FROM staff
             WHERE ({STAFF_ROLE_SQL}) = 'employee'
             ORDER BY lower(coalesce(name, email))"""
    ).fetchall()


# ----------------------------------------- moving papers into `intake`

_OLD_DOCUMENT_COLUMNS = (
    "id, filename, file_type, uploaded_by, uploaded_at, paper_grade, paper_subject, "
    "paper_title, notes, split_style, status, assigned_to, sections, "
    "assessment_settings, instruction_groups, edsider_labels")


def migrate_old_intake_tables(conn):
    """One-off: copy the papers the old public-schema tables hold
    (intake_documents, intake_questions, their comments and pictures, and
    edsider_lookups) into `intake` and `common`. Run by hand, by
    scripts/migrate_intake_schema.py, at the moment the new version is
    deployed - never on startup, since a local run and the deployed apps
    share one database and the older version still writes to those tables.

    Ids are kept, so links, bookmarks and comments still point at the same
    paper and question. A paper already copied is skipped, so running it
    again copies only what was added since. The old tables are left as
    they are; drop them by hand once nothing runs the older version.

    A grade or subject the old row names that is not on EdSider's list is
    left unset and reported. The paper goes to its uploader for data entry
    when the uploader is a Data Entry Operator - before this, whoever
    uploaded a paper was the one working on it. Returns what was done."""
    def exists(table):
        return conn.execute("SELECT to_regclass(?) AS r", (table,)).fetchone()["r"]

    report = {"lists": 0, "documents": 0, "questions": 0, "comments": 0, "images": 0,
              "unmatched": [], "dropped_settings": [], "operators": 0}
    if exists("edsider_lookups") and not conn.execute(
            "SELECT 1 FROM common.grade LIMIT 1").fetchone():
        rows = [dict(r) for r in conn.execute(
            "SELECT kind, edsider_id AS id, name, short_code, extra FROM edsider_lookups "
            "WHERE active AND kind = ANY(?)", (list(COMMON_LISTS),)).fetchall()]
        if rows:
            sync_edsider_lookups(conn, rows)
            report["lists"] = len(rows)
    if not exists("intake_documents"):
        return report

    done = {r["id"] for r in conn.execute("SELECT id FROM intake.document").fetchall()}
    todo = [d for d in conn.execute(
        f"SELECT {_OLD_DOCUMENT_COLUMNS} FROM intake_documents ORDER BY id").fetchall()
        if d["id"] not in done]
    if not todo:
        return report
    ids = [d["id"] for d in todo]

    clash = conn.execute(
        """SELECT document_id, seq FROM intake_questions WHERE document_id = ANY(?)
            GROUP BY document_id, seq HAVING count(*) > 1""", (ids,)).fetchall()
    if clash:
        raise ValueError("questions sharing a place on one paper (document, seq): "
                         + ", ".join(f"({r['document_id']}, {r['seq']})" for r in clash))

    operators = {r["identity"] for r in assignable_operators(conn)}
    for d in todo:
        grade_id = list_id(conn, "grade", d["paper_grade"], strict=False)
        subject_id = list_id(conn, "subject", d["paper_subject"], strict=False)
        for kind, value, found in (("grade", d["paper_grade"], grade_id),
                                   ("subject", d["paper_subject"], subject_id)):
            if value and found is None:
                report["unmatched"].append((d["id"], kind, value))
        labels = {}
        for key, value in json.loads(d["edsider_labels"] or "{}").items():
            kind = DOCUMENT_LABELS.get(key)
            if kind and value is not None and conn.execute(
                    f"SELECT 1 FROM common.{kind} WHERE id = ?", (value,)).fetchone():
                labels[key] = value
            elif kind and value is not None:
                report["unmatched"].append((d["id"], kind, value))
        columns = ["id", "filename", "file_type", "title", "grade_id", "subject_id",
                   "notes", "split_style", "status", "uploaded_by", "uploaded_at",
                   *labels]
        conn.execute(
            f"INSERT INTO intake.document ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' * len(columns))})",
            (d["id"], d["filename"], d["file_type"], d["paper_title"], grade_id,
             subject_id, d["notes"], d["split_style"], d["status"], d["uploaded_by"],
             d["uploaded_at"], *labels.values()))
        sections = [{k: v for k, v in s.items() if k in _PART_KEYS}
                    for s in json.loads(d["sections"] or "[]")]
        _upsert_parts(conn, d["id"], sections)
        _replace_groups(conn, d["id"], json.loads(d["instruction_groups"] or "[]"))
        settings = json.loads(d["assessment_settings"] or "{}")
        extra = set(settings) - set(_SETTINGS_COLUMNS) - {"marking"}
        if extra:
            report["dropped_settings"].append((d["id"], sorted(extra)))
        if settings:
            _save_settings(conn, d["id"], {k: v for k, v in settings.items()
                                           if k not in extra})
        if d["uploaded_by"] in operators:
            conn.execute(
                "INSERT INTO intake.review (document_id, stage, assigned_to, finished_at) "
                "VALUES (?, 'data_entry', ?, CASE WHEN ? THEN now() END)",
                (d["id"], d["uploaded_by"],
                 d["status"] not in ("extracted", "in_review", "refer_back")))
            report["operators"] += 1
        if d["assigned_to"]:
            conn.execute(
                "INSERT INTO intake.review (document_id, stage, assigned_to, finished_at) "
                "VALUES (?, 'teacher', ?, CASE WHEN ? THEN now() END)",
                (d["id"], d["assigned_to"], d["status"] != "teacher_review"))
        _log(conn, d["id"], "moved", actor="migrate_intake_schema", to_status=d["status"])

    conn.execute(
        """INSERT INTO intake.document_file (document_id, file_blob, raw_text, extractor)
           SELECT id, file_blob, raw_text, extractor FROM intake_documents
            WHERE id = ANY(?)""", (ids,))
    for r in conn.execute(
            """SELECT DISTINCT document_id, section FROM intake_questions
                WHERE document_id = ANY(?) AND section IS NOT NULL""", (ids,)).fetchall():
        _part_id_for(conn, r["document_id"], r["section"], _part_ids(conn, r["document_id"]))
    report["documents"] = len(ids)
    report["questions"] = conn.execute(
        """INSERT INTO intake.question
           (id, document_id, part_id, seq, parent_seq, question_number, text, marks,
            topic, answer, question_type, extraction_confidence, reviewed,
            teacher_reviewed, last_edited_by, last_edited_at, created_at)
           SELECT q.id, q.document_id,
                  (SELECT p.id FROM intake.part p WHERE p.document_id = q.document_id
                      AND p.label = q.section ORDER BY p.seq LIMIT 1),
                  q.seq, q.parent_seq, q.question_number, q.text, q.marks, q.topic,
                  q.answer, q.question_type, q.extraction_confidence,
                  coalesce(q.reviewed, 0) <> 0, coalesce(q.teacher_reviewed, 0) <> 0,
                  q.last_edited_by, q.last_edited_at, q.created_at::timestamptz
             FROM intake_questions q WHERE q.document_id = ANY(?)""", (ids,)).rowcount
    report["comments"] = conn.execute(
        """INSERT INTO intake.question_comment (id, question_id, author, comment, created_at)
           SELECT c.id, c.question_id, c.author, c.comment, c.created_at
             FROM intake_question_comments c
             JOIN intake_questions q ON q.id = c.question_id
            WHERE q.document_id = ANY(?)""", (ids,)).rowcount
    report["images"] = conn.execute(
        """INSERT INTO intake.question_image
           (id, question_id, slot, image_blob, content_type, created_at)
           SELECT i.id, i.question_id, i.slot, i.image_blob, i.content_type, i.created_at
             FROM intake_question_images i
             JOIN intake_questions q ON q.id = i.question_id
            WHERE q.document_id = ANY(?)""", (ids,)).rowcount
    # Ids were copied, not generated - the identity counters must start
    # after them, or the next upload collides with a copied paper.
    for table in ("document", "question", "question_comment", "question_image"):
        conn.execute(
            f"""SELECT setval(pg_get_serial_sequence('intake.{table}', 'id'),
                              (SELECT coalesce(max(id), 0) + 1 FROM intake.{table}), false)""")
    return report
