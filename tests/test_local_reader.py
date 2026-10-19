"""local_reader/reader.py - the OCR tool for data-entry computers. Only its
pure parts are tested here (no model is loaded: Surya is imported lazily,
the first time a page is actually read)."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.intake import extract

_spec = importlib.util.spec_from_file_location("local_reader", ROOT / "local_reader" / "reader.py")
reader = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reader)

SAMPLES = [
    "<p><b>1) පහත ප්‍රකාශ අතුරින්</b></p><ul><li>• A. දත්ත</li><li>• B. සංඛ්‍යානය</li></ul>",
    "<p>2) Which &amp; why?</p>\n<ol><li>1. one</li><li>2. two</li></ol><p>1</p>",
    "1. plain text\n2. stays exactly as it is < even with brackets >",
    "",
    "<p>1) Table below</p><table><tr><td>3</td><td>4</td></tr></table><p>2) Next</p>",
    "<table><tr><th>Subject - ICT</th><th>Grade - 12</th></tr></table>",
    "<table><tr><td>26. A</td><td>B</td></tr><tr><td>27. C</td><td>D</td></tr></table>",
]


def test_the_readers_copy_of_ocr_html_is_identical():
    # The reader is copied to other computers on its own and cannot import
    # from the repo - so it ships its own copy, which must never drift.
    assert (ROOT / "local_reader" / "ocr_html.py").read_bytes() ==         (ROOT / "src" / "intake" / "ocr_html.py").read_bytes()


@pytest.mark.parametrize("keep_tables", [False, True])
@pytest.mark.parametrize("sample", SAMPLES)
def test_the_readers_html_cleaner_matches_edsiderais(sample, keep_tables):
    # The reader is copied to other computers on its own and cannot import
    # from the repo - so its copy must never drift from the server's.
    assert (reader.html_to_lines(sample, keep_tables=keep_tables)
            == extract.html_to_lines(sample, keep_tables=keep_tables))


def _block(label, html, top, bottom):
    return {"label": label, "html": html, "bbox": [50, top, 900, bottom]}


def test_surya_table_blocks_are_left_out():
    blocks = [_block("Text", "<p>4) Study the table.</p>", 400, 440),
              _block("Table", "<table><tr><td>12</td><td>15</td></tr></table>", 460, 700),
              _block("Text", "<p>5) Next question.</p>", 720, 760)]
    assert reader.page_text(blocks, page_height=3500, first_page=False) == \
        "4) Study the table.\n5) Next question."


def test_a_header_table_on_the_first_page_is_kept_as_lines():
    header = _block("Table", "<table><tr><td>Subject - ICT</td><td>Grade - 12</td></tr></table>", 80, 300)
    question = _block("Text", "<p>1) First question.</p>", 600, 640)
    assert reader.page_text([header, question], page_height=3500, first_page=True) == \
        "Subject - ICT Grade - 12\n1) First question."
    # The same table lower down, or on a later page, is a real table.
    assert reader.page_text([header, question], page_height=3500, first_page=False) == \
        "1) First question."


def test_a_trailing_page_number_is_dropped_but_a_numbered_line_is_not():
    blocks = [{"html": "<p>4) Question?</p>"}, {"html": "<ol><li>1. A</li><li>2. B</li></ol>"},
              {"html": "<p>7</p>"}]
    assert reader.page_text(blocks) == "4) Question?\n1. A\n2. B"
    assert reader.page_text([{"html": "<p>5. last option</p>"}]) == "5. last option"


def test_a_transparent_screenshot_is_flattened_onto_white(tmp_path):
    from PIL import Image
    path = tmp_path / "shot.png"
    Image.new("RGBA", (4, 4), (0, 0, 0, 0)).save(path)
    count, pages = reader.page_images(path)
    img = next(pages)
    assert count == 1 and img.mode == "RGB" and img.getpixel((0, 0)) == (255, 255, 255)


def test_something_that_is_not_a_paper_is_refused(tmp_path):
    path = tmp_path / "notes.docx"
    path.write_bytes(b"x")
    with pytest.raises(ValueError):
        reader.page_images(path)


def test_progress_estimates_the_time_left_from_pages_read_so_far():
    job = {"id": "j", "filename": "p.pdf", "status": "reading", "error": None,
           "pages_total": 5, "pages": ["a", "", "c"], "page_seconds": [300, 200, 250],
           "load_seconds": 2.0}
    view = reader.job_view(job)
    assert (view["pages_done"], view["seconds_left"], view["text"]) == (3, 500, "a\nc")
