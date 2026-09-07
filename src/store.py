"""Database layer.

Everything that touches storage lives here. This is the seam: swapping
SQLite for PostgreSQL + pgvector later means changing this one module,
not the rest of the codebase.
"""

import json
import os
import sqlite3
from pathlib import Path

# The sample bank and a real EdSider import must not share a database -
# mixing them makes tune.py score 41,000 real questions against a 30-row
# answer key. Set EDSIDER_DB to keep them apart:
#
#   $env:EDSIDER_DB = "data/edsider_real.db"
DB_PATH = Path(os.environ.get(
    "EDSIDER_DB",
    Path(__file__).resolve().parent.parent / "data" / "edsider.db",
))

SCHEMA = """
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
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP
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
    vector       TEXT NOT NULL,
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Every human verdict on a flagged pair is recorded. This becomes the
-- evaluation set, the threshold tuner, and later, training data.
CREATE TABLE IF NOT EXISTS decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    question_a  TEXT NOT NULL,
    question_b  TEXT NOT NULL,
    similarity  REAL,
    guard_flags TEXT,
    verdict     TEXT NOT NULL CHECK (verdict IN ('duplicate','distinct','skipped')),
    decided_by  TEXT NOT NULL,
    decided_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Bookkeeping for scripts/sync.py: how far the last pull from EdSider's
-- own database got, per source, so the next run asks for only what is
-- new rather than re-reading everything. This is OUR state about a
-- sync, not a copy of anything from their side.
CREATE TABLE IF NOT EXISTS sync_state (
    source          TEXT PRIMARY KEY,
    last_id         INTEGER NOT NULL DEFAULT 0,
    last_synced_at  TEXT
);
"""


# Schema work runs once per process rather than once per connection.
# Reading scripts do not call init(), so without this an older database
# reaches a query that names a column it does not yet have.
_schema_ready = False


def connect():
    global _schema_ready
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # WAL lets a reader (the service answering a check) proceed while a
    # writer (a reviewer's decision, the importer) is mid-transaction.
    # The default journal mode blocks readers behind writers, which is
    # fine for a CLI run alone but not for a process serving requests
    # continuously. Cheap and idempotent to set on every connection.
    conn.execute("PRAGMA journal_mode=WAL")
    if not _schema_ready:
        _apply_schema(conn)
        conn.commit()
        _schema_ready = True
    return conn


# Columns added after a database already existed. CREATE TABLE IF NOT
# EXISTS silently does nothing to a table that is already there, so an
# older database would keep the old shape and every insert would fail.
MIGRATIONS = [
    ("questions", "title", "TEXT"),
    ("questions", "shape", "TEXT NOT NULL DEFAULT 'standalone'"),
]

# A question can only be compared on its text when the text IS the whole
# question. That rules out two groups, both measured on the real bank:
# child rows that depend on a parent for their context, and rows whose
# content is an image or equation - "Simplify" appears 110 times, each
# time against a different expression that lives in an attachment.
COMPARABLE = "shape = 'standalone' AND has_figure = 0"


def use_database(path):
    """Point every subsequent connection at a different file.

    The sample bank and a real import live in separate databases, and
    the UI lets a reviewer switch between them. Keeping that here means
    callers never touch DB_PATH or the schema flag themselves.
    """
    global DB_PATH, _schema_ready
    DB_PATH = Path(path)
    _schema_ready = False
    return DB_PATH


def _apply_schema(conn):
    conn.executescript(SCHEMA)
    for table, column, decl in MIGRATIONS:
        present = {r["name"] for r in
                   conn.execute(f"PRAGMA table_info({table})")}
        if column not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init():
    """Kept for callers that want the schema created explicitly.
    connect() already does this once per process."""
    with connect() as conn:
        _apply_schema(conn)


def save(conn, q):
    conn.execute(
        """INSERT OR REPLACE INTO questions
           (id, text, title, grade, subject, topic, marks, answer,
            language, contributor, source_file, content_hash, has_figure,
            shape)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            q["id"], q["text"], q.get("title"),
            q.get("grade"), q.get("subject"),
            q.get("topic"), q.get("marks"), q.get("answer"),
            q.get("language", "en"), q.get("contributor"),
            q.get("source_file"), q["content_hash"],
            1 if q.get("has_figure") else 0,
            q.get("shape", "standalone"),
        ),
    )


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
                   COUNT(*)         AS copies,
                   GROUP_CONCAT(id) AS ids,
                   MIN(text)        AS sample
            FROM questions
            WHERE {COMPARABLE}
            GROUP BY content_hash
            HAVING copies > 1
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
        """INSERT OR REPLACE INTO embeddings (content_hash, model, vector)
           VALUES (?,?,?)""",
        (content_hash, model, json.dumps([float(x) for x in vector])),
    )


def get_embedding(conn, content_hash, model):
    row = conn.execute(
        "SELECT vector FROM embeddings WHERE content_hash = ? AND model = ?",
        (content_hash, model),
    ).fetchone()
    return json.loads(row["vector"]) if row else None


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


def questions_with_embeddings(conn, model):
    """Every question joined to its fingerprint, comparable or not."""
    return conn.execute(
        """SELECT q.*, e.vector
           FROM questions q
           JOIN embeddings e
             ON e.content_hash = q.content_hash AND e.model = ?
           ORDER BY q.id""",
        (model,),
    ).fetchall()


def comparable_questions(conn, model):
    """The rows a duplicate check may actually score.

    This is what every caller wants. `questions_with_embeddings` remains
    for the rare case that needs the unfiltered set.
    """
    return conn.execute(
        f"""SELECT q.*, e.vector
            FROM questions q
            JOIN embeddings e
              ON e.content_hash = q.content_hash AND e.model = ?
            WHERE {COMPARABLE}
            ORDER BY q.id""",
        (model,),
    ).fetchall()


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
                    similarity=None, guard_flags=None):
    conn.execute(
        """INSERT INTO decisions
           (question_a, question_b, similarity, guard_flags, verdict, decided_by)
           VALUES (?,?,?,?,?,?)""",
        (a, b, similarity, guard_flags, verdict, decided_by),
    )
