"""Paper intake - extraction heuristics, the batch duplicate check, and
the intake storage.

No real model, no real PDF: the pure text rules (segment,
detect_paper_meta) are exercised directly, the batch check runs against
a fake embedder the same way test_check_new.py does, and read_docx runs
against a document python-docx builds in tmp_path.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import store
from src.intake import extract, review


# --------------------------------------------------------- segment()

def test_segment_splits_on_numbered_questions():
    text = (
        "1. What is the capital of Sri Lanka? (2 marks)\n"
        "2. Name the largest ocean on Earth. (3 marks)\n"
        "3. Explain the water cycle. (5 marks)\n"
    )
    drafts = extract.segment(text)
    assert [d["question_number"] for d in drafts] == ["1", "2", "3"]
    assert [d["marks"] for d in drafts] == [2, 3, 5]
    assert drafts[0]["seq"] == 1
    assert "capital of Sri Lanka" in drafts[0]["text"]


def test_segment_handles_q_prefix_and_parens():
    for text in (
        "Q1 Define photosynthesis. [4]\nQ2 State Newton's first law. [3]\n",
        "(1) Define photosynthesis. [4]\n(2) State Newton's first law. [3]\n",
    ):
        drafts = extract.segment(text)
        assert [d["question_number"] for d in drafts] == ["1", "2"]
        assert [d["marks"] for d in drafts] == [4, 3]


def test_mcq_options_on_their_own_lines_stay_inside_the_question():
    """The real Grade 12 GIT paper: questions "1." with options "(1)" to
    "(4)" one per line. Any left-margin number counted as a question, so
    options (2), (3) and (4) of question 1 became questions 2, 3 and 4,
    and the real questions 2-4 were dropped as going backwards."""
    text = (
        "1. Which of the following BEST describes a system?\n"
        "(1) A device\n(2) An organized set\n(3) A group\n(4) Any machine\n"
        "2. Which correctly represents IPO?\n"
        "(1) Output\n(2) Input\n(3) Process\n(4) Store\n"
    )
    drafts = extract.segment(text)
    assert [d["question_number"] for d in drafts] == ["1", "2"]
    assert "(4) Any machine" in drafts[0]["text"]


def test_segment_ignores_a_number_that_jumps_backwards():
    """A '1990.' at a line start inside prose is not question 1990 - a
    real paper only counts up."""
    text = (
        "1. In which year did the event happen?\n"
        "1990. was a significant year for many reasons that we will not\n"
        "2. What caused it?\n"
    )
    drafts = extract.segment(text)
    assert [d["question_number"] for d in drafts] == ["1", "2"]
    assert "1990. was a significant year" in drafts[0]["text"]


def test_missing_marks_is_not_low_confidence():
    """A real MCQ paper prints no per-question marks at all. Flagging
    that put a low-confidence warning on all 50 questions of a real
    Grade 13 paper and told the reviewer nothing."""
    drafts = extract.segment("1. A question with no marks annotation at all.\n")
    assert drafts[0]["marks"] is None
    assert drafts[0]["extraction_confidence"] == "ok"


def test_almost_no_text_is_low_confidence():
    """Short text is the signal that the extractor actually struggled."""
    drafts = extract.segment("1. ?\n2. A properly worded question here.\n")
    assert drafts[0]["extraction_confidence"] == "low"
    assert drafts[1]["extraction_confidence"] == "ok"


def test_segment_handles_number_formats_pdf_text_actually_produces():
    """The strict first version only split '1. ' with a trailing space.
    Real extracted text uses all of these."""
    cases = [
        "1.What is X?\n2.What is Y?",                 # no space after the dot
        "Question 1: Define X.\nQuestion 2: Define Y.",  # word + colon
        "Q.1 Define X.\nQ.2 Define Y.",                # Q. prefix
        "01. Define X.\n02. Define Y.",                # leading zero
        "1.\nDefine X on the next line.\n2.\nDefine Y.",  # number alone on its line
    ]
    for text in cases:
        drafts = extract.segment(text)
        assert len(drafts) == 2, f"did not split: {text!r}"
        assert [d["question_number"] for d in drafts] == ["1", "2"] or \
               [d["question_number"] for d in drafts] == ["01", "02"]


def test_segment_does_not_split_on_mcq_option_numbers():
    """(1)(2)(3)(4) listed as answer options after a question must not
    each become a new question."""
    text = (
        "5) What is the capital of Sri Lanka?\n"
        "(1) Kandy (2) Colombo (3) Galle (4) Jaffna\n"
        "6) Name the largest ocean.\n"
    )
    drafts = extract.segment(text)
    assert [d["question_number"] for d in drafts] == ["5", "6"]


def test_segment_does_not_read_a_mark_total_as_a_question():
    """'(20)' right before the word marks is a total, not question 20."""
    text = "1. Answer fully. (20) marks\n2. Next question. (10 marks)\n"
    drafts = extract.segment(text)
    assert [d["question_number"] for d in drafts] == ["1", "2"]


def test_segment_falls_back_to_one_draft_when_no_numbering():
    text = "This is just a blob of text with no question numbers anywhere."
    drafts = extract.segment(text)
    assert len(drafts) == 1
    assert drafts[0]["question_number"] is None
    assert drafts[0]["extraction_confidence"] == "low"
    assert drafts[0]["text"] == text


def test_segment_takes_the_last_marks_annotation():
    """A multi-part question lists part marks then a total - the total
    is the useful one."""
    text = "1. (a) Define X. (2 marks) (b) Explain Y. (3 marks) Total (5 marks)\n"
    drafts = extract.segment(text)
    assert drafts[0]["marks"] == 5


def test_segment_strips_the_printed_question_number_from_the_text():
    """The number is metadata (question_number), not part of the
    question - leaving it in would break exact-match de-duplication and
    misfire the numbers guard."""
    drafts = extract.segment(
        "3. Define osmosis. (2 marks)\n7. Define osmosis. (2 marks)\n"
    )
    assert drafts[0]["text"].startswith("Define osmosis")
    assert drafts[1]["text"].startswith("Define osmosis")
    # Same question under two different printed numbers -> identical text.
    assert drafts[0]["text"] == drafts[1]["text"]
    assert [d["question_number"] for d in drafts] == ["3", "7"]


def test_real_mcq_paper_shape_splits_on_every_question():
    """The shape of a real Grade 13 Biosystem Technology paper: '01.' with
    no space, options on the lines below. The first extractor required a
    space after the dot and returned the whole 18,000-character paper as
    one question."""
    text = (
        "BIOSYSTEM TECHNOLOGY ( I , II )\nThird term examination\nGrade 13\n"
        "\tPart I\n"
        "01.Interpretes weather conditions suitable for biological systems,\n"
        "Ignoring climate\nUsing forecasts\nBoth of these\n"
        "02.Water quality determination measures,\n"
        "Only temperature\npH and nutrients\nNeither\n"
        "03.Plantation crop production techniques emphasize,\n"
        "Ignoring quality standards\nMeeting them\n"
    )
    drafts = extract.segment(text)
    assert [d["question_number"] for d in drafts] == ["01", "02", "03"]
    assert drafts[0]["text"].startswith("Interpretes weather")
    # The options belong to their question, not to a new one.
    assert "Ignoring climate" in drafts[0]["text"]


def test_worked_solution_numbering_is_not_split_by_the_question_word_style():
    """A marking-scheme document numbers its SOLUTION STEPS, which look
    exactly like question numbers. Measured on a real Grade 13 Combined
    Maths upload: number-splitting found only solution steps. Splitting
    on the word 'Question' finds the real questions instead."""
    text = (
        "Question\nA particle is projected at 40 m/s.\n"
        "1. Initial Velocity Components\nvx = 40 cos a\n"
        "2. Finding the Vertical Velocity\nvy = u sin a - gt\n"
        "3. Magnitude of Resultant Velocity\nV = sqrt(vx^2 + vy^2)\n"
        "Question\nA string passes over a smooth pulley.\n"
        "1. Resolving Forces\n2mg - T = 2ma\n"
        "2. Taking Moments\nT = m(g + a)\n"
    )
    by_number = extract.segment(text, style="number")
    by_word = extract.segment(text, style="question_word")

    # Number-splitting is fooled - it finds the solution steps.
    assert len(by_number) > 2
    # The word style finds the two real questions.
    assert len(by_word) == 2
    assert by_word[0]["text"].startswith("A particle is projected")
    assert by_word[1]["text"].startswith("A string passes over")


def test_split_styles_are_declared_for_the_ui():
    """The page builds its radio buttons from these, so an unlabelled or
    missing style would render an empty choice."""
    assert extract.DEFAULT_SPLIT_STYLE in extract.SPLIT_STYLES
    assert "question_word" in extract.SPLIT_STYLES
    assert all(isinstance(v, str) and v for v in extract.SPLIT_STYLES.values())


# ------------------------------------------------------- structure()

# The shape of the real Grade 12 GIT first-term paper, cut down: a header,
# Part I (MCQs "1." with options "(1)"-"(4)"), Part II (structured "(01)"
# with parts "(1)", "(a)"), then a marking scheme for both.
_TWO_PART_PAPER = (
    "General Information Technology (GIT)\n"
    "Grade 12 — First Term Examination\n"
    "Time: Three Hours\n"
    "General Information Technology I\n"
    "Instructions:\n"
    "* Answer all the questions.\n"
    "* In each of the questions 1 to 40, pick one of the alternatives (1), (2), (3), (4) which is correct or most\n"
    "appropriate and mark your response on the answer sheet.\n"
    "1. Which of the following BEST describes a \"system\"?\n"
    "(1) A device that only performs a single function\n"
    "(2) An organized set of interacting components\n"
    "(3) A group of unrelated hardware parts\n"
    "(4) Any machine that uses electricity\n"
    "2. Which of the following devices can function as BOTH an input and an output device?\n"
    "(1) Keyboard\n(2) Touchscreen\n(3) Scanner\n(4) Plotter\n"
    "General Information Technology II\n"
    "* Answer FOUR questions only.\n"
    "* Each question carries 15 marks.\n"
    "(01) (1) A school library uses a computerized system to manage its book lending.\n"
    "(a) Define the term \"system\" as used in computing. [2]\n"
    "(b) Draw a fully labeled block diagram of a computer system. [5]\n"
    "(2) Consider the following scenario:\n"
    "(a) Identify ONE example of data and ONE example of information. [2]\n"
    "(02) (1) Nimal is studying number systems in his GIT class.\n"
    "(a) Convert the decimal number 53 into binary.\n"
    "[3]\n"
    "MARKING SCHEME\n"
    "Grade 12 — General Information Technology — First Term Examination\n"
    "Part I — Multiple Choice Question Answers\n"
    "Q1: (2) Q2: (2)\n"
    "Answer Justifications for Selected MCQ:\n"
    "Q2: Touchscreen both displays and accepts input → Answer (2)\n"
    "Part II — Marking Scheme (Each question: 15 marks)\n"
    "Question (01) [15 marks]\n"
    "(1)(a) Definition of a System [2 marks]\n"
    "A system is an organized collection of interacting components.\n"
    "Question (02) [15 marks]\n"
    "(1)(a) Convert Decimal 53 to Binary [3 marks]\n"
    "53₁₀ = 110101₂\n"
)


def _part(parsed, label):
    return [q for q in parsed["questions"] if q["section"] == label]


def test_a_two_part_paper_comes_back_as_its_two_parts():
    """Read as one list, the real GIT paper came back as 54 "questions"
    for 45 real ones - Part II's parts split apart and the marking scheme
    split into questions of its own."""
    parsed = extract.structure(_TWO_PART_PAPER)
    assert [(s["label"], s["kind"], s["questions"])
            for s in parsed["sections"]] == [
        ("Part I", "mcq", 2), ("Part II", "structured", 2)]

    part1, part2 = _part(parsed, "Part I"), _part(parsed, "Part II")
    assert [q["question_number"] for q in part1] == ["1", "2"]
    assert "(4) Any machine" in part1[0]["text"]
    assert [q["question_number"] for q in part2] == ["01", "02"]
    # A question's numbered parts stay inside it.
    assert "(2) Consider the following scenario" in part2[0]["text"]
    # "Each question carries 15 marks", not the last part's "[2]".
    assert [q["marks"] for q in part2] == [15, 15]
    assert [q["seq"] for q in parsed["questions"]] == [1, 2, 3, 4]


def test_the_marking_scheme_becomes_answers_not_questions():
    parsed = extract.structure(_TWO_PART_PAPER)
    everything = " ".join(q["text"] for q in parsed["questions"])
    assert "MARKING SCHEME" not in everything
    assert "Q1: (2)" not in everything

    part1, part2 = _part(parsed, "Part I"), _part(parsed, "Part II")
    assert [q["answer"] for q in part1] == ["(2)", "(2)"]
    assert part2[0]["answer"].startswith("(1)(a) Definition of a System")
    assert "110101" in part2[1]["answer"]


def test_a_parts_heading_and_instructions_stay_with_it():
    part2 = extract.structure(_TWO_PART_PAPER)["sections"][1]
    assert part2["heading"] == "General Information Technology II"
    assert part2["marks_each"] == 15
    assert "Answer FOUR questions only" in part2["instructions"]


def test_questions_before_a_part_2_heading_are_part_1():
    """The real Combined Maths paper has no heading over its first part,
    only "Part 2" before the second."""
    text = (
        "Combined mathematics- 2\nGrade -13\n1st term exam\n"
        "Answer the all questions\n"
        "1. Find x. [25 Marks]\n2. Find y. [25 Marks]\n"
        "Part 2\nAnswer any 5 questions\n1. Prove it. [25 Marks]\n"
    )
    parsed = extract.structure(text)
    assert [s["label"] for s in parsed["sections"]] == ["Part 1", "Part 2"]
    assert [q["section"] for q in parsed["questions"]] == [
        "Part 1", "Part 1", "Part 2"]


def test_a_paper_in_one_piece_has_no_part_labels():
    parsed = extract.structure("1. Define X.\n2. Define Y.\n")
    assert [s["label"] for s in parsed["sections"]] == [None]
    assert all(q["section"] is None for q in parsed["questions"])


def test_a_part_title_repeated_on_every_page_is_still_one_part():
    parsed = extract.structure(
        "Part I\n1. First question here.\nPart I\n2. Second question here.\n")
    assert [s["label"] for s in parsed["sections"]] == ["Part I"]
    assert [q["question_number"] for q in parsed["questions"]] == ["1", "2"]


def test_lettered_sub_parts_are_not_mistaken_for_mcq_options():
    """The real Grade 13 Biosystem Technology paper's Part II-B: each
    question is "a) Explain ... b) Describe ...". Counted as options, the
    whole part was labelled multiple choice."""
    text = (
        "Part II – B\nAnswer any four questions.\n"
        "1. Irrigation\na) Explain the limitations of drip irrigation.\n"
        "b) Describe the method of sprinkler irrigation.\n"
        "c) State two advantages of furrow irrigation.\n"
        "2. Fish processing\na) Explain the steps in preparing jar fish.\n"
        "b) Compare salting and drying.\nc) Name two preservatives.\n"
    )
    assert extract.structure(text)["sections"][0]["kind"] != "mcq"


def test_a_single_stray_number_does_not_beat_a_real_numbering():
    """Worked solutions numbered like the questions they sit in restart
    over and over; that must not hand the paper to a format that numbers
    one thing once."""
    text = (
        "1. Find the velocity.\n1. Resolve.\n2. Solve.\n3. Conclude.\n"
        "2. Find the force.\n1. Resolve.\n2. Solve.\n"
        "3. Find the time.\n(4) An equation label on its own line\n"
    )
    numbers = [q["question_number"] for q in extract.segment(text)]
    assert len(numbers) >= 2
    assert "4" not in numbers


def test_heading_recognition():
    h = extract._heading
    assert h("General Information Technology II")["label"] == "Part II"
    assert h("\tPart I")["label"] == "Part I"
    assert h("Part II – B")["label"] == "Part II-B"
    assert h("Section A (Answer all questions)")["label"] == "Section A"
    assert h("MARKING SCHEME")["kind"] == "answers"
    # Lines from real papers that must not start a part.
    assert h("Part 1 of the experiment uses a convex lens.") is None
    assert h("Paper – 1 | Part 2 Questions with Answers") is None
    assert h("Answer") is None
    assert h("Answer the all questions") is None
    assert h("Explain the causes of World War II") is None


# ------------------------------------------------- detect_paper_meta()

def test_detect_paper_meta_reads_grade_subject_and_title():
    text = "Provincial Department of Education\nGrade 10 - Science\nFirst Term Test\n"
    meta = extract.detect_paper_meta(text)
    assert meta == {"grade": "G10", "subject": "Science",
                    "title": "First Term Test"}


def test_detect_paper_meta_prefers_the_longer_subject_name():
    text = "Grade 11\nBusiness Studies\nSecond Term\n"
    meta = extract.detect_paper_meta(text)
    assert meta["subject"] == "Business Studies"


def test_detect_paper_meta_returns_none_when_absent():
    meta = extract.detect_paper_meta("Just some text with no header at all.")
    assert meta == {"grade": None, "subject": None, "title": None}


def test_paper_meta_uses_the_banks_own_grade_and_subject_names():
    """The bank stores grades as G06-G13 and subjects under its own
    names. A detected "13" or "Combined Mathematics" matched nothing in
    the review page's lists, so grade and subject showed up blank."""
    maths = extract.detect_paper_meta(
        "Combined mathematics- 2\nGrade -13\n1st term exam\n")
    assert maths == {"grade": "G13", "subject": "Combined Maths",
                     "title": "1st Term Exam"}
    git = extract.detect_paper_meta(
        "General Information Technology (GIT)\n"
        "Grade 12 — First Term Examination\n")
    assert git == {"grade": "G12", "subject": "General Information Technology",
                   "title": "First Term Examination"}


