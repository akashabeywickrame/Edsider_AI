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
# Turning Surya's HTML into text - formulas, equations, tables - is
# ocr_html.py, an identical copy of EdsiderAI's src/intake/ocr_html.py:
# this folder is copied to other computers on its own and cannot import
# from the repo. tests/test_local_reader.py fails if the two differ.
sys.path.insert(0, str(HERE))
from ocr_html import html_to_lines  # noqa: E402

# Data entry adds every table to its question as a picture, so a table's
# cells are left out of the text - except a paper's header laid out as a
# grid (subject / grade / term) at the top of the first page, which
# EdsiderAI reads the paper's details from. Same rule as EdsiderAI's own
# extractor (src/intake/extract.py, _data_tables).
HEADER_ZONE = 0.3

# What Surya labels a page's own header and footer - see page_text().
PAGE_FURNITURE = {"PageHeader", "PageFooter"}


def _is_header_table(block, page_height, first_page):
    bbox = block.get("bbox") or []
    return bool(first_page and page_height and len(bbox) == 4
                and bbox[3] <= HEADER_ZONE * page_height)


def page_text(blocks, page_height=None, first_page=False):
    """A page's text from Surya's blocks, minus its tables (Surya labels
    them "Table") and a trailing page number - a bare "7" alone on the
    last line belongs to the page, not the paper.

    Page headers and footers are left out too - Surya labels them. On the
    real A/L Chemistry scan they were the paper code and page number
    ("AL/2025/02/S-I", "- 4 -"), a serial number, and "[see the next
    page]" ("[පස්වැනි පිටුව බලන්න.]"), which landed in the middle of
    whichever question ran on to the next page and hid the marks line
    before it."""
    parts = []
    for b in blocks:
        if b.get("label") in PAGE_FURNITURE:
            continue
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

def page_images(path, only=None):
    """Every page of a PDF rendered at RENDER_DPI, or the one image - as
    PIL images, in order. Pages are rendered as they are needed, not all
    up front: a 30-page paper at 300 dpi is ~1 GB of pixels.

    `only` is the page numbers (from 1) to render; the others are skipped
    without being rendered. The count returned is always the whole file's."""
    from PIL import Image, ImageOps
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf":
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(path))
        count = len(pdf)

        def gen():
            for i in range(count):
                if only is None or i + 1 in only:
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
        return 1, iter([img.convert("RGB")] if only is None or 1 in only else [])
    raise ValueError(f"Not a PDF or an image: {Path(path).name}")


def page_picture(path, number, width):
    """One page as a PNG, `width` pixels wide - the thumbnails that let a
    person untick adverts and blank pages before spending minutes on each,
    and the larger view a page's text is checked against."""
    import io
    from PIL import Image, ImageOps
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf":
        import pypdfium2 as pdfium
        page = pdfium.PdfDocument(str(path))[number - 1]
        img = page.render(scale=width / page.get_width()).to_pil()
    else:
        img = ImageOps.exif_transpose(Image.open(path))
        img.thumbnail((width, width * 3))
    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG")
    return out.getvalue()


# A line that opens with a question number: "1.", "09 The...", "(12)",
# "3) ...". The first thing after the number must not be a digit or
# punctuation, so a list of data ("30, 35, 38") is not counted.
_QUESTION_LINE = re.compile(r"^\s*\(?\d{1,3}\s*[.)]?\s+[^\d\s,.;:]", re.M)
_SCRIPTS = {"Sinhala": (0x0D80, 0x0DFF), "Tamil": (0x0B80, 0x0BFF)}


