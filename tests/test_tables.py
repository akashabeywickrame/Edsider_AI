"""Tables are left out of extracted text - data entry adds each one to its
question as a picture (src/intake/extract.py, _data_tables). Built from
real files, not strings: a generated Word document and a generated PDF
with ruled tables, read through the same read_docx / read_pdf an upload
uses. Boxes (one column) and a paper's header table are kept - both shapes
were seen on real papers (see _data_tables' notes)."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.intake import extract

docx = pytest.importorskip("docx")
fitz = pytest.importorskip("fitz")


def _no_ocr(*_args, **_kwargs):
    raise AssertionError("OCR was not expected here")


# ---- Word ----

@pytest.fixture
def word_paper(tmp_path):
    d = docx.Document()
    header = d.add_table(rows=2, cols=2)
    header.cell(0, 0).text, header.cell(0, 1).text = "Subject", "Information Technology"
    header.cell(1, 0).text, header.cell(1, 1).text = "Grade", "12"
    d.add_paragraph("1. Study the table below and answer.")
    data = d.add_table(rows=3, cols=3)
    for r in range(3):
        for c in range(3):
            data.cell(r, c).text = f"CELLTEXT{r}{c}"
    box = d.add_table(rows=2, cols=1)
    box.cell(0, 0).text = "A shop keeps its records in a spreadsheet."
    box.cell(1, 0).text = "It wants to move them to a database."
    d.add_paragraph("2. Explain one advantage of a database.")
    path = tmp_path / "paper.docx"
    d.save(path)
    return path


def test_a_word_data_table_is_left_out_and_the_rest_kept(word_paper):
    text, note, _bold = extract.read_docx(word_paper, _no_ocr)
    assert "CELLTEXT" not in text
    assert "1. Study the table below" in text and "2. Explain one advantage" in text
    assert "table(s) left out" in note and "1 table" in note


def test_a_word_header_table_and_a_one_column_box_are_kept(word_paper):
    text, _note, _bold = extract.read_docx(word_paper, _no_ocr)
    assert "Information Technology" in text      # header, before question 1
    assert "keeps its records in a spreadsheet" in text   # a box, one column


# ---- PDF ----

def _grid(page, x0, y0, rows, cols, cell_w, cell_h, label):
    for r in range(rows + 1):
        page.draw_line((x0, y0 + r * cell_h), (x0 + cols * cell_w, y0 + r * cell_h))
    for c in range(cols + 1):
        page.draw_line((x0 + c * cell_w, y0), (x0 + c * cell_w, y0 + rows * cell_h))
    for r in range(rows):
        for c in range(cols):
            page.insert_text((x0 + c * cell_w + 4, y0 + r * cell_h + cell_h / 2 + 4),
                             f"{label}{r}{c}", fontsize=9)
    return (x0, y0, x0 + cols * cell_w, y0 + rows * cell_h)


@pytest.fixture
def pdf_paper(tmp_path):
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    _grid(page, 60, 40, 2, 2, 120, 22, "HEAD")          # header grid, top of page 1
    page.insert_text((60, 160), "1. Study the table below and answer the question.", fontsize=11)
    _grid(page, 60, 200, 3, 3, 120, 24, "CELL")          # a real data table
    page.insert_text((60, 320), "2. Explain one advantage of a database.", fontsize=11)
    page2 = doc.new_page(width=595, height=842)
    page2.insert_text((60, 60), "3. A third question on page two.", fontsize=11)
    # A grid filling nearly the whole page - a border, not a table.
    _grid(page2, 20, 80, 2, 2, 277, 370, "BORDER")
    path = tmp_path / "paper.pdf"
    doc.save(path)
    return path


def test_a_pdf_data_table_is_left_out_and_the_rest_kept(pdf_paper):
    text, note, _bold = extract.read_pdf(pdf_paper, _no_ocr)
    assert "CELL" not in text
    assert "1. Study the table below" in text and "2. Explain one advantage" in text
    assert "3. A third question" in text
    assert "1 table(s) left out" in note


def test_a_pdf_header_table_and_a_page_border_are_kept(pdf_paper):
    text, _note, _bold = extract.read_pdf(pdf_paper, _no_ocr)
    assert "HEAD00" in text
    assert "BORDER00" in text


def test_a_page_read_by_ocr_has_its_table_painted_out_first(pdf_paper):
    seen = []

    def fake_ocr(image):
        seen.append(image.convert("L"))
        return "page text"

    extract.read_pdf(pdf_paper, fake_ocr, force_ocr=True)
    first = seen[0]
    scale = first.width / 595
    # The data table's area (60,200)-(420,272) is pure white...
    table_area = first.crop((int(61 * scale), int(201 * scale), int(419 * scale), int(271 * scale)))
    assert table_area.getextrema()[0] == 255
    # ...while question 1's line above it still has ink.
    question_line = first.crop((int(60 * scale), int(148 * scale), int(400 * scale), int(165 * scale)))
    assert question_line.getextrema()[0] < 128


# ---- questions laid out in a grid are questions, not a table ----
# The real shape: Political Science page 7, "26. <statement> | <statement>"
# down to 30. - dropping it deleted the five questions outright.

def test_a_word_question_grid_is_kept(tmp_path):
    d = docx.Document()
    d.add_paragraph("1. First question.")
    grid = d.add_table(rows=3, cols=2)
    for r, n in enumerate((26, 27, 28)):
        grid.cell(r, 0).text = f"{n}. Statement {n} A"
        grid.cell(r, 1).text = f"Statement {n} B"
    legend = d.add_table(rows=2, cols=3)
    for r, n in enumerate((1, 2)):
        legend.cell(r, 0).text = str(n)
        legend.cell(r, 1).text, legend.cell(r, 2).text = "LEGENDTRUE", "LEGENDFALSE"
    path = tmp_path / "grid.docx"
    d.save(path)
    text, note, _bold = extract.read_docx(path, _no_ocr)
    assert "26. Statement 26 A" in text and "28. Statement 28 A" in text
    assert "LEGEND" not in text            # a bare "1" in a cell is a real table
    assert "1 table(s) left out" in note


def test_a_pdf_question_grid_is_kept(tmp_path):
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 300), "25. The question before the grid.", fontsize=11)
    x0, y0, w, h = 60, 340, 220, 24
    for r in range(4):
        page.draw_line((x0, y0 + r * h), (x0 + 2 * w, y0 + r * h))
    for c in range(3):
        page.draw_line((x0 + c * w, y0), (x0 + c * w, y0 + 3 * h))
    for r, n in enumerate((26, 27, 28)):
        page.insert_text((x0 + 4, y0 + r * h + 16), f"{n}. Statement {n} A", fontsize=9)
        page.insert_text((x0 + w + 4, y0 + r * h + 16), f"Statement {n} B", fontsize=9)
    path = tmp_path / "grid.pdf"
    doc.save(path)
    text, note, _bold = extract.read_pdf(path, _no_ocr)
    assert "26. Statement 26 A" in text and "28. Statement 28 A" in text
    assert "left out" not in note


def test_a_data_table_numbering_its_own_rows_from_1_is_still_a_table():
    # The real shape: Grade 13 Biology, "1. First product of CO2 fixation | C3 | C4".
    html = ("<table><tr><td>1. First product</td><td>OAA</td><td>3-PGA</td></tr>"
            "<tr><td>2. CO2 acceptor</td><td>PEP</td><td>RuBP</td></tr></table>"
            "<p>13) The next question.</p>")
    assert extract.html_to_lines(html) == "13) The next question."


def test_a_pasted_question_grid_is_kept_and_a_legend_dropped():
    html = ("<table><tr><td>26. Statement A</td><td>Statement B</td></tr>"
            "<tr><td>27. Statement C</td><td>Statement D</td></tr></table>"
            "<table><tr><td>1</td><td>LEGENDTRUE</td></tr><tr><td>2</td><td>LEGENDFALSE</td></tr></table>")
    assert extract.html_to_lines(html) == "26. Statement A Statement B\n27. Statement C Statement D"


# ---- HTML from the local reader, or pasted ----

def test_pasted_html_loses_its_tables():
    html = ("<p>1) What does the table show?</p><table><tr><td>3</td><td>4</td></tr>"
            "<tr><td>5</td><td>6</td></tr></table><p>2) Next question</p>")
    assert extract.html_to_lines(html) == "1) What does the table show?\n2) Next question"


def test_a_kept_table_becomes_one_line_per_row():
    html = "<table><tr><td>Subject - ICT</td><td>Grade - 12</td></tr></table>"
    assert extract.html_to_lines(html, keep_tables=True) == "Subject - ICT Grade - 12"


def test_plain_text_is_never_touched():
    assert extract.html_to_lines("1. a | b\n2. c") == "1. a | b\n2. c"


# ---- tables kept as pictures, attached where they were ----
# An upload passes `tables`: each data table comes back as a picture of
# itself, with a "[Table N]" line where it stood, and lands on the
# question (or answer) around it.

PNG = b"\x89PNG\r\n\x1a\n"


def _size(png):
    from PIL import Image
    from io import BytesIO
    return Image.open(BytesIO(png)).size


def test_a_pdf_table_becomes_a_picture_where_it_was(pdf_paper):
    tables = []
    text, note, _bold = extract.read_pdf(pdf_paper, _no_ocr, tables=tables)
    assert [(t["n"], t["page"]) for t in tables] == [(1, 1)]
    assert tables[0]["png"].startswith(PNG)
    # The 360 x 72 point table, at 200 dpi, with a little margin.
    width, height = _size(tables[0]["png"])
    assert 1000 <= width <= 1030 and 200 <= height <= 230
    assert "CELL" not in text
    assert text.index("1. Study the table") < text.index("[Table 1]") < text.index("2. Explain")
    assert "1 table(s) kept as pictures" in note
    # The header grid and the page border are still text, not pictures.
    assert "HEAD00" in text and "BORDER00" in text

    q1, q2, _q3 = extract.structure(text)["questions"]
    assert q1["tables"] == [{"n": 1, "slot": "question"}]
    assert "[Table" not in q1["text"] and "tables" not in q2


def test_a_scanned_page_is_read_around_its_table(pdf_paper):
    """OCR'd in bands - above the table, the table's own band with the
    table painted white, below it - so the marker lands in between."""
    bands = []

    def fake_ocr(image):
        bands.append(image.convert("L"))
        return f"band {len(bands)}"

    tables = []
    text, _note, _bold = extract.read_pdf(pdf_paper, fake_ocr, force_ocr=True, tables=tables)
    page_one = text.split("band 1", 1)[1]
    assert page_one.index("band 2") < page_one.index("[Table 1]") < page_one.index("band 3")
    # The table's own band is white where the table is.
    table_band = bands[1]
    middle = table_band.crop((int(table_band.width * 0.2), 5, int(table_band.width * 0.6),
                              table_band.height - 5))
    assert middle.getextrema()[0] == 255
    assert len(tables) == 1 and tables[0]["png"].startswith(PNG)


def test_a_word_table_is_drawn_with_its_merged_cells(tmp_path):
    d = docx.Document()
    d.add_paragraph("1. Study the table below and answer.")
    t = d.add_table(rows=3, cols=3)
    t.style = "Table Grid"
    t.cell(0, 0).merge(t.cell(0, 2)).text = "CO₂ fixation in plants"
    t.cell(1, 0).text, t.cell(1, 1).text, t.cell(1, 2).text = "ශාකය", "C3", "தாவரம்"
    t.cell(1, 0).merge(t.cell(2, 0))
    t.cell(2, 1).text, t.cell(2, 2).text = "C4", "x² ≤ 5"
    d.add_paragraph("2. Explain one advantage of a database.")
    path = tmp_path / "merged.docx"
    d.save(path)

    tables = []
    text, note, _bold = extract.read_docx(path, _no_ocr, tables=tables)
    assert text.index("1. Study") < text.index("[Table 1]") < text.index("2. Explain")
    assert "ශාකය" not in text and "1 table(s) kept as pictures" in note
    assert len(tables) == 1 and tables[0]["png"].startswith(PNG)
    width, height = _size(tables[0]["png"])
    assert width > height > 100


def test_an_answer_key_printed_as_a_grid_stays_text(tmp_path):
    """The GIT Grade 12 marking scheme prints its MCQ key as a 5 x 8 grid
    of "Q1: (2)". It is the paper's answers, not a table to show."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 300), "1. Which is an output device?", fontsize=11)
    page.insert_text((60, 360), "MARKING SCHEME", fontsize=11)
    x0, y0, w, h = 60, 400, 90, 22
    for r in range(3):
        page.draw_line((x0, y0 + r * h), (x0 + 5 * w, y0 + r * h))
    for c in range(6):
        page.draw_line((x0 + c * w, y0), (x0 + c * w, y0 + 2 * h))
    for r in range(2):
        for c in range(5):
            page.insert_text((x0 + c * w + 10, y0 + r * h + 15), f"Q{r * 5 + c + 1}: ({c % 4 + 1})",
                             fontsize=9)
    path = tmp_path / "key.pdf"
    doc.save(path)
    tables = []
    text, note, _bold = extract.read_pdf(path, _no_ocr, tables=tables)
    assert tables == [] and "Q1: (1)" in text and "Q10: (1)" in text