def test_paper_meta_falls_back_to_the_filename():
    meta = extract.detect_paper_meta(
        "1. A question with no header.\n", filename="GIT_Gr12_FirstTT (1).pdf")
    assert meta == {"grade": "G12", "subject": "General Information Technology",
                    "title": "First Term Test"}
    assert extract.detect_paper_meta(
        "", filename="G-13, C.maths, Applied , 1st term exam.pdf"
    ) == {"grade": "G13", "subject": "Combined Maths", "title": "1st Term Exam"}


def test_medium_of_instruction_is_not_the_subject():
    meta = extract.detect_paper_meta("Grade 9\nEnglish Medium\nScience\n")
    assert meta["subject"] == "Science"


def test_grade_label_reads_the_way_a_person_writes_it():
    assert extract.grade_label("G12") == "Grade 12"
    assert extract.grade_label("G06") == "Grade 6"
    assert extract.grade_label(None) == ""


def test_detect_paper_meta_rejects_an_impossible_grade():
    meta = extract.detect_paper_meta("Grade 47 monster paper")
    assert meta["grade"] is None


# --------------------------------------------------- extract() on docx

def test_extract_reads_a_docx_end_to_end(tmp_path):
    docx = pytest.importorskip("docx")
    path = tmp_path / "paper.docx"
    d = docx.Document()
    d.add_paragraph("Grade 9 - Mathematics")
    d.add_paragraph("1. Add 12 and 5. (2 marks)")
    d.add_paragraph("2. Subtract 5 from 12. (2 marks)")
    d.save(str(path))

    result = extract.extract(path)
    assert result["file_type"] == "docx"
    assert result["extractor"] == "docx-text"
    assert result["paper"] == {"grade": "G09", "subject": "Maths",
                               "title": None}
    assert [q["question_number"] for q in result["questions"]] == ["1", "2"]
    # Paper-level meta is inherited by each draft.
    assert all(q["grade"] == "G09" and q["subject"] == "Maths"
               for q in result["questions"])


