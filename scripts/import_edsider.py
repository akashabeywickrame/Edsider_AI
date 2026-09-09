"""Import a real EdSider export into the local database.

Usage:
    python scripts/import_edsider.py path\\to\\export.csv
    python scripts/import_edsider.py path\\to\\export.json

The rest of the pipeline does not care where questions came from, so
once this has run, embed.py and find_similar.py work unchanged.

COLUMN MAPPING is the only thing you should need to edit. Set it to
match the column names in EdSider's export.

Producing the export
--------------------
Grade, subject and language are separate tables, so the questions row
holds only ids. Join them, or the import stores numbers where a reviewer
expects names. Run from psql - \\copy writes client-side and needs no
server privileges:

    \\copy (
      SELECT q.id,
             q.question_title,
             q.question_content,
             g.name AS grade_name,
             s.name AS subject_name,
             l.name AS language_name,
             q.created_by,
             q.created_at
      FROM questions q
      LEFT JOIN grades    g ON g.id = q.grade_id
      LEFT JOIN subjects  s ON s.id = q.subject_id
      LEFT JOIN languages l ON l.id = q.language_id
      WHERE q.deleted_at IS NULL
    ) TO 'edsider_questions.csv' WITH (FORMAT csv, HEADER)

LEFT JOIN, not JOIN - an inner join would silently drop every question
whose grade or subject is unset, which is exactly the data worth seeing.

Confirm the real table and column names first:

    SELECT column_name, data_type FROM information_schema.columns
    WHERE table_name = 'questions' ORDER BY ordinal_position;

FORMAT csv matters. Draft.js content is JSON full of commas, quotes and
newlines, and RFC4180 quoting is what carries it intact. Do not open the
result in Excel and re-save - it truncates cells at 32,767 characters.
"""

import csv
import json
import sys
from pathlib import Path

# A single Draft.js cell can carry a base64 image. The largest in the
# real bank is 6.5 MB, against a default limit of 128 KB - without this
# the reader raises "field larger than field limit" on the first one.
csv.field_size_limit(512 * 1024 * 1024)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import store
from src.normalize import content_hash, extract_text, has_attachments

# ---------------------------------------------------------------------
# EDIT THIS to match EdSider's actual column names.
# Left side  = our field name.
# Right side = list of possible column names in their export; the first
#              one that exists is used.
# ---------------------------------------------------------------------
COLUMN_MAP = {
    "id":          ["id", "question_id", "qid"],
    # question_content is EdSider's own name for the field that holds the
    # question itself, and it goes first for that reason. Their editor
    # also has a question_title, a passage and a marking_guidance; none of
    # those is the question, so none of them belongs here.
    "text":        ["question_content", "question", "question_text",
                    "text", "body", "content"],
    "title":       ["question_title", "title", "name", "label"],
    # Grade, subject and language live in their own tables in EdSider, so
    # the questions row holds only an id. The readable name is preferred
    # when the export joins it in - the metadata guard compares these for
    # equality, which works on either, but "Mathematics" is a far better
    # thing to show a reviewer than "7".
    "grade":       ["grade_name", "grade", "grade_level", "class",
                    "grade_id"],
    "subject":     ["subject_name", "subject", "subject_id"],
    "topic":       ["topic_name", "topic", "unit", "syllabus_unit",
                    "lesson", "chapter", "topic_id", "chapter_id"],
    "marks":       ["marks", "mark", "score", "allocated_marks"],
    "answer":      ["answer", "correct_answer", "ans", "solution"],
    "language":    ["language_name", "language", "lang", "medium",
                    "language_id"],
    "contributor": ["contributor", "created_by", "author", "teacher"],
    "source_file": ["source_file", "source", "paper", "paper_name"],
    # Used to work out a row's shape, not stored directly.
    "sub_questions":  ["sub_questions", "subquestions", "children",
                       "child_ids"],
    "is_subquestion": ["is_subquestion", "issubquestion", "is_sub"],
}