def test_a_table_in_a_worked_solution_goes_to_the_answer():
    text = (
        "Question 1\nConvert 53 into binary.\n"
        "Solution\nDivide by 2 repeatedly:\n[Table 1]\nSo 53 = 110101.\n"
        "Question 2\nComplete the truth table below.\n[Table 2]\n"
    )
    q1, q2 = extract.structure(text, style="question_word")["questions"]
    assert q1["tables"] == [{"n": 1, "slot": "answer"}]
    assert q1["answer"] == "Divide by 2 repeatedly:\nSo 53 = 110101."
    assert q2["tables"] == [{"n": 2, "slot": "question"}]
    assert q2["text"] == "Complete the truth table below."


def test_a_table_before_the_first_question_goes_to_it():
    text = "Use the table below for all questions.\n[Table 1]\n1. First question.\n2. Second one.\n"
    parsed = extract.structure(text)
    assert "[Table" not in (parsed["paper_instructions"] or "")
    assert parsed["questions"][0]["tables"] == [{"n": 1, "slot": "question"}]


def test_a_scheme_part_label_matching_no_part_stays_on_the_question():
    """The GIT A/L model paper's scheme labels question (02)'s parts "(a)"
    where the paper prints "(1)(a)" - the worked answer was dropped."""
    paper = (
        "Structured Questions\n"
        "Question 01\n"
        "(1) A clinic keeps patient records on paper.\n"
        "(a) Name two tables a database for it would need, with their fields. [6]\n"
        "(b) State one advantage of using a database for these records. [2]\n"
        "(2) The clinic's receptionist books appointments by telephone.\n"
        "(a) Describe how an online booking form would change her work. [4]\n"
        "(b) Name one risk of keeping the records online and how to reduce it. [3]\n"
        "MARKING SCHEME\n"
        "Question (01) [15 marks]\n"
        "(a) Two Database Tables\nPATIENT and APPOINTMENT.\n"
        "(b) Advantage\nRecords can be searched quickly.\n"
    )
    qs = {q["question_number"]: q
          for q in extract.structure(paper, style="question_word")["questions"]}
    assert qs["01(1)(a)"]["answer"] is None          # "(a)" is not "(1)(a)"
    assert "(a) Two Database Tables" in qs["01"]["answer"]
    assert "searched quickly" in qs["01"]["answer"]