def test_extract_unsupported_type_does_not_raise(tmp_path):
    path = tmp_path / "note.txt"
    path.write_text("1. Something. (1 mark)")
    result = extract.extract(path)
    assert result["file_type"] == "other"
    assert result["extractor"].startswith("unsupported")


def test_extract_legacy_doc_asks_for_docx(tmp_path):
    path = tmp_path / "old.doc"
    path.write_bytes(b"\xd0\xcf\x11\xe0 legacy binary word file")
    result = extract.extract(path)
    assert "save as .docx" in result["extractor"]
    assert extract.was_incomplete(result["extractor"])


def test_extract_corrupt_pdf_does_not_raise(tmp_path):
    pytest.importorskip("pdfplumber")
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"this is not a pdf")
    result = extract.extract(path, ocr=FakeOCR(""))
    assert result["extractor"].startswith("failed")
    assert result["questions"][0]["extraction_confidence"] == "low"


# ------------------------------------------- pictures, scans, equations

class FakeOCR:
    """image -> fixed text, recording what it was handed. Tesseract is
    not needed for these tests - they prove which parts of a file get
    sent to OCR and where the result lands, not how well OCR reads."""

    def __init__(self, text):
        self.text = text
        self.calls = []

    def __call__(self, image):
        self.calls.append(image)
        return self.text


