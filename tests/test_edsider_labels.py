"""The labels EdSider needs on a paper (src/edsider/labels.py). Every file
name and text shape here is from a real stored upload."""

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import store
from src.edsider import labels
import service
from service import DocumentUpdate, PastedPaperIn, create_document_from_text, update_document

LISTS = [
    {"kind": "language", "id": 1, "name": "English", "short_code": "EM", "extra": None},
    {"kind": "language", "id": 2, "name": "සිංහල", "short_code": "SM", "extra": None},
    {"kind": "language", "id": 3, "name": "தமிழ்", "short_code": "TM", "extra": None},
    {"kind": "term", "id": 1, "name": "First Term Test", "short_code": "T1", "extra": None},
    {"kind": "term", "id": 2, "name": "Second Term Test", "short_code": "T2", "extra": None},
    {"kind": "term", "id": 3, "name": "Third Term Test", "short_code": "T3", "extra": None},
    {"kind": "year", "id": 6, "name": "2025", "short_code": None, "extra": None},
    {"kind": "year", "id": 7, "name": "2026", "short_code": None, "extra": None},
    {"kind": "exam_category", "id": 1, "name": "Model - Term", "short_code": "MT", "extra": None},
]


@pytest.mark.parametrize("filename, term", [
    ("Grade 12_Physics_1st term_part 1 (1).pdf", 1),
    ("GIT_Gr12_FirstTT (1).pdf", 1),
    ("General Information TechnologyTerm02.docx", 2),
    ("grade 13 2nd term paper.pdf", 2),
    ("General English_Term3_Gr12.docx", 3),
    ("12 ශ්_රේණිය 1 වන වාරය (ප්_රශ්න පත්_රය).pdf", 1),
    ("midterm notes 1.pdf", None),
    ("GIT_AL_Model Paper_recorrected.pdf", None),
])
def test_the_term_is_read_from_the_way_real_papers_write_it(filename, term):
    assert labels.detect_term("", filename) == term


def test_the_medium_is_the_script_the_paper_is_written_in():
    assert labels.detect_medium("පහත ප්‍රශ්නයට නිවැරදි පිළිතුර තෝරන්න.") == "SM"
    # The Tamil GIT paper is 14% Latin letters (code, file types) - still Tamil.
    assert labels.detect_medium("கலத்தில் பயன்படுத்தக்க வினா jpg படம்") == "TM"
    assert labels.detect_medium("Which of the following devices is an output device?") == "EM"
    # A scan with no text at all: EdSider's own medium code in the file name.
    assert labels.detect_medium("", "2025 Chemistry Part I SM.pdf") == "SM"
    assert labels.detect_medium("", "notes.pdf") is None


def test_a_year_inside_a_question_is_never_the_papers_year():
    # Communication & Media: an MCQ option, far into the paper.
    text = "Grade 12 - Communication and Media\n" + "x " * 400 + "\nA. 1998\nB. 2000\nE. 2012\n"
    assert labels.detect_year(text, "Grade 12 - Communication & Media Studies - Term 01.docx") is None
    assert labels.detect_year("", "2025 Chemistry Part I SM.pdf") == "2025"


def _seed():
    with store.connect() as conn:
        store.sync_edsider_lookups(conn, LISTS)
    service._edsider_lists(refresh=True)


def test_what_the_paper_states_is_offered_and_saved_only_when_asked(local_db):
    _seed()
    doc = create_document_from_text(
        PastedPaperIn(text="Second Term Test 2026\n1. Which device is an output device?\n(1) A\n(2) B\n",
                      title="GIT Gr12"), identity="x")
    assert doc.suggested_edsider_labels == {"medium_id": 1, "term_id": 2, "year_id": 7}
    assert doc.edsider_labels == {}           # nothing saved yet
    saved = update_document(doc.id, DocumentUpdate(edsider_labels={
        "medium_id": 1, "term_id": 2, "year_id": 7, "exam_category_id": 1, "unknown_key": 5}))
    assert saved.edsider_labels["exam_category_id"] == 1
    assert "unknown_key" not in saved.edsider_labels
    assert saved.edsider_labels["author_id"] is None


def test_an_id_not_on_edsiders_list_is_refused(local_db):
    _seed()
    doc = create_document_from_text(PastedPaperIn(text="1. A question here.\n2. Another one.\n"),
                                    identity="x")
    with pytest.raises(HTTPException) as err:
        update_document(doc.id, DocumentUpdate(edsider_labels={"term_id": 99}))
    assert err.value.status_code == 400
    assert "term" in err.value.detail
