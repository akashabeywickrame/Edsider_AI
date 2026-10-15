"""EdsiderAI local reader - reads a scanned question paper on THIS computer.

Why this exists: EdsiderAI's server reads typed PDFs and Word files itself,
but its OCR (Tesseract) cannot read scanned Sinhala/Tamil papers - on the
real Grade 12 Sinhala Business Statistics scan it turned every "A." into
"&," and dropped the "1."-"5." answer numbers. Surya, a vision OCR model,
read the same page essentially perfectly, but needs minutes of computing
per page, and Azure App Service has no GPU at any tier. So the reading
happens here, on the data-entry person's own computer, and only the text
goes to EdsiderAI: copy it, then Paper intake -> "Paste text".

Nothing leaves this computer: the page is served on 127.0.0.1 only, the
model runs locally, and the text goes nowhere until a person pastes it.

Usage:
    start.bat                        the page, at http://127.0.0.1:8765
    python reader.py --time FILE     time the first page (the laptop test)
"""

import argparse
import html
import os
import re
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORT = 8765

# Surya reads these when it is imported, so they are set first. One page
# at a time is all this tool asks for (it reports progress per page), so
# the reading program only needs room for 2 at once - the default
# reserves room for 8 (98,304 tokens of context, against 24,576 here),
# which is memory a laptop cannot spare.
os.environ.setdefault("SURYA_INFERENCE_BACKEND", "llamacpp")
os.environ.setdefault("SURYA_INFERENCE_PARALLEL", "2")
_LLAMA = HERE / "llamacpp" / "llama-server.exe"
if _LLAMA.exists():
    os.environ.setdefault("LLAMA_CPP_BINARY", str(_LLAMA))

RENDER_DPI = 300  # what the measured runs used; 400 read no better
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}


# ---------------------------------------------------------------- text
# Mirrors src/intake/extract.py's html_to_lines() - this folder is copied
# to other computers on its own, so it cannot import from the repo.
# tests/test_local_reader.py checks the two stay identical.

_BLOCK_TAG = re.compile(r"</?(?:p|li|ul|ol|br|div|h[1-6]|tr|table)\b[^>]*>", re.IGNORECASE)
_ANY_TAG = re.compile(r"<[^>]+>")
_LOOKS_LIKE_HTML = re.compile(r"</(?:p|li|td|th|tr|table)>", re.IGNORECASE)
_BULLET = re.compile(r"^[•●▪‣⁃]\s*")
_TABLE_HTML = re.compile(r"<table\b.*?</table\s*>", re.IGNORECASE | re.DOTALL)
_CELL_END = re.compile(r"</t[dh]\s*>", re.IGNORECASE)

# Data entry adds every table to its question as a picture, so a table's
# cells are left out of the text - except a paper's header laid out as a
# grid (subject / grade / term) at the top of the first page, which
# EdsiderAI reads the paper's details from. Same rule as EdsiderAI's own
# extractor (src/intake/extract.py, _data_tables).
HEADER_ZONE = 0.3

# Questions laid out in a grid ("26. <statement> | <statement>" - a real
# Political Science paper) are questions, not a table: rows whose first
# cell starts with a numbered item keep the table - when the numbers carry
# on the paper's numbering (not starting at 1; a data table numbers its
# own items from 1). A bare "1" alone in a cell never counts.
_NUMBERED_CELL = re.compile(r"^\s*\(?(\d{1,3})\s*[.)]\s*\S")
_HTML_ROW = re.compile(r"<tr\b.*?</tr\s*>", re.IGNORECASE | re.DOTALL)
_HTML_CELL = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]\s*>", re.IGNORECASE | re.DOTALL)


def _rows_are_questions(first_cells):
    numbers = [int(m.group(1)) for m in (_NUMBERED_CELL.match(c or "") for c in first_cells) if m]
    return len(numbers) >= 2 and numbers[0] > 1


def _drop_unless_questions(match):
    table = match.group(0)
    first_cells = []
    for row in _HTML_ROW.findall(table):
        cells = [html.unescape(_ANY_TAG.sub("", c)).strip() for c in _HTML_CELL.findall(row)]
        first_cells.append(next((c for c in cells if c), ""))
    return table if _rows_are_questions(first_cells) else "\n"