# ---- stored: attached to their questions, and again after a re-split ----

def test_an_uploads_tables_land_on_their_questions_and_survive_a_resplit(local_db, pdf_paper):
    from src import store
    result = extract.extract(pdf_paper, ocr=_no_ocr)
    with store.connect() as conn:
        doc_id = store.create_intake_document(conn, "paper.pdf", raw_text=result["raw_text"],
                                              sections=result["sections"])
        store.save_document_tables(conn, doc_id, result["tables"])
        store.add_extracted_questions(conn, doc_id, result["questions"])
        first = store.intake_questions(conn, doc_id)[0]
        images = store.question_images_for_document(conn, doc_id)[first["id"]]
        assert [i["slot"] for i in images] == ["question"]
        assert store.question_image_blob(conn, images[0]["id"]) == result["tables"][0]["png"]

        # "Split it differently" re-reads the kept text, markers and all.
        parsed = extract.structure(result["raw_text"])
        store.replace_intake_questions(conn, doc_id, parsed["questions"],
                                       sections=parsed["sections"],
                                       instruction_groups=parsed["instruction_groups"])
        first = store.intake_questions(conn, doc_id)[0]
        again = store.question_images_for_document(conn, doc_id)[first["id"]]
    assert [i["slot"] for i in again] == ["question"] and first["text"].startswith("Study the table")


