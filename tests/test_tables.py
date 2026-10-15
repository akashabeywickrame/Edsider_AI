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