def unavailable_ocr(image):
    raise extract.ExtractionUnavailable("Tesseract is not installed")


def _write_pdf(path, lines, picture=None, pictures=()):
    """A one-page PDF built by hand: Helvetica text at the given heights
    (PDF y, bottom-up) and grey inline pictures at (x, y, width, height)
    - enough to drive each of read_pdf's routes without a PDF library or
    a real paper."""
    ops = [f"BT /F1 12 Tf 50 {y} Td ({s}) Tj ET" for y, s in lines]
    for x, y, w, h in ([picture] if picture else []) + list(pictures):
        ops.append(f"q {w} 0 0 {h} {x} {y} cm BI /W 1 /H 1 /CS /G /BPC 8 ID \x80 EI Q")
    stream = "\n".join(ops).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1, xref)
    path.write_bytes(bytes(out))


def test_segment_restarts_numbering_after_a_part_heading():
    text = (
        "Part 1\n1. Define velocity.\n2. Define acceleration.\n"
        "Part 2\nAnswer any 5 questions\n1. Prove the result.\n2. Find the force.\n"
    )
    drafts = extract.segment(text)
    assert [d["question_number"] for d in drafts] == ["1", "2", "1", "2"]
    assert drafts[2]["text"] == "Prove the result."


def test_segment_still_drops_backwards_numbers_without_a_heading():
    text = "1. Choose one.\n(1) red\n(2) blue\n2. Choose again.\n"
    assert [d["question_number"] for d in extract.segment(text)] == ["1", "2"]