def test_reading_an_older_upload_again_brings_its_tables(local_db, pdf_paper):
    """A paper uploaded before tables were kept has no "[Table N]" lines
    in its text; reading its file again gives it them."""
    from src import store
    import service
    plain, _note, _bold = extract.read_pdf(pdf_paper, _no_ocr)
    with store.connect() as conn:
        doc_id = store.create_intake_document(conn, "paper.pdf", file_type="pdf", raw_text=plain,
                                              file_blob=pdf_paper.read_bytes())
        store.add_extracted_questions(conn, doc_id, extract.structure(plain)["questions"])
        assert store.question_images_for_document(conn, doc_id) == {}

    service.reread_document(doc_id, service.RereadRequest(style="number"), identity="reviewer")
    with store.connect() as conn:
        questions = store.intake_questions(conn, doc_id)
        images = store.question_images_for_document(conn, doc_id)
        raw = store.get_intake_document(conn, doc_id)["raw_text"]
    assert [q["question_number"] for q in questions] == ["1", "2", "3"]
    assert [i["slot"] for i in images[questions[0]["id"]]] == ["question"]
    assert "[Table 1]" in raw


# ---- pictures kept as themselves, like tables ----

def _picture_png(width=120, height=80, colour=(200, 40, 40)):
    from PIL import Image
    from io import BytesIO
    out = BytesIO()
    Image.new("RGB", (width, height), colour).save(out, format="PNG")
    return out.getvalue()


