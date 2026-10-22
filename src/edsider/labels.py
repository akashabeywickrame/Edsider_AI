"""The labels EdSider needs on a paper and we did not capture: medium,
term, year (read from the paper where it says so), exam category,
difficulty, source and author (chosen by data entry). Stored as EdSider's
own ids, from the lists scripts/sync.py copies (store.edsider_lookups()),
in intake_documents.edsider_labels.

What can be read from the paper was measured on every stored upload
before writing these rules (21 papers):
- Medium: the script the text is written in - Sinhala papers came out
  98-100% Sinhala letters, Tamil ones 86-92% Tamil, English ones 100%
  Latin. The scanned Chemistry papers have no text at all, but their file
  names carry EdSider's own medium code ("2025 Chemistry Part I SM.pdf").
- Term: "1st term", "First", "THIRD", "Term 01", "Term02", "2nd", "Second"
  and the Sinhala "1 වන වාරය" were all seen. Sinhala word forms ("පළමු
  වාරය") and Tamil terms were not, so they are not matched yet.
- Year: only from the file name, the title and the paper's first lines.
  Further in, years are question content - Communication & Media's MCQ
  option "E. 2012", a date in a Tamil GIT question.
"""

import re

# KEYS in the stored JSON - EdSider ids, each nullable.
KEYS = ("medium_id", "term_id", "year_id", "exam_category_id", "difficulty_id",
        "source_id", "author_id")

# Which EdSider list each key is chosen from (store.edsider_lookups kinds).
LIST_FOR = {"medium_id": "language", "term_id": "term", "year_id": "year",
            "exam_category_id": "exam_category", "difficulty_id": "difficulty",
            "source_id": "source", "author_id": "author"}

_SINHALA = re.compile(r"[඀-෿]")
_TAMIL = re.compile(r"[஀-௿]")
_LATIN = re.compile(r"[A-Za-z]")
# A script counts as the paper's medium at this share of its letters - an
# English paper quoting a Sinhala word, or a Tamil one with English code in
# it (the Tamil GIT paper is 14% Latin), stays in its own medium.
_SCRIPT_SHARE = 0.3
# EdSider's own medium codes as they turn up in file names.
_MEDIUM_TOKEN = re.compile(r"(?<![A-Za-z])(EM|SM|TM)(?![A-Za-z])")
_MEDIUM_WORDS = re.compile(r"\b(English|Sinhala|Tamil)\s+medium\b", re.I)

_ORDINAL = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3}
_TERM = re.compile(
    # Not \b: an underscore counts as a word character, and file names
    # write "Grade 12_Physics_1st term".
    r"(?<![A-Za-z0-9])(first|1st|second|2nd|third|3rd)\s*term"
    # No word boundary before "term": "General Information TechnologyTerm02".
    r"|term\s*[-:_]?\s*0?([1-3])(?!\d)"
    r"|(first|second|third)tt\b"          # "GIT_Gr12_FirstTT"
    r"|([1-3])\s*වන\s*වාරය",
    re.IGNORECASE)
_YEAR = re.compile(r"(?<!\d)(20[1-3]\d)(?!\d)")
# How much of the paper's text counts as its heading for the year.
_HEADING_CHARS = 300


def _by(lists, kind, field, value):
    for item in lists.get(kind, []):
        if str(item.get(field) or "").strip().lower() == str(value).strip().lower():
            return item["id"]
    return None


def detect_medium(raw_text, filename=None):
    """'EM' / 'SM' / 'TM' (EdSider's own codes), or None."""
    text = raw_text or ""
    sin, tam, lat = (len(p.findall(text)) for p in (_SINHALA, _TAMIL, _LATIN))
    letters = sin + tam + lat
    if letters:
        if sin / letters >= _SCRIPT_SHARE:
            return "SM"
        if tam / letters >= _SCRIPT_SHARE:
            return "TM"
    for source in (filename or "", text[:_HEADING_CHARS]):
        m = _MEDIUM_TOKEN.search(source)
        if m:
            return m.group(1)
        m = _MEDIUM_WORDS.search(source)
        if m:
            return {"english": "EM", "sinhala": "SM", "tamil": "TM"}[m.group(1).lower()]
    return "EM" if lat else None


def detect_term(raw_text, filename=None, title=None):
    """1, 2 or 3, or None - from the title, the file name, then the
    paper's heading."""
    for source in (title or "", filename or "", (raw_text or "")[:2000]):
        m = _TERM.search(source)
        if not m:
            continue
        word = m.group(1) or m.group(3)
        if word:
            return _ORDINAL[word.lower()]
        return int(m.group(2) or m.group(4))
    return None


def detect_year(raw_text, filename=None, title=None):
    """'2025', or None - never from inside the questions."""
    for source in (title or "", filename or "", (raw_text or "")[:_HEADING_CHARS]):
        m = _YEAR.search(source)
        if m:
            return m.group(1)
    return None


def suggest(raw_text, filename, title, lists):
    """{key: EdSider id} for what the paper itself says - medium, term,
    year - each only when it is both read from the paper and on EdSider's
    list."""
    out = {}
    medium = detect_medium(raw_text, filename)
    if medium:
        out["medium_id"] = _by(lists, "language", "short_code", medium)
    term = detect_term(raw_text, filename, title)
    if term:
        out["term_id"] = _by(lists, "term", "short_code", f"T{term}")
    year = detect_year(raw_text, filename, title)
    if year:
        out["year_id"] = _by(lists, "year", "name", year)
    return {k: v for k, v in out.items() if v is not None}


def clean(labels):
    """Only the known keys, each an int or None - what is stored."""
    out = {}
    for key in KEYS:
        value = (labels or {}).get(key)
        out[key] = int(value) if value not in (None, "") else None
    return out