def page_stats(text, blocks=()):
    """What a person checking the reading wants to know about one page:
    how much was read, how many questions it seems to hold, its formulas,
    tables and pictures (data entry adds those as pictures), which
    language it is in, and anything that looks wrong."""
    counts = {name: 0 for name in _SCRIPTS}
    latin = 0
    for ch in text:
        code = ord(ch)
        for name, (lo, hi) in _SCRIPTS.items():
            if lo <= code <= hi:
                counts[name] += 1
                break
        else:
            if ch.isascii() and ch.isalpha():
                latin += 1
    counts["English"] = latin
    letters = sum(counts.values())
    languages = [name for name, n in sorted(counts.items(), key=lambda kv: -kv[1])
                 if letters and n / letters >= 0.15]
    chars = len(re.sub(r"\s", "", text))
    labels = [b.get("label") for b in blocks]
    stats = {
        "chars": chars,
        "lines": len([l for l in text.splitlines() if l.strip()]),
        "questions": len(_QUESTION_LINE.findall(text)),
        "formulas": sum((b.get("html") or "").count("<math") for b in blocks),
        "tables": labels.count("Table"),
        "pictures": sum(1 for l in labels if l in ("Picture", "Figure")),
        "languages": languages,
        "warnings": [],
    }
    if chars < 60:
        stats["warnings"].append(
            "Very little text - a picture, a blank page, a cover or an advert?")
    elif not stats["questions"]:
        stats["warnings"].append("No question numbers found on this page.")
    if stats["tables"]:
        stats["warnings"].append(
            f"{stats['tables']} table(s) left out of the text - add them to their "
            "questions as pictures.")
    return stats


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

    def read_blocks(self, image):
        return self.load()([image], full_page=True)[0].model_dump()["blocks"]

    def read_page(self, image, first_page=False):
        return page_text(self.read_blocks(image), image.height, first_page)

    def read_page_detail(self, image, first_page=False):
        blocks = self.read_blocks(image)
        text = page_text(blocks, image.height, first_page)
        return text, page_stats(text, blocks)


READER = Reader()


# ---------------------------------------------------------------- speed

# How long a page took on THIS computer, kept between runs so the time a
# paper will take can be shown before it is started.
SPEED_FILE = HERE / "speed.json"


def typical_page_seconds():
    import json
    try:
        recent = json.loads(SPEED_FILE.read_text(encoding="utf-8")).get("recent") or []
    except (OSError, ValueError):
        return None
    return round(sum(recent) / len(recent)) if recent else None


