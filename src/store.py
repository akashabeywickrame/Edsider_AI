"""Database layer.

Everything that touches storage lives here. This is the seam: swapping
SQLite for PostgreSQL + pgvector later means changing this one module,
not the rest of the codebase.
"""

import json
import os
import sqlite3
from pathlib import Path

# No default database. There used to be one - a 30-question sample bank
# - and silently falling back to it when EDSIDER_DB was forgotten was
# already documented as dangerous: it would open with the sample's
# planted duplicates showing as though they were EdSider's own. The
# sample bank is gone now, which makes the silent fallback worse, not
# safer - SQLite would happily create a brand new EMPTY database at
# whatever path was guessed, and duplicate checking would silently
# report "nothing found" for everything, with no error at all. connect()
# refuses to run instead.
#
#   $env:EDSIDER_DB = "data/edsider_real.db"
#
# None here is a real value, not a bug - it means "not configured yet",
# checked explicitly by connect(). Tests never rely on this default;
# every fixture calls use_database() before connect().
DB_PATH = Path(os.environ["EDSIDER_DB"]) if os.environ.get("EDSIDER_DB") else None

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

-- Which assessments (exam papers) each question appears on. Mirrors
-- EdSider's own assessment.assessment_question junction table directly -
-- a question can legitimately appear on more than one assessment (the
-- same item reused across two Grade 10 Term 1 papers is normal, not a
-- bug), so this is a many-to-many link, not a column on questions.
-- Keyed on link_id - their own assessment_question_id - so a re-synced
-- row replaces itself instead of duplicating.
CREATE TABLE IF NOT EXISTS question_assessments (
    link_id          INTEGER PRIMARY KEY,
    assessment_id    TEXT NOT NULL,
    question_id      TEXT NOT NULL,
    question_number  INTEGER,
    mark_allocation  REAL,
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
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
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
    -- 'extracted'  first pass done, nobody has looked yet
    -- 'in_review'  a reviewer has started editing
    -- 'checked'    within-batch duplicate check has been run
    status        TEXT NOT NULL DEFAULT 'extracted',
    uploaded_at   TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS intake_questions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
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
"""


# Schema work runs once per process rather than once per connection.
# Reading scripts do not call init(), so without this an older database
# reaches a query that names a column it does not yet have.
_schema_ready = False


def connect():
    global _schema_ready
    if DB_PATH is None:
        raise RuntimeError(
            "No database configured. Set EDSIDER_DB to a real .db file "
            "before running anything, e.g.:\n\n"
            '    $env:EDSIDER_DB = "data/edsider_real.db"\n\n'
            "or call store.use_database(path) directly."
        )
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


def use_database(path):
    """Point every subsequent connection at a different file.

    Different databases exist for different purposes (a real import vs.
    a throwaway one a test builds in tmp_path), and the UI lets a
    reviewer switch between them. Keeping that here means callers never
    touch DB_PATH or the schema flag themselves.

    `path=None` resets to "not configured" - the same state DB_PATH
    starts in when EDSIDER_DB was never set. A test that saves the
    original DB_PATH before pointing at its own tmp_path database, then
    restores it afterward, must be able to pass that original value
    straight back through here even when it was None.
    """
    global DB_PATH, _schema_ready
    DB_PATH = Path(path) if path is not None else None
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
            language, contributor, source_file, content_hash, has_figure,
            shape, children_raw)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
               text=excluded.text, title=excluded.title,
               grade=excluded.grade, subject=excluded.subject,
               topic=excluded.topic, marks=excluded.marks,
               answer=excluded.answer, language=excluded.language,
               contributor=excluded.contributor,
               source_file=excluded.source_file,
               content_hash=excluded.content_hash,
               has_figure=excluded.has_figure, shape=excluded.shape,
               children_raw=excluded.children_raw""",
        (
            q["id"], q["text"], q.get("title"),
            q.get("grade"), q.get("subject"),
            q.get("topic"), q.get("marks"), q.get("answer"),
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

    Returns how many child rows carry a parent_id after this call.
    """
    parents = conn.execute(
        "SELECT id, children_raw FROM questions "
        "WHERE shape = 'parent' AND children_raw IS NOT NULL"
    ).fetchall()

    for parent in parents:
        child_ids = [c.strip() for c in parent["children_raw"].split(",")
                    if c.strip()]
        for cid in child_ids:
            conn.execute(
                "UPDATE questions SET parent_id = ? WHERE id = ?",
                (parent["id"], cid),
            )

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
    return conn.execute(
        f"""SELECT q.*, e.vector
            FROM questions q
            JOIN embeddings e
              ON e.content_hash = q.content_hash AND e.model = ?
            WHERE {COMPARABLE_CHILD}
            ORDER BY q.id""",
        (model,),
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
                    similarity=None, guard_flags=None,
                    candidate_contrast_terms=None):
    """`candidate_contrast_terms` is (only_in_a, only_in_b) from
    guards.candidate_contrast_terms() - only meaningful, and only ever
    passed by a caller, when verdict is 'distinct': what vocabulary
    might explain why a pair the system flagged turned out not to be a
    duplicate. Stored as JSON for a person to review later; nothing
    reads it back automatically."""
    conn.execute(
        """INSERT INTO decisions
           (question_a, question_b, similarity, guard_flags, verdict,
            decided_by, candidate_contrast_terms)
           VALUES (?,?,?,?,?,?,?)""",
        (a, b, similarity, guard_flags, verdict, decided_by,
         json.dumps(candidate_contrast_terms) if candidate_contrast_terms else None),
    )


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
    return conn.execute(
        f"""SELECT q.*, e.vector, qa.question_number
            FROM question_assessments qa
            JOIN questions  q ON q.id = qa.question_id
            JOIN embeddings e ON e.content_hash = q.content_hash
                              AND e.model = ?
            WHERE qa.assessment_id = ? AND {COMPARABLE}
            ORDER BY qa.question_number""",
        (model, assessment_id),
    ).fetchall()


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
                           sections=None):
    """Record one uploaded file. Returns its new id.

    `sections` is the paper's list of parts from
    intake.extract.structure(), kept as JSON - read it back with
    intake_sections().
    """
    cur = conn.execute(
        """INSERT INTO intake_documents
           (filename, file_type, extractor, uploaded_by, paper_grade,
            paper_subject, raw_text, split_style, paper_title, sections)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (filename, file_type, extractor, uploaded_by, paper_grade,
         paper_subject, raw_text, split_style, paper_title,
         json.dumps(sections) if sections is not None else None),
    )
    return cur.lastrowid


def intake_sections(doc):
    """An uploaded paper's parts, or [] for one read before papers were
    split into parts. `doc` is an intake_documents row or a dict of one."""
    raw = doc["sections"] if "sections" in doc.keys() else None
    return json.loads(raw) if raw else []


def add_intake_question(conn, document_id, seq, text, question_number=None,
                        grade=None, subject=None, marks=None, topic=None,
                        answer=None, extraction_confidence="ok",
                        section=None):
    conn.execute(
        """INSERT INTO intake_questions
           (document_id, seq, question_number, text, grade, subject,
            marks, topic, answer, extraction_confidence, section)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (document_id, seq, question_number, text, grade, subject, marks,
         topic, answer, extraction_confidence, section),
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
        )


def intake_documents(conn):
    return conn.execute(
        "SELECT * FROM intake_documents ORDER BY id DESC"
    ).fetchall()


def get_intake_document(conn, doc_id):
    return conn.execute(
        "SELECT * FROM intake_documents WHERE id = ?", (doc_id,)
    ).fetchone()


def intake_questions(conn, doc_id):
    return conn.execute(
        "SELECT * FROM intake_questions WHERE document_id = ? ORDER BY seq",
        (doc_id,),
    ).fetchall()


_INTAKE_Q_EDITABLE = {
    "question_number", "text", "grade", "subject", "marks", "topic",
    "answer", "reviewed", "extraction_confidence", "section",
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
    number - that a reviewer drops entirely."""
    conn.execute("DELETE FROM intake_questions WHERE id = ?", (q_id,))


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