def test_a_word_picture_lands_on_its_question_or_option(tmp_path):
    from io import BytesIO
    d = docx.Document()
    d.add_paragraph("1. Study the diagram below and name the part labelled X.")
    d.add_paragraph().add_run().add_picture(BytesIO(_picture_png()))
    d.add_paragraph("2. Which of these is a mammal?")
    d.add_paragraph("A. A shark")
    option = d.add_paragraph("B. ")
    option.add_run().add_picture(BytesIO(_picture_png(colour=(40, 40, 200))))
    d.add_paragraph("C. A frog")
    d.add_paragraph("D. A lizard")
    path = tmp_path / "pictures.docx"
    d.save(path)

    result = extract.extract(path, ocr=lambda image: "")
    assert [(t["n"], t["kind"], t["content_type"]) for t in result["tables"]] == [
        (1, "picture", "image/png"), (2, "picture", "image/png")]
    q1, q2 = result["questions"]
    assert q1["tables"] == [{"n": 1, "slot": "question"}]
    assert q2["tables"] == [{"n": 2, "slot": "option:B"}]
    assert "[Picture" not in q1["text"] + q2["text"] and "[image]" not in q1["text"]


def test_a_tiny_word_picture_is_not_a_figure(tmp_path):
    from io import BytesIO
    d = docx.Document()
    d.add_paragraph("1. A question with a bullet picture.")
    d.add_paragraph().add_run().add_picture(BytesIO(_picture_png(10, 10)))
    path = tmp_path / "tiny.docx"
    d.save(path)
    assert extract.extract(path, ocr=lambda image: "")["tables"] == []


def test_a_pdf_picture_on_a_text_page_lands_where_it_sits(tmp_path):
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 100), "1. Study the graph below and answer the question.", fontsize=11)
    page.insert_image(fitz.Rect(60, 120, 300, 280), stream=_picture_png(480, 320))
    page.insert_text((60, 320), "2. A second question after the graph.", fontsize=11)
    path = tmp_path / "graph.pdf"
    doc.save(path)

    result = extract.extract(path, ocr=lambda image: "")
    assert [(t["kind"], t["page"]) for t in result["tables"]] == [("picture", 1)]
    assert result["tables"][0]["png"].startswith(PNG)
    q1, q2 = result["questions"]
    assert q1["tables"] == [{"n": 1, "slot": "question"}] and "tables" not in q2


def test_a_picture_before_the_first_question_is_the_papers_crest():
    text = "[Picture 1]\nDepartment of Examinations\n1. First question.\n2. Second.\n"
    parsed = extract.structure(text)
    assert all("tables" not in q for q in parsed["questions"])