def _remember_speed(seconds):
    import json
    try:
        recent = json.loads(SPEED_FILE.read_text(encoding="utf-8")).get("recent") or []
    except (OSError, ValueError):
        recent = []
    recent = (recent + [round(seconds, 1)])[-10:]
    try:
        SPEED_FILE.write_text(json.dumps({"recent": recent}), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------- jobs

FILES = {}  # papers chosen on the page: id -> {path, filename, size, pages_total, thumbs}
JOBS = {}
LAST_JOB = {"id": None}
_BUSY = threading.Lock()  # one paper at a time - each page uses the whole CPU
THUMB_WIDTH = 180
VIEW_WIDTH = 900


def _log(job, message):
    job["log"].append({"t": time.time(), "msg": message})


def _duration(seconds):
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    return f"{seconds // 60} min {seconds % 60:02d} s"


def _summary(stats):
    bits = [f"{stats['chars']:,} characters", f"{stats['questions']} question number(s)"]
    if stats["formulas"]:
        bits.append(f"{stats['formulas']} formula(s)")
    if stats["languages"]:
        bits.append(" + ".join(stats["languages"]))
    return ", ".join(bits)


def new_job(file, pages):
    return {
        "id": uuid.uuid4().hex, "file_id": file["id"], "filename": file["filename"],
        "status": "queued", "error": None, "stop": False,
        "created": time.time(), "finished": None, "load_seconds": None,
        "file_pages": file["pages_total"],
        "typical": typical_page_seconds(),
        "pages": [{"n": n, "status": "waiting", "started": None, "seconds": None,
                   "text": "", "stats": None, "error": None} for n in pages],
        "log": [],
    }


def _run_job(job, path):
    try:
        job["status"] = "starting"
        _log(job, "Starting the reading model...")
        started = time.time()
        READER.load()
        job["load_seconds"] = round(time.time() - started, 1)
        _log(job, f"Reading model ready ({_duration(job['load_seconds'])}).")
        job["status"] = "reading"
        _, images = page_images(path, only={p["n"] for p in job["pages"]})
        for rec, image in zip(job["pages"], images):
            if job["stop"]:
                break
            rec["status"], rec["started"] = "reading", time.time()
            _log(job, f"Page {rec['n']}: reading...")
            try:
                rec["text"], rec["stats"] = READER.read_page_detail(image, first_page=rec["n"] == 1)
            except Exception as e:  # one bad page should not lose the others
                rec["status"], rec["error"] = "failed", f"{type(e).__name__}: {e}"
                rec["seconds"] = round(time.time() - rec["started"], 1)
                _log(job, f"Page {rec['n']}: could not be read - {rec['error']}")
                continue
            rec["seconds"] = round(time.time() - rec["started"], 1)
            rec["status"] = "done"
            _remember_speed(rec["seconds"])
            _log(job, f"Page {rec['n']}: done in {_duration(rec['seconds'])} - "
                      f"{_summary(rec['stats'])}.")
            for w in rec["stats"]["warnings"]:
                _log(job, f"Page {rec['n']}: check - {w}")
        skipped = [p for p in job["pages"] if p["status"] == "waiting"]
        for p in skipped:
            p["status"] = "skipped"
        if job["stop"]:
            job["status"] = "stopped"
            _log(job, f"Stopped - {len(skipped)} page(s) not read.")
        else:
            job["status"] = "done"
            done = [p for p in job["pages"] if p["status"] == "done"]
            _log(job, f"Finished - {len(done)} of {len(job['pages'])} page(s) read in "
                      f"{_duration(time.time() - job['created'])}.")
    except Exception as e:  # shown on the page - nothing else would see it
        job["status"] = "failed"
        job["error"] = f"{type(e).__name__}: {e}"
        _log(job, f"Failed - {job['error']}")
    finally:
        job["finished"] = time.time()
        _BUSY.release()


def job_view(job, now=None):
    now = time.time() if now is None else now
    pages = job["pages"]
    done = [p for p in pages if p["status"] == "done"]
    secs = [p["seconds"] for p in done]
    average = sum(secs) / len(secs) if secs else job.get("typical")
    remaining = None
    if average and job["status"] in ("queued", "starting", "reading"):
        waiting = sum(1 for p in pages if p["status"] == "waiting")
        current = [p for p in pages if p["status"] == "reading"]
        remaining = average * waiting
        if current:
            remaining += max(average - (now - current[0]["started"]), 0)
        remaining = round(remaining)
    return {
        "id": job["id"], "file_id": job.get("file_id"), "filename": job["filename"],
        "status": job["status"], "error": job["error"], "stopping": job.get("stop", False),
        "pages_total": len(pages), "file_pages": job.get("file_pages"),
        "pages_done": len(done), "page_seconds": secs,
        "average_seconds": round(average) if average else None,
        "estimate_is_measured": bool(secs),
        "load_seconds": job["load_seconds"], "seconds_left": remaining,
        "created": job.get("created"), "finished": job.get("finished"), "now": now,
        "pages": [{k: p[k] for k in ("n", "status", "started", "seconds", "text", "stats", "error")}
                  for p in pages],
        "log": job.get("log", []),
        "text": "\n".join(p["text"] for p in done if p["text"]),
    }


def _forget_files(keep=()):
    """Delete the copies of papers no longer needed - everything except
    `keep` and the paper being read."""
    reading = {JOBS[LAST_JOB["id"]]["file_id"]} if LAST_JOB["id"] and _BUSY.locked() else set()
    for fid in list(FILES):
        if fid in keep or fid in reading:
            continue
        try:
            os.unlink(FILES[fid]["path"])
        except OSError:
            pass
        del FILES[fid]


def _sweep_old_uploads(older_than=24 * 3600):
    """Copies left behind by a reader that was closed mid-paper. Only old
    ones: a second reader window may still be using a recent copy."""
    for leftover in HERE.glob("_upload_*"):
        try:
            if time.time() - leftover.stat().st_mtime > older_than:
                leftover.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- web

def create_app():
    import platform

    from fastapi import Body, FastAPI, File, HTTPException, UploadFile
    from fastapi.responses import HTMLResponse, PlainTextResponse, Response

    app = FastAPI(title="EdsiderAI local reader")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (HERE / "page.html").read_text(encoding="utf-8")

    @app.post("/files")
    def add_file(file: UploadFile = File(...)):
        """A paper chosen on the page: kept here until it is read, with a
        thumbnail of every page so adverts and blank pages can be left out."""
        suffix = Path(file.filename or "").suffix.lower()
        if suffix != ".pdf" and suffix not in IMAGE_TYPES:
            raise HTTPException(400, "Choose a PDF or a picture (PNG, JPG, TIFF, BMP).")
        _forget_files()
        fid = uuid.uuid4().hex
        path = HERE / f"_upload_{fid}{suffix}"
        data = file.file.read()
        path.write_bytes(data)
        try:
            count, _ = page_images(path, only=set())
            thumbs = [page_picture(path, n, THUMB_WIDTH) for n in range(1, count + 1)]
        except Exception as e:
            path.unlink()
            raise HTTPException(400, f"Could not open this file - {type(e).__name__}: {e}")
        FILES[fid] = {"id": fid, "path": path, "filename": file.filename, "size": len(data),
                      "pages_total": count, "thumbs": thumbs}
        return {"id": fid, "filename": file.filename, "size": len(data), "pages_total": count,
                "seconds_per_page": typical_page_seconds(),
                "computer": f"{platform.node()} - {os.cpu_count()} CPU threads"}

    @app.get("/files/{file_id}/pages/{number}")
    def picture(file_id: str, number: int, big: bool = False):
        f = FILES.get(file_id)
        if not f or not 1 <= number <= f["pages_total"]:
            raise HTTPException(404, "That page is no longer here - choose the paper again.")
        if not big:
            return Response(f["thumbs"][number - 1], media_type="image/png")
        if not f["path"].exists():
            raise HTTPException(404, "That paper is no longer here - choose it again.")
        return Response(page_picture(f["path"], number, VIEW_WIDTH), media_type="image/png")

    @app.post("/read")
    def read(file_id: str = Body(...), pages: list = Body(...)):
        f = FILES.get(file_id)
        if not f:
            raise HTTPException(404, "That paper is no longer here - choose it again.")
        chosen = sorted({int(n) for n in pages if 1 <= int(n) <= f["pages_total"]})
        if not chosen:
            raise HTTPException(400, "Tick at least one page to read.")
        if not _BUSY.acquire(blocking=False):
            raise HTTPException(409, "Already reading a paper - wait for it to finish, "
                                     "or stop it.")
        job = new_job(f, chosen)
        skipped = f["pages_total"] - len(chosen)
        _log(job, f"Received {f['filename']} ({f['size'] / 1e6:.1f} MB, "
                  f"{f['pages_total']} page(s)).")
        _log(job, f"Reading {len(chosen)} page(s)" +
                  (f" - {skipped} left out." if skipped else "."))
        JOBS[job["id"]] = job
        LAST_JOB["id"] = job["id"]
        threading.Thread(target=_run_job, args=(job, f["path"]), daemon=True).start()
        return {"id": job["id"]}

    @app.get("/jobs/current")
    def current():
        """The paper being read (or read last), so a reloaded page picks
        up where it was."""
        if not LAST_JOB["id"]:
            raise HTTPException(404, "Nothing has been read yet.")
        return job_view(JOBS[LAST_JOB["id"]])

    @app.get("/jobs/{job_id}")
    def status(job_id: str):
        if job_id not in JOBS:
            raise HTTPException(404, "No such job - the reader may have been restarted.")
        return job_view(JOBS[job_id])

    @app.post("/jobs/{job_id}/stop")
    def stop(job_id: str):
        """Stop after the page being read - a page cannot be cut short."""
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "No such job.")
        if job["status"] in ("queued", "starting", "reading") and not job["stop"]:
            job["stop"] = True
            _log(job, "Stop asked for - finishing the page being read, then stopping.")
        return job_view(job)

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
    p.add_argument("--port", type=int, default=PORT, help="another port, if this one is in use")
    args = p.parse_args()
    if args.time:
        time_first_pages(args.time, args.pages)
        return
    import uvicorn
    _sweep_old_uploads()
    url = f"http://127.0.0.1:{args.port}"
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"\nEdsiderAI reader running at {url} - keep this window open while you use it.\n")
    # 127.0.0.1 only: this page is for this computer, never the network.
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    sys.exit(main())