def row_shape(row):
    """Is this a whole question, one part of one, or the wrapper?

    EdSider stores a multi-part question as a parent row carrying the
    shared stimulus and the instruction, plus one child row per part:

        id=842  sub_questions: 837,838,...
                "Write the answer ... using below food chain
                 Grass -> Grass hopper -> Frog -> Snake"
        id=837  "13.How many links of the above food chain"

    The child cannot be compared on its own text - "How many links of the
    above food chain" appears under six different food chains and means
    something different each time. The parent is an instruction, and on
    the real bank parents are 43% redundant simply because "Fill in the
    blanks" recurs. Only standalone rows are safe to compare.
    """
    if _str(pick(row, COLUMN_MAP["sub_questions"])) is not None:
        return "parent"
    is_sub = _str(pick(row, COLUMN_MAP["is_subquestion"]))
    if is_sub is not None and is_sub.lower() in ("true", "1", "yes", "t"):
        return "sub-question"
    return "standalone"


def pick(row, candidates):
    """First matching column, case-insensitive."""
    lowered = {k.lower().strip(): v for k, v in row.items() if k}
    for name in candidates:
        if name in lowered and lowered[name] not in (None, ""):
            return lowered[name]
    return None


def to_question(row, index):
    text = extract_text(pick(row, COLUMN_MAP["text"]))
    if not text:
        return None

    qid = pick(row, COLUMN_MAP["id"]) or f"IMP{index:06d}"

    marks = pick(row, COLUMN_MAP["marks"])
    try:
        marks = int(float(marks)) if marks is not None else None
    except (TypeError, ValueError):
        marks = None

    return {
        "id": str(qid),
        "text": text,
        # Stored but deliberately NOT fingerprinted. A title is a label
        # for the question, not the question, and two different questions
        # can share one.
        "title": extract_text(pick(row, COLUMN_MAP["title"])) or None,
        "grade": _str(pick(row, COLUMN_MAP["grade"])),
        "subject": _str(pick(row, COLUMN_MAP["subject"])),
        "topic": _str(pick(row, COLUMN_MAP["topic"])),
        "marks": marks,
        "answer": extract_text(pick(row, COLUMN_MAP["answer"])),
        "language": _str(pick(row, COLUMN_MAP["language"])) or "en",
        "contributor": _str(pick(row, COLUMN_MAP["contributor"])),
        "source_file": _str(pick(row, COLUMN_MAP["source_file"])),
        "content_hash": content_hash(text),
        "has_figure": has_attachments(pick(row, COLUMN_MAP["text"])),
        "shape": row_shape(row),
        # Only ever populated on a parent row - row_shape() returns
        # "parent" for exactly the rows where this is present. Kept
        # verbatim so store.link_children_to_parents() can derive
        # parent_id on the children later, in any order, any number of
        # times.
        "children_raw": _str(pick(row, COLUMN_MAP["sub_questions"])),
    }


def _str(v):
    # EdSider's export writes SQL NULL as the literal four characters
    # NULL, unquoted. Left alone it becomes the string "NULL", which the
    # metadata guard would then happily compare against another "NULL"
    # and call a match. It also makes every fill rate read 100%.
    if v is None or str(v).strip() in ("", "NULL", "None", "\\N"):
        return None
    return str(v).strip()