def test_page_route_sends_sparse_or_garbled_pages_to_ocr():
    # Only a page number as text - the real model papers' picture pages.
    assert extract._pdf_page_route("9", []) == "page"
    assert extract._pdf_page_route("", [(0, 0, 400, 500)]) == "page"
    assert extract._pdf_page_route("A long enough line (cid:9) here", []) == "page"
    assert extract._pdf_page_route("1. Name the capital of Sri Lanka.", []) == "text"
    assert extract._pdf_page_route(
        "1. Name the capital of Sri Lanka.", [(0, 0, 200, 100)]) == "pictures"


def test_fix_ligatures_restores_the_ti_glyph():
    assert (extract._fix_ligatures("Combined mathema(cid:415)cs ques(cid:415)ons")
            == "Combined mathematics questions")


def test_scanned_pdf_is_rendered_and_ocrd(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    pytest.importorskip("pdfplumber")
    path = tmp_path / "scan.pdf"
    Image.new("L", (400, 500), 255).save(path, "PDF")

    ocr = FakeOCR("Grade 9 Science\n1. Name a planet.\n2. Name a star.")
    result = extract.extract(path, ocr=ocr)

    assert [q["question_number"] for q in result["questions"]] == ["1", "2"]
    assert "ocr (1 of 1 pages)" in result["extractor"]
    # A rendered page image - the old code handed the .pdf file itself
    # to Pillow, which cannot open a PDF.
    assert len(ocr.calls) == 1 and isinstance(ocr.calls[0], Image.Image)
    assert not extract.was_incomplete(result["extractor"])


def test_scanned_pdf_without_tesseract_says_so(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    pytest.importorskip("pdfplumber")
    path = tmp_path / "scan.pdf"
    Image.new("L", (400, 500), 255).save(path, "PDF")

    result = extract.extract(path, ocr=unavailable_ocr)
    assert "1 of 1 pages not read" in result["extractor"]
    assert extract.was_incomplete(result["extractor"])
    assert result["questions"][0]["extraction_confidence"] == "low"


def test_scanned_page_ocr_finds_nothing_is_flagged(tmp_path):
    # Handwriting: Tesseract runs fine and returns nothing. That must not
    # pass for an empty paper.
    Image = pytest.importorskip("PIL.Image")
    pytest.importorskip("pdfplumber")
    path = tmp_path / "handwritten.pdf"
    Image.new("L", (400, 500), 255).save(path, "PDF")

    result = extract.extract(path, ocr=FakeOCR("  "))
    assert "1 of 1 pages not read: OCR found no text" in result["extractor"]
    assert extract.was_incomplete(result["extractor"])


def test_text_pdf_never_touches_ocr(tmp_path):
    pytest.importorskip("pdfplumber")
    path = tmp_path / "typed.pdf"
    _write_pdf(path, [(780, "Grade 9 Science"),
                      (740, "1. Name a planet. (2 marks)"),
                      (700, "2. Name a star. (2 marks)")])
    ocr = FakeOCR("SHOULD NOT APPEAR")
    result = extract.extract(path, ocr=ocr)

    assert ocr.calls == []
    assert result["extractor"] == "pdf-text"
    assert [q["question_number"] for q in result["questions"]] == ["1", "2"]


def test_picture_in_a_typed_pdf_is_read_where_it_sits(tmp_path):
    pytest.importorskip("pdfplumber")
    path = tmp_path / "mixed.pdf"
    # Question 1's text, then a picture below it, then question 2.
    _write_pdf(path, [(780, "Grade 9 Science"),
                      (740, "1. Look at the picture below."),
                      (400, "2. Name a star.")],
               picture=(50, 500, 300, 150))
    ocr = FakeOCR("Which planet is shown here?")
    result = extract.extract(path, ocr=ocr)

    raw = result["raw_text"]
    assert (raw.index("Look at the picture") < raw.index("Which planet")
            < raw.index("Name a star"))
    assert "Which planet is shown here?" in result["questions"][0]["text"]
    assert len(ocr.calls) == 1
    assert "ocr (1 of 1 pages)" in result["extractor"]


def test_picture_halves_are_joined_into_one_picture():
    # Coordinates from the real Grade 13 Applied paper's river question.
    top = (121.9, 100.0, 516.5, 211.3)
    bottom = (121.9, 211.3, 516.5, 322.6)
    separate = (121.9, 400.0, 516.5, 500.0)
    assert extract._merge_touching([bottom, separate, top]) == [
        (121.9, 100.0, 516.5, 322.6), separate]


def test_picture_split_by_word_is_ocrd_once_in_a_typed_pdf(tmp_path):
    pytest.importorskip("pdfplumber")
    path = tmp_path / "halves.pdf"
    _write_pdf(path, [(780, "Grade 13 Combined Mathematics"),
                      (740, "1. Answer the question below.")],
               pictures=[(50, 575, 300, 75), (50, 500, 300, 75)])
    ocr = FakeOCR("A river of width a flows with speed u.")
    result = extract.extract(path, ocr=ocr)
    assert len(ocr.calls) == 1
    assert result["raw_text"].count("A river of width") == 1


_MATH_NS = 'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"'


def _r(s):
    return f"<m:r><m:t>{s}</m:t></m:r>"


def test_omml_fraction_and_root():
    from docx.oxml import parse_xml
    xml = (f"<m:oMath {_MATH_NS}><m:f><m:num>{_r('1')}</m:num><m:den>"
           f"<m:rad><m:radPr><m:degHide m:val=\"1\"/></m:radPr><m:deg/>"
           f"<m:e>{_r('2')}</m:e></m:rad></m:den></m:f></m:oMath>")
    # A bare root counts as one term, the way the real paper reads it:
    # "80 x 1/√2 = 40√2".
    assert extract._omml(parse_xml(xml)) == "1/√2"


def test_omml_subscripts_and_superscripts_under_a_root():
    from docx.oxml import parse_xml

    def v_squared(sub):
        return (f"<m:sSup><m:e><m:sSub><m:e>{_r('v')}</m:e>"
                f"<m:sub>{_r(sub)}</m:sub></m:sSub></m:e>"
                f"<m:sup>{_r('2')}</m:sup></m:sSup>")

    xml = (f"<m:oMath {_MATH_NS}><m:rad><m:deg/><m:e>"
           f"{v_squared('x')}{_r('+')}{v_squared('y')}"
           f"</m:e></m:rad></m:oMath>")
    assert extract._omml(parse_xml(xml)) == "√(v_x^2+v_y^2)"


def test_docx_keeps_equations_inside_the_question(tmp_path):
    docx = pytest.importorskip("docx")
    from docx.oxml import parse_xml
    d = docx.Document()
    d.add_paragraph("Grade 12 - Combined Mathematics")
    p = d.add_paragraph("1. Show that 80 x ")
    p._p.append(parse_xml(
        f"<m:oMath {_MATH_NS}><m:f><m:num>{_r('1')}</m:num>"
        f"<m:den>{_r('2')}</m:den></m:f></m:oMath>"))
    p.add_run(" = 40. (5 marks)")
    path = tmp_path / "maths.docx"
    d.save(str(path))

    result = extract.extract(path)
    assert result["questions"][0]["text"] == "Show that 80 x 1/2 = 40. (5 marks)"
    assert result["questions"][0]["marks"] == 5


def test_docx_reads_tables_and_pictures_in_order(tmp_path):
    docx = pytest.importorskip("docx")
    Image = pytest.importorskip("PIL.Image")
    picture = tmp_path / "q.png"
    Image.new("RGB", (200, 60), "white").save(picture)

    d = docx.Document()
    d.add_paragraph("1. Which is the capital?")
    table = d.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "A. Colombo"
    table.cell(0, 1).text = "B. Kandy"
    d.add_paragraph("2. Answer the question in the picture.")
    d.add_picture(str(picture))
    path = tmp_path / "paper.docx"
    d.save(str(path))

    ocr = FakeOCR("What is the boiling point of water?")
    result = extract.extract(path, ocr=ocr)
    q1, q2 = result["questions"]
    assert "A. Colombo | B. Kandy" in q1["text"]
    assert "boiling point of water" in q2["text"]
    assert result["extractor"] == "docx-text + ocr (1 image)"


def test_docx_picture_without_tesseract_is_marked_not_dropped(tmp_path):
    docx = pytest.importorskip("docx")
    Image = pytest.importorskip("PIL.Image")
    picture = tmp_path / "q.png"
    Image.new("RGB", (200, 60), "white").save(picture)

    d = docx.Document()
    d.add_paragraph("1. Answer the question in the picture.")
    d.add_picture(str(picture))
    path = tmp_path / "paper.docx"
    d.save(str(path))

    result = extract.extract(path, ocr=unavailable_ocr)
    assert "[image - not read]" in result["questions"][0]["text"]
    assert extract.was_incomplete(result["extractor"])


def test_prepare_for_ocr_flattens_transparency_onto_white():
    Image = pytest.importorskip("PIL.Image")
    # A transparent PNG screenshot: naive greyscale turns it black.
    out = extract._prepare_for_ocr(Image.new("RGBA", (100, 40), (0, 0, 0, 0)))
    assert out.mode == "L"
    assert out.getpixel((0, 0)) == 255
    assert out.width == 400   # upscaled, but capped at 4x


# --------------------------------------------------- check_batch()

class FakeEmbedder:
    """text -> vector, exactly as given. Lets a test decide which drafts
    the model should think are alike."""

    def __init__(self, vectors):
        self.vectors = vectors

    def encode(self, texts, is_query=False):
        return [self.vectors[t] for t in texts]


def test_check_batch_flags_a_near_duplicate_pair():
    drafts = [
        {"id": 1, "text": "Explain how plants make food."},
        {"id": 2, "text": "Describe the way plants produce their own food."},
        {"id": 3, "text": "State Newton's second law of motion."},
    ]
    vectors = {
        drafts[0]["text"]: [1.0, 0.0, 0.0],
        drafts[1]["text"]: [1.0, 0.0, 0.0],   # same meaning per the model
        drafts[2]["text"]: [0.0, 1.0, 0.0],
    }
    out = review.check_batch(drafts, FakeEmbedder(vectors))
    assert out["exact_groups"] == []
    assert len(out["similar_pairs"]) == 1
    pair = out["similar_pairs"][0]
    assert {pair["a"], pair["b"]} == {1, 2}


def test_check_batch_reports_identical_text_as_an_exact_group_only():
    drafts = [
        {"id": 1, "text": "What is 2 + 2?"},
        {"id": 2, "text": "What is 2 + 2?"},
        {"id": 3, "text": "What is the capital of France?"},
    ]
    vectors = {
        "What is 2 + 2?": [1.0, 0.0],
        "What is the capital of France?": [0.0, 1.0],
    }
    out = review.check_batch(drafts, FakeEmbedder(vectors))
    assert out["exact_groups"] == [[1, 2]]
    # The exact pair must not also be listed as a "similar" pair.
    assert out["similar_pairs"] == []


def test_check_batch_catches_the_same_question_under_different_numbers():
    """The real end-to-end case: segment() strips the printed number, so
    a question copy-pasted onto a paper twice at different positions
    hashes identically and lands as an exact group - not lost to the
    numbers guard firing on '3.' vs '7.'."""
    drafts_text = extract.segment(
        "3. Define osmosis. (2 marks)\n7. Define osmosis. (2 marks)\n"
    )
    drafts = [{"id": i + 1, "text": d["text"]} for i, d in enumerate(drafts_text)]
    out = review.check_batch(
        drafts, FakeEmbedder({drafts[0]["text"]: [1.0, 0.0]}),
    )
    assert out["exact_groups"] == [[1, 2]]
    assert out["similar_pairs"] == []


def test_check_batch_needs_at_least_two_drafts():
    out = review.check_batch([{"id": 1, "text": "only one"}], FakeEmbedder({}))
    assert out == {"exact_groups": [], "similar_pairs": []}


def test_check_batch_skips_blank_drafts():
    drafts = [
        {"id": 1, "text": "a real question here"},
        {"id": 2, "text": "   "},
    ]
    out = review.check_batch(drafts, FakeEmbedder({"a real question here": [1.0]}))
    assert out == {"exact_groups": [], "similar_pairs": []}


# ---------------------------------------------------------- storage

@pytest.fixture
def local_db(tmp_path):
    original = store.DB_PATH
    store.use_database(tmp_path / "intake_test.db")
    yield
    store.use_database(original)


def test_document_and_draft_round_trip(local_db):
    with store.connect() as conn:
        doc_id = store.create_intake_document(
            conn, "grade10-science.pdf", file_type="pdf",
            extractor="pdf-text", paper_grade="10", paper_subject="Science",
        )
        store.add_extracted_questions(conn, doc_id, [
            {"seq": 1, "question_number": "1", "text": "Q one", "marks": 2,
             "extraction_confidence": "ok"},
            {"seq": 2, "question_number": "2", "text": "Q two", "marks": None,
             "extraction_confidence": "low"},
        ], fallback_grade="10", fallback_subject="Science")

        drafts = store.intake_questions(conn, doc_id)

    assert [d["seq"] for d in drafts] == [1, 2]
    assert drafts[0]["grade"] == "10" and drafts[0]["subject"] == "Science"
    assert drafts[1]["extraction_confidence"] == "low"


def test_update_intake_question_applies_edits(local_db):
    with store.connect() as conn:
        doc_id = store.create_intake_document(conn, "p.pdf")
        store.add_intake_question(conn, doc_id, 1, "raw text", marks=None)
        q_id = store.intake_questions(conn, doc_id)[0]["id"]

        store.update_intake_question(
            conn, q_id, text="corrected text", marks=5, reviewed=1,
        )
        row = store.intake_questions(conn, doc_id)[0]

    assert row["text"] == "corrected text"
    assert row["marks"] == 5
    assert row["reviewed"] == 1


def test_update_intake_question_rejects_unknown_columns(local_db):
    with store.connect() as conn:
        doc_id = store.create_intake_document(conn, "p.pdf")
        store.add_intake_question(conn, doc_id, 1, "t")
        q_id = store.intake_questions(conn, doc_id)[0]["id"]
        with pytest.raises(ValueError, match="editable"):
            store.update_intake_question(conn, q_id, document_id=999)


def test_delete_intake_question_drops_a_spurious_row(local_db):
    with store.connect() as conn:
        doc_id = store.create_intake_document(conn, "p.pdf")
        store.add_intake_question(conn, doc_id, 1, "a heading read as a question")
        store.add_intake_question(conn, doc_id, 2, "a real question")
        q_id = store.intake_questions(conn, doc_id)[0]["id"]

        store.delete_intake_question(conn, q_id)
        left = store.intake_questions(conn, doc_id)

    assert [d["seq"] for d in left] == [2]


def test_raw_text_is_kept_so_a_paper_can_be_resplit(local_db):
    """Without this a reviewer has to re-upload the file just to pick up
    an extractor fix - which is exactly how two real papers ended up
    stuck on a stale, wrong segmentation."""
    with store.connect() as conn:
        doc_id = store.create_intake_document(
            conn, "paper.docx", raw_text="1. Alpha.\n2. Beta.\n",
            split_style="number",
        )
        doc = store.get_intake_document(conn, doc_id)
    assert doc["raw_text"] == "1. Alpha.\n2. Beta.\n"
    assert doc["split_style"] == "number"


def test_replace_intake_questions_swaps_the_whole_set(local_db):
    with store.connect() as conn:
        doc_id = store.create_intake_document(conn, "p.docx")
        store.add_intake_question(conn, doc_id, 1, "one big wrong blob")
        assert len(store.intake_questions(conn, doc_id)) == 1

        fresh = extract.segment("1. Alpha question.\n2. Beta question.\n")
        store.replace_intake_questions(conn, doc_id, fresh,
                                       fallback_grade="13")
        rows = store.intake_questions(conn, doc_id)

    assert [r["question_number"] for r in rows] == ["1", "2"]
    assert rows[0]["text"] == "Alpha question."
    assert rows[0]["grade"] == "13"


def test_parts_and_answers_round_trip(local_db):
    parsed = extract.structure(_TWO_PART_PAPER)
    with store.connect() as conn:
        doc_id = store.create_intake_document(
            conn, "GIT_Gr12_FirstTT.pdf", paper_title="First Term Examination",
            sections=parsed["sections"],
        )
        store.add_extracted_questions(conn, doc_id, parsed["questions"])
        doc = store.get_intake_document(conn, doc_id)
        rows = store.intake_questions(conn, doc_id)
        store.update_intake_document(conn, doc_id,
                                     sections=parsed["sections"][:1])
        after = store.intake_sections(store.get_intake_document(conn, doc_id))

    assert doc["paper_title"] == "First Term Examination"
    assert [s["label"] for s in store.intake_sections(doc)] == [
        "Part I", "Part II"]
    assert [r["section"] for r in rows] == [
        "Part I", "Part I", "Part II", "Part II"]
    assert rows[0]["answer"] == "(2)"
    assert [s["label"] for s in after] == ["Part I"]


def test_a_paper_read_before_parts_existed_has_no_parts(local_db):
    with store.connect() as conn:
        doc_id = store.create_intake_document(conn, "old.pdf")
        assert store.intake_sections(
            store.get_intake_document(conn, doc_id)) == []


def test_update_intake_document_sets_paper_level_fields(local_db):
    with store.connect() as conn:
        doc_id = store.create_intake_document(conn, "p.pdf")
        store.update_intake_document(
            conn, doc_id, paper_grade="8", paper_subject="History",
            status="checked",
        )
        doc = store.get_intake_document(conn, doc_id)

    assert doc["paper_grade"] == "8"
    assert doc["paper_subject"] == "History"
    assert doc["status"] == "checked"