def html_to_lines(text, keep_tables=False):
    """One line per paragraph or list item, so "1." / "A." / "අ)" stay at
    the start of a line - where EdsiderAI's splitter looks for them. A
    table's content is left out unless `keep_tables` (the header table)."""
    if not _LOOKS_LIKE_HTML.search(text or ""):
        return text
    if not keep_tables:
        text = _TABLE_HTML.sub(_drop_unless_questions, text)
    text = _CELL_END.sub(" ", text)
    lines = []
    for chunk in _BLOCK_TAG.split(text):
        line = _BULLET.sub("", html.unescape(_ANY_TAG.sub("", chunk)).strip())
        if line:
            lines.append(line)
    return "\n".join(lines)


def _is_header_table(block, page_height, first_page):
    bbox = block.get("bbox") or []
    return bool(first_page and page_height and len(bbox) == 4
                and bbox[3] <= HEADER_ZONE * page_height)


def page_text(blocks, page_height=None, first_page=False):
    """A page's text from Surya's blocks, minus its tables (Surya labels
    them "Table") and a trailing page number - a bare "7" alone on the
    last line belongs to the page, not the paper."""
    parts = []
    for b in blocks:
        source = b.get("html") or b.get("text") or ""
        # A table block goes through html_to_lines like the rest, which
        # drops it unless its rows are questions; a header table on page 1
        # is kept whatever it holds.
        header = b.get("label") == "Table" and _is_header_table(b, page_height, first_page)
        parts.append(html_to_lines(source, keep_tables=header))
    lines = "\n".join(p for p in parts if p).split("\n")
    if lines and re.fullmatch(r"\d{1,3}", lines[-1].strip()):
        lines.pop()
    return "\n".join(lines).strip()


# ---------------------------------------------------------------- pages

def page_images(path):
    """Every page of a PDF rendered at RENDER_DPI, or the one image - as
    PIL images, in order. Pages are rendered as they are needed, not all
    up front: a 30-page paper at 300 dpi is ~1 GB of pixels."""
    from PIL import Image, ImageOps
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf":
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(path))
        count = len(pdf)

        def gen():
            for i in range(count):
                yield pdf[i].render(scale=RENDER_DPI / 72).to_pil().convert("RGB")
        return count, gen()
    if suffix in IMAGE_TYPES:
        img = ImageOps.exif_transpose(Image.open(path))
        if img.mode in ("RGBA", "LA", "P"):
            # A transparent screenshot, converted naively, turns black.
            bg = Image.new("RGB", img.size, "white")
            rgba = img.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[-1])
            img = bg
        return 1, iter([img.convert("RGB")])
    raise ValueError(f"Not a PDF or an image: {Path(path).name}")


class Reader:
    """Surya, loaded once and kept: starting the reading program costs
    seconds to minutes, so every paper after the first skips it."""

    def __init__(self):
        self._predictor = None

    def load(self):
        if self._predictor is None:
            from surya.inference import SuryaInferenceManager
            from surya.recognition import RecognitionPredictor
            self._predictor = RecognitionPredictor(SuryaInferenceManager())
        return self._predictor

    def read_page(self, image, first_page=False):
        result = self.load()([image], full_page=True)[0]
        return page_text(result.model_dump()["blocks"], image.height, first_page)


READER = Reader()


# ---------------------------------------------------------------- jobs

JOBS = {}
_BUSY = threading.Lock()  # one paper at a time - each page uses the whole CPU


def _run_job(job, path):
    try:
        job["status"] = "starting"
        started = time.time()
        READER.load()
        job["load_seconds"] = round(time.time() - started, 1)
        count, pages = page_images(path)
        job["pages_total"] = count
        job["status"] = "reading"
        for n, image in enumerate(pages):
            t = time.time()
            job["pages"].append(READER.read_page(image, first_page=n == 0))
            job["page_seconds"].append(round(time.time() - t, 1))
        job["status"] = "done"
    except Exception as e:  # shown on the page - nothing else would see it
        job["status"] = "failed"
        job["error"] = f"{type(e).__name__}: {e}"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
        _BUSY.release()