def read_rows(path):
    """Yield rows one at a time.

    A generator rather than a list because a real export is large - the
    bank measured 684 MB across 41,139 rows, and materialising that as
    Python dicts costs several gigabytes for no reason. Nothing
    downstream needs more than one row at a time.
    """
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for key in ("questions", "data", "rows", "results"):
                if key in data:
                    data = data[key]
                    break
            else:
                data = [data]
        yield from data
        return

    with path.open(encoding="utf-8-sig", newline="") as f:
        yield from csv.DictReader(f)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"\nFile not found: {path}\n")
        return

    print(f"\nReading {path.name} "
          f"({path.stat().st_size / 1024 / 1024:,.0f} MB)")

    store.init()
    imported, skipped, with_figures, seen = 0, 0, 0, 0
    columns_shown = False

    # Which fields actually arrive populated decides what the rest of the
    # pipeline can rely on. The metadata guard is silent without grade and
    # subject, so a low fill rate here means the guard layer is weaker on
    # real data than the sample set suggests. Better to see the number
    # than to assume.
    # Counted from the RAW row rather than the mapped question, because
    # to_question() fills in defaults - language becomes "en" when absent,
    # which would report as 100% populated and tell us nothing.
    tracked = ("title", "grade", "subject", "language",
               "topic", "marks", "answer")
    filled = {f: 0 for f in tracked}

    with store.connect() as conn:
        for i, row in enumerate(read_rows(path), start=1):
            if not columns_shown:
                print(f"Columns found: "
                      f"{', '.join(str(k) for k in row.keys())}\n")
                columns_shown = True

            seen = i
            q = to_question(row, i)
            if q is None:
                skipped += 1
                continue
            store.save(conn, q)
            imported += 1
            if q["has_figure"]:
                with_figures += 1
            for f in tracked:
                # _str, not pick - the export writes SQL NULL as the
                # literal string "NULL", which pick returns happily and
                # which would make every fill rate read 100%.
                if _str(pick(row, COLUMN_MAP[f])) is not None:
                    filled[f] += 1

            # A real bank takes a while. Say something, or it looks hung.
            if imported % 5000 == 0:
                print(f"  ... {imported:,} imported")

        total = store.count(conn)

    # Every parent row now carries its own children_raw; use it to link
    # each child to its parent_id. Runs once per import, over every
    # parent currently in the database (not just this batch's), so it
    # is correct regardless of which rows arrived in which run.
    with store.connect() as conn:
        linked = store.link_children_to_parents(conn)

    print(f"Rows read         : {seen}")
    print(f"Imported          : {imported}")
    print(f"Skipped (no text) : {skipped}")
    print(f"Contain images    : {with_figures}  (text alone is incomplete)")
    print(f"Database total    : {total}")
    print(f"Sub-questions linked to a parent : {linked}")

    if imported:
        print("\nROW SHAPES (only standalone rows without figures are")
        print("compared - the rest cannot be judged on their text alone)")
        print("-" * 62)
        with store.connect() as conn:
            comparable = 0
            for r in store.shape_counts(conn):
                comparable += r["comparable"]
                print(f"  {r['shape']:<14} {r['total']:>7,} rows   "
                      f"{r['figures']:>6,} with figures   "
                      f"{r['comparable']:>7,} comparable")
        print("-" * 62)
        print(f"  {comparable:,} of {total:,} questions can be compared "
              f"on their text ({comparable/total*100:.1f}%)")

        print("\nFIELD FILL RATES (of the rows imported)")
        print("-" * 46)
        for f, n in filled.items():
            pct = n / imported * 100
            note = ""
            if f in ("grade", "subject") and pct < 50:
                note = "  <-- metadata guard needs this"
            print(f"  {f:<12} {n:>7} / {imported}  {pct:5.1f}%{note}")
        if with_figures:
            print(f"\n  {with_figures} question(s) contain an image or equation.")
            print("  For those the text alone is not the whole question, so a")
            print("  duplicate verdict on them should go to a human.")

    # A handful of skips is normal - the real bank holds 542 rows whose
    # content is an empty Draft.js block. Skipping most of the file is
    # the signal that matters, and it means the mapping is wrong.
    if skipped and imported == 0:
        print("\nEVERY row was skipped, so COLUMN_MAP does not match this")
        print("export. Check the column list printed above and edit the")
        print("mapping at the top of this file.")
    elif skipped > imported * 0.05:
        print(f"\n{skipped:,} rows had no question text - more than 5% of the")
        print("file. Check COLUMN_MAP against the column list above.")
    elif skipped:
        print(f"\n{skipped:,} rows had no question text (empty content).")

    print("\nNext:")
    print("  python scripts/embed.py")
    print("  python scripts/find_duplicates.py")
    print("  streamlit run app.py\n")


if __name__ == "__main__":
    main()