# ---- pictures that ARE the options (paper 6, question 7) ----
# Five bags of fish labelled I to V, the labels printed beside the
# pictures, no option text at all: each picture is its option.

def test_pdf_option_pictures_go_to_their_options(tmp_path):
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((89, 395), "07.Select the correct way of packaging of live fish.", fontsize=11)
    # As paper 6 lays them out: I, II, III on a row, IV and V under it;
    # "II" sits above its bag, "V" inside the box of the bag before it.
    for label, (lx, ly) in {"I": (72, 424), "II": (231, 424), "III": (385, 424),
                            "IV": (108, 538), "V": (319, 538)}.items():
        page.insert_text((lx, ly), label, fontsize=11)
    boxes = {"I": (88, 415, 164, 488), "II": (179, 431, 261, 511), "III": (358, 414, 459, 511),
             "IV": (126, 530, 347, 710), "V": (389, 527, 463, 604)}
    colours = {"I": (200, 0, 0), "II": (0, 200, 0), "III": (0, 0, 200), "IV": (200, 200, 0),
               "V": (0, 200, 200)}
    for label, box in boxes.items():
        page.insert_image(fitz.Rect(*box), stream=_picture_png(200, 200, colours[label]))
    page.insert_text((89, 760), "08.The next question, about ornamental fish breeding.", fontsize=11)
    path = tmp_path / "fish.pdf"
    doc.save(path)

    result = extract.extract(path, ocr=lambda image: "")
    q7 = result["questions"][0]
    assert extract.split_mcq_options(q7["text"])[1] and [
        o["label"] for o in extract.split_mcq_options(q7["text"])[1]] == ["I", "II", "III", "IV", "V"]
    # Which picture went where, by its colour.
    from PIL import Image
    from io import BytesIO
    pictures = {t["n"]: Image.open(BytesIO(t["png"])).convert("RGB") for t in result["tables"]}
    got = {}
    for t in q7["tables"]:
        image = pictures[t["n"]]
        got[t["slot"]] = image.getpixel((image.width // 2, image.height // 2))
    for label, colour in colours.items():
        assert got[f"option:{label}"] == colour


def test_word_option_pictures_are_matched_in_the_order_they_show():
    """Word keeps a floating picture where it is anchored: the first of the
    row in the file is the middle one on the page (paper 6.docx)."""
    text = ("07.Select the correct way of packaging of live fish.\n"
            "[Picture 1]\n\n[Picture 2]\n\n[Picture 3]\nI\t\tII\tIII\n\t\t\n\n\tIV\tV\n"
            "[Picture 4]\n\n[Picture 5]\nS\n08.The next question.")
    row1, row2 = object(), object()
    # Offsets across, as in the file: picture 3 left, 1 middle, 2 right.
    places = {1: (id(row1), 1362075), 2: (id(row1), 3638550), 3: (id(row1), 209550),
              4: (id(row2), 300000), 5: (id(row2), 2900000)}
    marked = extract._docx_option_pictures(text, places)
    assert "[Picture 3 option I]" in marked and "[Picture 1 option II]" in marked
    assert "[Picture 2 option III]" in marked and "[Picture 5 option V]" in marked

    q7 = extract.structure(marked)["questions"][0]
    slots = {t["n"]: t["slot"] for t in q7["tables"]}
    assert slots == {3: "option:I", 1: "option:II", 2: "option:III", 4: "option:IV", 5: "option:V"}
    assert q7["text"].endswith("I.\nII.\nIII.\nIV.\nV.") and "\t" not in q7["text"]


def test_pictures_labelled_beside_text_options_stay_on_the_question():
    """Paper 6's question 42: ploughs labelled A, B, C, and text options
    I to V that talk about them - the pictures are the question's."""
    text = ("42.Choose the correct implements with its use.\n"
            "[Picture 6 option A]\n[Picture 7 option B]\n[Picture 8 option C]\nA B C\n"
            "I. A-Sweep hoe is used for initial land preparation\n"
            "II. B-Primary plough is used for secondary tillage\n"
            "III. C-Primary plough is used for initial land preparation\n"
            "IV. A-Swiss hoe is used for deep land preparation\n"
            "V. B-Cultivator is used for both tillages\n")
    q = extract.structure(text)["questions"][0]
    assert {t["slot"] for t in q["tables"]} == {"question"}
