"""Tests for importing a real EdSider export.

The export is a CSV whose question_content column holds Draft.js JSON -
so it is JSON, containing commas, quotes and newlines, inside a CSV cell.
Every one of those is a chance to corrupt the text before it is ever
fingerprinted, which is why the round trip is tested rather than assumed.

Grade, subject and language are separate tables in EdSider, so the export
joins them and the columns arrive as grade_name / subject_name.
"""

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.import_edsider import (COLUMN_MAP, pick, to_question, read_rows,
                                    row_shape)


def draftjs(text, with_image=False):
    """A Draft.js payload of the shape react-draft-wysiwyg produces."""
    state = {
        "blocks": [
            {"key": "a1b2c", "text": text, "type": "unstyled",
             "depth": 0, "inlineStyleRanges": [], "entityRanges": []}
        ],
        "entityMap": (
            {"0": {"type": "IMAGE", "mutability": "MUTABLE",
                   "data": {"src": "https://edsider.lk/img/9.png"}}}
            if with_image else {}
        ),
    }
    return json.dumps(state)


ROWS = [
    {
        "id": "101",
        "question_title": "Photosynthesis basics",
        "question_content": draftjs(
            "Explain photosynthesis in green plants, including the "
            "role of chlorophyll."),
        "passage": "",
        "grade_name": "8",
        "subject_name": "Science",
        "language_name": "English",
    },
    {
        "id": "102",
        "question_title": "Rectangle area",
        # Embedded newline and quotes - the things CSV quoting must carry.
        "question_content": draftjs(
            'Calculate the area of a rectangle.\nUse "length x width", '
            "and show your working."),
        "passage": "",
        "grade_name": "8",
        "subject_name": "Mathematics",
        "language_name": "English",
    },
    {
        "id": "103",
        "question_title": "",
        "question_content": draftjs(
            "Identify the parts labelled in the diagram.", with_image=True),
        "passage": "",
        "grade_name": "",
        "subject_name": "Science",
        "language_name": "English",
    },
]


def write_csv(path):
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(ROWS[0].keys()))
        w.writeheader()
        w.writerows(ROWS)
    return path


# ---------- the mapping ----------

def test_question_content_is_the_text_field():
    """This was a real blocker: question_content was absent from the
    candidate list, so pick() returned None and EVERY row was skipped."""
    assert pick(ROWS[0], COLUMN_MAP["text"]) is not None


def test_joined_name_columns_map_to_grade_and_subject():
    """Grade and subject are separate tables; the export joins the names."""
    q = to_question(ROWS[0], 1)
    assert q["grade"] == "8"
    assert q["subject"] == "Science"


def test_id_column_is_preferred_over_a_generated_one():
    assert to_question(ROWS[0], 1)["id"] == "101"


def test_title_is_captured_but_not_fingerprinted():
    a = to_question(ROWS[0], 1)
    assert a["title"] == "Photosynthesis basics"
    # The hash must come from the question, not the label. Same question
    # under a different title is still the same question.
    changed = dict(ROWS[0], question_title="Something else entirely")
    assert to_question(changed, 1)["content_hash"] == a["content_hash"]


def test_missing_title_becomes_none_not_empty_string():
    assert to_question(ROWS[2], 3)["title"] is None


# ---------- the round trip ----------

def test_draftjs_survives_the_csv_round_trip(tmp_path):
    # read_rows streams, so a real import never holds the file in memory.
    # These tests want random access, hence the list().
    rows = list(read_rows(write_csv(tmp_path / "export.csv")))
    assert len(rows) == 3

    q = to_question(rows[1], 2)
    assert "Calculate the area of a rectangle." in q["text"]
    assert 'Use "length x width"' in q["text"]
    # The Draft.js wrapper must be gone, not merely present as text.
    assert "blocks" not in q["text"]
    assert "entityMap" not in q["text"]


def test_identical_text_hashes_identically_through_the_csv(tmp_path):
    """A question read from the export and the same question typed by
    hand must land on one fingerprint, or Pass 1 finds nothing."""
    # read_rows streams, so a real import never holds the file in memory.
    # These tests want random access, hence the list().
    rows = list(read_rows(write_csv(tmp_path / "export.csv")))
    from_csv = to_question(rows[0], 1)

    from src.normalize import content_hash
    typed = ("Explain photosynthesis in green plants, including the "
             "role of chlorophyll.")
    assert from_csv["content_hash"] == content_hash(typed)


def test_image_questions_are_flagged(tmp_path):
    """Text alone does not represent a question whose content is a
    diagram, so those must reach a human rather than a score."""
    # read_rows streams, so a real import never holds the file in memory.
    # These tests want random access, hence the list().
    rows = list(read_rows(write_csv(tmp_path / "export.csv")))
    assert to_question(rows[2], 3)["has_figure"] is True
    assert to_question(rows[0], 1)["has_figure"] is False


def test_rows_without_question_content_are_skipped(tmp_path):
    blank = dict(ROWS[0], id="104", question_content="")
    assert to_question(blank, 4) is None


# ---------- what the real export actually contains ----------

def test_literal_null_string_becomes_none():
    """EdSider's export writes SQL NULL as the four characters NULL.
    Left as a string, two questions with an unset grade would compare
    "NULL" == "NULL" and the metadata guard would call that a match."""
    row = dict(ROWS[0], grade_name="NULL", subject_name="NULL")
    q = to_question(row, 1)
    assert q["grade"] is None
    assert q["subject"] is None


def test_oversized_draftjs_cell_can_be_read(tmp_path):
    """The real bank holds a 6.5 MB Draft.js cell - a base64 image. The
    default csv field limit is 128 KB and raises on the first one."""
    big = draftjs("Identify the structure shown." + " " * 200_000)
    path = tmp_path / "big.csv"
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(ROWS[0].keys()))
        w.writeheader()
        w.writerow(dict(ROWS[0], question_content=big))

    rows = list(read_rows(path))
    assert "Identify the structure shown." in to_question(rows[0], 1)["text"]


def test_read_rows_streams_rather_than_materialising(tmp_path):
    """A 684 MB export must not be loaded into memory as dicts."""
    import types
    assert isinstance(read_rows(write_csv(tmp_path / "export.csv")),
                      types.GeneratorType)


# ---------- row shape: what can and cannot be compared ----------

def test_parent_rows_are_identified_by_their_children():
    """id=842 holds 'using below food chain: Grass -> Frog' and points at
    its parts. It is an instruction plus a stimulus, not a question."""
    assert row_shape({"sub_questions": "837,838,839", "is_subquestion": ""}) \
        == "parent"


def test_sub_questions_are_identified():
    """'13.How many links of the above food chain' means nothing without
    the parent that holds the food chain."""
    assert row_shape({"sub_questions": "NULL", "is_subquestion": "true"}) \
        == "sub-question"


def test_plain_rows_are_standalone():
    assert row_shape({"sub_questions": "NULL", "is_subquestion": "NULL"}) \
        == "standalone"


def test_shape_defaults_to_standalone_when_columns_absent():
    """The sample set has no such columns, and must keep working."""
    assert row_shape({"id": "1", "question_content": "x"}) == "standalone"


def test_shape_reaches_the_saved_question():
    row = dict(ROWS[0], sub_questions="1,2,3")
    assert to_question(row, 1)["shape"] == "parent"
    assert to_question(ROWS[0], 1)["shape"] == "standalone"