def job_view(job):
    done = len(job["pages"])
    secs = job["page_seconds"]
    remaining = None
    if secs and job["pages_total"]:
        remaining = round(sum(secs) / len(secs) * (job["pages_total"] - done))
    return {
        "id": job["id"], "filename": job["filename"], "status": job["status"],
        "error": job["error"], "pages_total": job["pages_total"],
        "pages_done": done, "page_seconds": secs,
        "load_seconds": job["load_seconds"], "seconds_left": remaining,
        "text": "\n".join(p for p in job["pages"] if p),
    }


# ---------------------------------------------------------------- web

def create_app():
    from fastapi import FastAPI, File, HTTPException, UploadFile
    from fastapi.responses import HTMLResponse, PlainTextResponse

    app = FastAPI(title="EdsiderAI local reader")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (HERE / "page.html").read_text(encoding="utf-8")

    @app.post("/read")
    def read(file: UploadFile = File(...)):
        suffix = Path(file.filename or "").suffix.lower()
        if suffix != ".pdf" and suffix not in IMAGE_TYPES:
            raise HTTPException(400, "Choose a PDF or a picture (PNG, JPG, TIFF, BMP).")
        if not _BUSY.acquire(blocking=False):
            raise HTTPException(409, "Already reading a paper - wait for it to finish.")
        tmp = HERE / f"_upload_{uuid.uuid4().hex}{suffix}"
        tmp.write_bytes(file.file.read())
        job = {"id": uuid.uuid4().hex, "filename": file.filename, "status": "queued",
               "error": None, "pages_total": None, "pages": [], "page_seconds": [],
               "load_seconds": None}
        JOBS[job["id"]] = job
        threading.Thread(target=_run_job, args=(job, tmp), daemon=True).start()
        return {"id": job["id"]}

    @app.get("/jobs/{job_id}")
    def status(job_id: str):
        if job_id not in JOBS:
            raise HTTPException(404, "No such job - the reader may have been restarted.")
        return job_view(JOBS[job_id])

    @app.get("/jobs/{job_id}/text", response_class=PlainTextResponse)
    def download(job_id: str):
        if job_id not in JOBS:
            raise HTTPException(404, "No such job.")
        job = JOBS[job_id]
        name = Path(job["filename"] or "paper").stem + ".txt"
        return PlainTextResponse(
            job_view(job)["text"],
            headers={"Content-Disposition": f'attachment; filename="{name}"'})

    return app


# ---------------------------------------------------------------- timing

def time_first_pages(path, pages=1):
    """The laptop test: how long one page takes on THIS computer. Prints
    the numbers to send back - nothing else is needed to decide whether
    this machine is fast enough."""
    import platform
    print(f"\nTiming the EdsiderAI reader on {platform.node()} "
          f"({os.cpu_count()} CPU threads)")
    t = time.time()
    READER.load()
    print(f"  starting the reader : {time.time() - t:6.1f} s  (once per session)")
    count, images = page_images(path)
    times = []
    for n, image in enumerate(images, 1):
        if n > pages:
            break
        t = time.time()
        text = READER.read_page(image, first_page=n == 1)
        times.append(time.time() - t)
        print(f"  page {n} of {count}         : {times[-1]:6.1f} s  ({len(text)} characters read)")
    if times:
        per = sum(times) / len(times)
        print(f"\n  about {per / 60:.1f} min per page - a {count}-page paper "
              f"would take about {per * count / 60:.0f} min here.\n")


def main():
    p = argparse.ArgumentParser(description="EdsiderAI local reader")
    p.add_argument("--time", metavar="FILE", help="time reading the first page(s) of FILE")
    p.add_argument("--pages", type=int, default=1, help="with --time: how many pages")
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()
    if args.time:
        time_first_pages(args.time, args.pages)
        return
    import uvicorn
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    print(f"\nEdsiderAI reader running at http://127.0.0.1:{PORT} - "
          "keep this window open while you use it.\n")
    # 127.0.0.1 only: this page is for this computer, never the network.
    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    sys.exit(main())
