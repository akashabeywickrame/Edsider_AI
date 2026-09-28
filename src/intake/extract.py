"""Uploaded file -> list of draft questions.

Two layers here, and only the second one is testable without a real
file:

- `read_*`  pull raw text out of a PDF / Word doc / image. These lazily
            import their libraries, so importing this module never fails
            just because Tesseract or pdfplumber is not installed - the
            failure surfaces when that path is actually used, with an
            install hint.
- `segment` / `detect_paper_meta`  pure text rules, no dependencies -
            split a paper's text into questions and pull grade, subject
            and marks. This is where the heuristics live, and where the
            tests are.

None of this is trusted output. It is a first pass for a person to
correct, and the confidence flag on each draft says where to look first.
"""

import os
import re
import shutil
from io import BytesIO
from pathlib import Path


class ExtractionUnavailable(RuntimeError):
    """A format's reader library (or Tesseract itself) is not installed.

    Raised rather than returning empty text, so the caller can tell
    "nothing could be read" apart from "the paper genuinely had no
    text".
    """


# ------------------------------------------------------------ text rules

# A question starts at the left margin with its number, in any of the
# forms real papers actually use:
#
#   5.  5)  5 .  (5)  05.  Q5  Q.5  Question 5  Question 5:  Question (5)
#   5.What   (no space)          5.\n What   (number alone on its line)
#
# What must NOT match: "1.5" (a decimal), "In 1990." (a year in prose),
# an MCQ option "(1)" listed after a real question - the last one is
# left to segment()'s "numbers only go up" filter rather than the regex.
#
# "Question (06)" is the same bracketed numbering as "(01)", just with
# the word "Question" printed in front of it - measured on a real GIT
# A/L paper whose Part II numbers questions 1-5 as bare "(01)".."(05)"
# then switches to "Question (06)" and "Question (07)" for the last
# two, with no Part heading in between. Treating the word as decoration
# rather than a different scheme keeps all seven in one counting-up
# "paren" run instead of losing the last two, which otherwise get
# silently glued onto question 5's text.
_Q_START = re.compile(
    r"""^[ \t]*
    (?:
        (?:question|q)[ \t.:\-]*(?P<qn_word>\d{1,3})
      | (?:(?:question|q)[ \t.:\-]*)?\(\s*(?P<qn_paren>\d{1,3})\s*\)
      | (?P<qn_dot>\d{1,3})[ \t]*(?P<close>[.)])
    )
    (?![ \t]*marks?\b)
    (?=[\s:)(\[A-Za-z'"]|$)
    """,
    re.MULTILINE | re.IGNORECASE | re.VERBOSE,
)

# Marks are almost always parenthesised or bracketed and next to the
# word "marks". A bare number is never read as marks - "12 cm" and
# "in 1948" would both be caught. "[12]" alone is common enough on its
# own to include.
_MARKS_PATTERNS = [
    re.compile(r"\(\s*(\d{1,3})\s*mark[s]?\s*\)", re.IGNORECASE),
    re.compile(r"\(\s*(\d{1,3})\s*mks?\s*\)", re.IGNORECASE),
    re.compile(r"\[\s*(\d{1,3})\s*mark[s]?\s*\]", re.IGNORECASE),
    re.compile(r"\bmark[s]?\s*[:\-]?\s*(\d{1,3})\b", re.IGNORECASE),
    re.compile(r"\b(\d{1,3})\s*mark[s]?\b", re.IGNORECASE),
    re.compile(r"\[\s*(\d{1,3})\s*\]"),
]

# Any way a paper or its filename writes a grade: "Grade 12", "Grade -13",
# "Gr12", "G-13".
_GRADE = re.compile(
    r"\b(?:grade|gr|g)[ \t]*[-:.–—]?[ \t]*(\d{1,2})\b", re.IGNORECASE
)

# Subjects under the bank's own names. The review page builds its subject
# list from the bank, so a detected name the bank does not use - this
# module used to say "Combined Mathematics", the bank says "Combined
# Maths" - matched nothing and the subject showed up blank. Each is found
# by the spellings papers and filenames actually use, acronyms included.
# "Bio System Technolgy" is the bank's own spelling, kept so a match lands
# on it.
_SUBJECT_ALIASES = [
    ("General Information Technology",
     [r"general\s+information\s+technology", r"(?-i:GIT)"]),
    ("ICT", [r"information\s+(?:and|&)\s+communication\s+technology",
             r"(?-i:ICT)"]),
    ("Combined Maths", [r"combined\s+math(?:ematic)?s", r"c\s*\.?\s*maths"]),
    ("Bio System Technolgy", [r"bio\s*-?\s*systems?\s+technology"]),
    ("Engineering Technology", [r"engineering\s+technology"]),
    ("Logic and Scientific Method", [r"logic\s+and\s+scientific\s+method"]),
    ("Communication and Media",
     [r"communication\s+and\s+media(?:\s+studies)?"]),
    ("Business Statistics", [r"business\s+statistics"]),
    ("Business Studies", [r"business\s+studies"]),
    ("Political Science", [r"political\s+science"]),
    ("Home Economics", [r"home\s+economics"]),
    ("English Literature", [r"english\s+literature"]),
    # "English Medium" on a Science paper is the language it is written
    # in, not its subject.
    ("General English",
     [r"general\s+english", r"english(?!\s+(?:language\s+)?medium)"]),
    ("Sinhala Language", [r"sinhala(?:\s+language)?(?!\s+medium)"]),
    ("Tamil Language", [r"tamil(?:\s+language)?(?!\s+medium)"]),
    ("Maths", [r"mathematics", r"maths?"]),
    ("Science", [r"science"]),
    ("Biology", [r"biology"]),
    ("Chemistry", [r"chemistry"]),
    ("Physics", [r"physics"]),
    ("History", [r"history"]),
    ("Geography", [r"geography"]),
    ("Economics", [r"economics"]),
    ("Accounting", [r"accounting"]),
    ("Agriculture",
     [r"agricultur(?:e|al)(?:\s+(?:and\s+food\s+)?(?:science|technology))?"]),
    ("Islam", [r"islam"]),
]
_SUBJECT_PATTERNS = [
    (name, re.compile(r"\b(?:" + "|".join(aliases) + r")\b", re.IGNORECASE))
    for name, aliases in _SUBJECT_ALIASES
]
KNOWN_SUBJECTS = {name for name, _ in _SUBJECT_ALIASES}

_TITLE = re.compile(
    r"\b(?:(?P<nth>first|second|third|1st|2nd|3rd)[ \t]*"
    r"(?P<kind>term[ \t]+(?:test|exam(?:ination)?)|tt)"
    r"|term[ \t]*[-:–—][ \t]*(?P<nth2>first|second|third|1st|2nd|3rd)"
    r"|(?P<other>(?:model|past|revision|practice)[ \t]+papers?"
    r"|(?:mid[- ]?year|final|annual|unit|monthly)[ \t]+(?:test|exam(?:ination)?)))\b",
    re.IGNORECASE,
)

# How much of the top of a paper to search for grade, subject and title -
# they live in the header, and scanning the whole paper only adds false
# hits ("the economics of..." three pages in).
_HEADER_CHARS = 900


def _grade_from(text):
    for m in _GRADE.finditer(text):
        g = int(m.group(1))
        if 1 <= g <= 13:
            return f"G{g:02d}"
    return None


def _find_subject(text):
    """The subject named earliest in `text`, the longer name winning a
    tie - "Political Science" over "Science", "Combined Maths" over
    "Maths"."""
    best = None
    for name, pat in _SUBJECT_PATTERNS:
        m = pat.search(text)
        if m:
            rank = (m.start(), -(m.end() - m.start()))
            if best is None or rank < best[0]:
                best = (rank, name)
    return best[1] if best else None


def _title_from(text):
    m = _TITLE.search(text)
    if not m:
        return None
    if m.group("nth"):
        kind = m.group("kind")
        phrase = f"{m.group('nth')} {'term test' if kind.lower() == 'tt' else kind}"
    elif m.group("nth2"):
        phrase = f"{m.group('nth2')} term"
    else:
        phrase = m.group("other")
    return " ".join(w if w[0].isdigit() else w.capitalize()
                    for w in phrase.split())


def detect_paper_meta(text, filename=None):
    """Grade, subject and title from a paper's header, then from its
    filename for whatever the header does not say. Any may be None.

    Grade comes back in the bank's own form ("G12"), subject under the
    bank's own name, so both can be copied straight onto a question.

    Paper-level, not per-question: the reviewer sets it once and every
    draft question inherits it, then overrides the odd one that differs.
    """
    header = text[:_HEADER_CHARS]
    # "GIT_Gr12_FirstTT" - underscores count as word characters, so none
    # of the \b-anchored patterns would see the words inside it.
    name = re.sub(r"[_\-.,]+", " ", Path(filename).stem) if filename else ""
    return {
        "grade": _grade_from(header) or _grade_from(name),
        "subject": _find_subject(header) or _find_subject(name),
        "title": _title_from(header) or _title_from(name),
    }


def is_grade_code(value):
    return bool(re.fullmatch(r"G\d{2}", value or ""))


def grade_label(code):
    """"G12" -> "Grade 12", for people. Anything else comes back as-is."""
    m = re.fullmatch(r"G(\d{1,2})", code or "")
    return f"Grade {int(m.group(1))}" if m else (code or "")


def _find_marks(body):
    """The last marks annotation in a question body, 1-100, or None.

    Last rather than first: a multi-part question lists part marks
    ("(a) ... (2 marks) (b) ... (3 marks)") and then, more often than
    not, a total - and the total is the useful one.
    """
    hits = []
    for pat in _MARKS_PATTERNS:
        for m in pat.finditer(body):
            value = int(m.group(1))
            if 1 <= value <= 100:
                hits.append((m.start(), value))
    if not hits:
        return None
    return max(hits, key=lambda h: h[0])[1]


def _confidence(text, marks):
    """'low' flags a draft that most needs a human eye first.

    Deliberately NOT triggered by missing marks. A real MCQ paper prints
    no per-question marks at all - flagging that as low confidence put a
    warning on all 50 questions of a real Grade 13 paper and told the
    reviewer nothing. Short text is the signal that actually means the
    extractor struggled.
    """
    return "low" if len(text.strip()) < 15 else "ok"


# Some uploads are not clean question papers at all - a worked-solutions
# or marking-scheme document numbers its SOLUTION STEPS ("1. Initial
# Velocity Components", "2. Finding the Vertical Velocity"), which look
# exactly like question numbers to any regex. Measured on a real Grade 13
# Combined Maths upload: number-splitting found 5 "questions", all of them
# solution steps, while the real questions were the lines saying
# "Question". No rule can tell those apart, so the reviewer picks the
# style instead of the extractor guessing.
SPLIT_STYLES = {
    "number": "Numbered at the left margin  (1.  2.  01.  Q1  (1))",
    "question_word": "Lines containing the word 'Question'",
}
DEFAULT_SPLIT_STYLE = "number"

_QUESTION_WORD_START = re.compile(
    r"^[ \t]*(?:\d{1,3}[ \t]*[.)])?[ \t]*question\b[ \t.:\-]*(?P<qn>\d{1,3})?",
    re.MULTILINE | re.IGNORECASE,
)


def _number_starts(text, start=0, end=None):
    """(position, number, printed_form) for the question numbers in
    text[start:end] - in ONE numbering format.

    A part of a paper numbers its questions one way and what sits inside
    a question another. The real Grade 12 GIT paper numbers its MCQs "1."
    with options "(1)"-"(4)", and its structured questions "(01)" with
    parts "(1)", "(2)". Counting any left-margin number as a question
    turned options into questions in Part I and parts into questions in
    Part II. So each format - bracket style, and zero-padded or not - is
    run on its own, and the format that behaves most like question
    numbers wins: real ones count up, while options and parts restart at
    1 under every question. Each restart counts against its format, so
    four options under each of two questions lose to the two questions.
    """
    end = len(text) if end is None else end
    found = []
    for m in _Q_START.finditer(text, start, end):
        if m.group("qn_word"):
            raw, kind = m.group("qn_word"), "word"
        elif m.group("qn_paren"):
            raw, kind = m.group("qn_paren"), "paren"
        else:
            raw, kind = m.group("qn_dot"), m.group("close")
        found.append((m.start(), int(raw), raw, kind))

    best, best_score = [], None
    for kind in dict.fromkeys(f[3] for f in found):
        for padded in (False, True):
            run, dropped, last_n = [], 0, 0
            for pos, n, raw, k in found:
                if k != kind:
                    continue
                # "10" onwards is the same in both, so it counts for both.
                if n < 10 and (len(raw) > 1 and raw[0] == "0") != padded:
                    continue
                # A genuine question number only ever goes up. A "(20)"
                # that is really a mark total is handled in the regex.
                if run and n <= last_n:
                    dropped += 1
                    continue
                run.append((pos, n, raw))
                last_n = n
            # A format that numbers only one thing is not a numbering at
            # all - on the real Combined Maths paper, whose worked
            # solutions restart "1. 2. 3." inside questions numbered the
            # same way, penalties alone let a single stray number win.
            score = (len(run) >= 2, len(run) - dropped)
            if run and (best_score is None or score > best_score):
                best, best_score = run, score
    return best


def _question_word_starts(text, start=0, end=None):
    end = len(text) if end is None else end
    return [
        (m.start(), i + 1, m.group("qn") or str(i + 1))
        for i, m in enumerate(_QUESTION_WORD_START.finditer(text, start, end))
    ]


# ------------------------------------------------------- sub-questions
#
# A structured question is a stem plus its parts, and each part is what a
# teacher actually marks and answers:
#
#   (01) (1) A school library uses a computerized system ...
#        (a) Define the term "system" as used in computing. [2]
#        (b) Draw a fully labeled block diagram ...          [5]
#
# Read as one draft that is a single box with a single answer, which is
# neither how it is marked nor how EdSider stores it - their bank holds a
# parent row with the shared stimulus and one child row per part (see
# CLAUDE.md, "A question is not always a row"). So a question is split
# into a stem and its parts, recursively, and each part carries its own
# marks and its own answer.

#
# A bare number at the left margin ("1." "2.") is NOT a part marker: it is
# the paper's own question numbering, and on a worked-solutions document
# it is the numbered solution steps - the ambiguity SPLIT_STYLES exists
# for. Digits count only in brackets, "(1) (2)", which is how the real
# Grade 12 GIT paper numbers the parts inside its structured questions.
_SUB_MARKER = re.compile(
    r"^[ \t]*(?:\((?P<btag>[ivxIVX]{1,4}|[a-zA-Z]|\d{1,2})\)"
    r"|(?P<tag>[ivxIVX]{1,4}|[a-zA-Z])(?P<close>[.)]))"
    r"[ \t]*(?=\S)",
    re.MULTILINE,
)

_ROMAN_SEQUENCE = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"]

# How deep to go. Three levels covers every real paper seen - question,
# its parts, and the parts' own sub-parts ("(d) ... (i) (ii) (iii)").
MAX_SUB_DEPTH = 2

# A part shorter than this, with no marks of its own, is an MCQ option or
# a list item rather than a question to be answered separately. Measured
# against the real papers: the shortest real part is "(a) Define the term
# 'system' as used in computing. [2]" at 48 characters; MCQ options on
# the same paper run 3 to 30.
_MIN_PART_CHARS = 35


def _tag_kind(tag):
    """Whether a marker reads as a number, a roman numeral or a letter.

    "i" is both a letter and roman one, and nothing in the marker itself
    settles it - the sequence does, which is why this returns both
    readings and _sub_starts() tries each."""
    if tag.isdigit():
        return ["digit"]
    lower = tag.lower()
    kinds = []
    if lower in _ROMAN_SEQUENCE:
        kinds.append("roman")
    if len(tag) == 1 and tag.isalpha():
        kinds.append("alpha")
    return kinds


def _expected(kind, index):
    """The marker a counting-up sequence of this kind has at `index`."""
    if kind == "digit":
        return str(index + 1)
    if kind == "roman":
        return _ROMAN_SEQUENCE[index] if index < len(_ROMAN_SEQUENCE) else None
    return chr(ord("a") + index) if index < 26 else None


def _sub_starts(body):
    """(position, end of marker, printed marker) for one question's parts.

    Every style is tried on its own - bracketed or not, and read as
    letters, roman numerals or numbers - and the style that counts up the
    furthest wins, the same reasoning as _number_starts(): a real part
    list counts a, b, c while anything else (a stray "e.g.", a number
    inside the text) does not continue.
    """
    found = []
    for m in _SUB_MARKER.finditer(body):
        tag = m.group("btag") or m.group("tag")
        bracketed = m.group("btag") is not None
        style = (bracketed, m.group("close") or ")")
        found.append((m.start(), m.end(), tag, style))

    best, best_score = [], None
    styles = {s for _, _, _, s in found}
    for style in styles:
        for kind in ("alpha", "roman", "digit"):
            run, index = [], 0
            for pos, end, tag, this_style in found:
                if this_style != style or kind not in _tag_kind(tag):
                    continue
                if tag.lower() != _expected(kind, index):
                    continue
                run.append((pos, end, tag))
                index += 1
            # One marker is not a list - a lone "(a)" inside a sentence
            # must not turn a question into two.
            if len(run) < 2:
                continue
            # The OUTER list is the one that starts first, not the one
            # with the most entries: the real Grade 12 GIT question (01)
            # is "(1) ... (a)(b)(c)(d) ... (2) ... (a)(b)", where the
            # inner letters outnumber the outer numbers 6 to 2. Picking
            # the longer run split it at the letters and hung "(2)"'s own
            # parts under "(d)". Length only breaks a tie.
            score = (-run[0][0], len(run))
            if best_score is None or score > best_score:
                best, best_score = run, score
    return best


def _split_parts(body, depth=0):
    """A question body -> (stem, [part, ...]), each part the same shape.

    The stem is what comes before the first part - the shared scenario or
    diagram reference every part depends on. Parts nest: a part with its
    own "(i) (ii)" list is split again, down to MAX_SUB_DEPTH.

    Returns ("the whole body", []) when there is nothing to split, so a
    plain question is unaffected.
    """
    if depth >= MAX_SUB_DEPTH:
        return body.strip(), []

    starts = _sub_starts(body)
    if not starts:
        return body.strip(), []

    pieces = []
    for i, (pos, end, tag) in enumerate(starts):
        stop = starts[i + 1][0] if i + 1 < len(starts) else len(body)
        pieces.append((tag, body[end:stop].strip()))

    # An MCQ's options and a list of choices look exactly like a part
    # list. What tells them apart is that a part is a question in its own
    # right: it has marks of its own, or enough text to be answered.
    real = [p for _, p in pieces
            if len(p) >= _MIN_PART_CHARS or _find_marks(p)]
    if len(real) < 2:
        return body.strip(), []

    parts = []
    for tag, piece in pieces:
        inner_stem, inner_parts = _split_parts(piece, depth + 1)
        parts.append({"tag": tag, "text": inner_stem, "marks": _find_marks(piece),
                      "parts": inner_parts})
    return body[:starts[0][0]].strip(), parts


# ------------------------------------------------------ paper structure
#
# A real upload is rarely one flat run of questions. The real Grade 12 GIT
# first-term paper is a header, "General Information Technology I" (40
# MCQs), "General Information Technology II" (five structured questions),
# then a MARKING SCHEME for both. Read as one list it came back as 54
# "questions": Part II's parts split apart, and the answer key and worked
# answers split into questions of their own.

_TO_ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V"}
KIND_LABELS = {"mcq": "Multiple choice", "structured": "Structured / essay"}

_PART_LINE = re.compile(
    r"^(?P<word>part|section|paper)[ \t]*[-:.]?[ \t]*"
    r"(?P<tag>\d{1,2}|iv|v|i{1,3}|[a-e])\b(?P<rest>.*)$",
    re.IGNORECASE,
)
# "Part II- A", "Part II – B" - sub-parts, as on the real Grade 13
# Biosystem Technology paper.
_SUB_TAG = re.compile(r"^[ \t]*[-–—][ \t]*(?P<sub>[a-d])\b(?P<rest>.*)$",
                      re.IGNORECASE)
# "General Information Technology II" - the subject name and a roman
# numeral is how the GIT paper titles each part. Only a heading when the
# words before the numeral name a subject, so a line of a history
# question ending "World War II" is not one.
_ROMAN_TITLE = re.compile(
    r"^(?P<title>[A-Za-z][A-Za-z&(),.\- ]{2,70}?)[ \t]+(?P<tag>III|II|IV|I)$"
)
_KIND_LINE = re.compile(
    r"^(?:multiple[ \t]+choice(?:[ \t]+questions?)?|mcqs?"
    r"|structured(?:[ \t]+essay)?[ \t]+questions?"
    r"|essay(?:[ \t]+type)?(?:[ \t]+questions?)?)$",
    re.IGNORECASE,
)
# Everything from one of these on is answers, not questions. A bare
# "Answer" is deliberately not here: the real Combined Maths paper labels
# each worked solution "Answer", inside its question.
_ANSWERS_LINE = re.compile(
    r"^(?:marking[ \t]+scheme|answer[ \t]+(?:key|scheme|guide)"
    r"|model[ \t]+answers|answers)\b",
    re.IGNORECASE,
)
_MCQ_HINT = re.compile(
    r"multiple[ \t]+choice|\bmcqs?\b|pick one of the alternatives"
    r"|choose the (?:correct|most appropriate)|select the correct"
    r"|underline the correct",
    re.IGNORECASE,
)
_STRUCTURED_HINT = re.compile(r"structured|essay", re.IGNORECASE)
_MARKS_EACH = re.compile(
    r"(?:each|every)[ \t]+question[ \t]+carries[ \t]+(\d{1,3})[ \t]+marks?",
    re.IGNORECASE,
)
# An MCQ option - "(1) RAM", "A. Keyboard" - and not a lettered part of a
# structured question, which says what to do: the real Biosystem
# Technology paper's Part II-B is all "a) Explain ... b) Describe ...",
# and read as options the whole part was labelled multiple choice.
_OPTION_LINE = re.compile(
    r"^[ \t]*(?:\([ \t]*(?:[1-5]|[a-eA-E])[ \t]*\)|[a-eA-E][.)])[ \t]+"
    r"(?!(?:explain|describe|state|name|draw|write|calculate|define|list"
    r"|give|identify|compare|discuss|show|find|prove|outline|mention"
    r"|briefly|what|why|how|which|who|when|where|convert|complete"
    r"|evaluate|suggest|classify|distinguish|justify|analy[sz]e|determine"
    r"|derive|sketch|label)\b)\S",
    re.MULTILINE | re.IGNORECASE,
)
# "Q1: (2)" - one entry of an MCQ answer key. "Question (01)" at the start
# of a line - one structured question's block in a marking scheme.
_MCQ_KEY = re.compile(
    r"\bQ(?:uestion)?[ \t]*\.?[ \t]*(\d{1,3})[ \t]*[:.)=\-][ \t]*"
    r"\([ \t]*([1-5A-Ea-e])[ \t]*\)",
    re.IGNORECASE,
)
_ANSWER_BLOCK = re.compile(
    r"^[ \t]*question[ \t]*\(?[ \t]*(\d{1,3})[ \t]*\)?(?![ \t]*[:.=\-][ \t]*\()",
    re.MULTILINE | re.IGNORECASE,
)


# A marking scheme labels each part's answer the way the paper labels the
# part - "(1)(a) Definition of a System [2 marks]" under "Question (01)".
# One to three bracketed tags, nothing else on the label but a title, so
# an answer line that merely opens with "(2 marks - 1 for ...)" is not one.
_ANSWER_PART = re.compile(
    r"^[ \t]*(?P<tags>(?:\([ \t]*[0-9A-Za-z]{1,4}[ \t]*\)){1,3})"
    r"(?![ \t]*[0-9]*[ \t]*marks?\b)(?P<rest>.*)$",
    re.MULTILINE,
)


def _split_answer_by_part(body):
    """One question's worked answer -> (what belongs to the question,
    {"(1)(a)": that part's answer}).

    A structured question is marked part by part, and now that each part
    is its own draft with its own answer box, its answer belongs in that
    box rather than in one wall of text on the question. Anything before
    the first part label stays on the question itself.
    """
    labels = list(_ANSWER_PART.finditer(body))
    if not labels:
        return body.strip(), {}

    parts = {}
    for i, m in enumerate(labels):
        stop = labels[i + 1].start() if i + 1 < len(labels) else len(body)
        tags = re.sub(r"[ \t]", "", m.group("tags"))
        text = (m.group("rest") + body[m.end():stop]).strip()
        parts.setdefault(tags, text)
    return body[:labels[0].start()].strip(), parts


def _kind_hint(text):
    if _MCQ_HINT.search(text):
        return "mcq"
    if _STRUCTURED_HINT.search(text):
        return "structured"
    return None


def _kind_from(bodies):
    """MCQ or structured, from the questions themselves when no heading
    or instruction says: MCQs list options and carry no marks each;
    structured questions carry marks."""
    bodies = list(bodies)
    if not bodies:
        return None
    options = sum(1 for b in bodies
                  if len(_OPTION_LINE.findall(b)) >= 3 and _find_marks(b) is None)
    marked = sum(1 for b in bodies if _find_marks(b) is not None)
    if options * 2 > len(bodies):
        return "mcq"
    if marked * 2 > len(bodies):
        return "structured"
    return None


def _heading(line):
    """{kind, label, heading} for a line that starts a part of the paper,
    or its answers - else None."""
    s = line.strip()
    if not s or len(s) > 90 or "|" in s:
        return None
    if _ANSWERS_LINE.match(s) and len(s) <= 60:
        return {"kind": "answers", "label": None, "heading": s}
    m = _PART_LINE.match(s)
    if m:
        tag = m.group("tag")
        tag = str(int(tag)) if tag.isdigit() else tag.upper()
        rest = m.group("rest")
        sub = _SUB_TAG.match(rest)
        if sub:
            tag += "-" + sub.group("sub").upper()
            rest = sub.group("rest")
        lead = rest.lstrip()
        # "Part 1 of the experiment uses a lens." is a sentence.
        if len(lead) <= 60 and (not lead or lead[0] in "-–—:(.["
                                or lead[0].isupper()):
            return {"kind": _kind_hint(s),
                    "label": f"{m.group('word').title()} {tag}", "heading": s}
        return None
    m = _ROMAN_TITLE.match(s)
    if m and _find_subject(m.group("title")):
        return {"kind": _kind_hint(s), "label": f"Part {m.group('tag')}",
                "heading": s}
    if _KIND_LINE.match(s):
        return {"kind": _kind_hint(s), "label": None, "heading": s}
    return None


def _fold(earlier, later):
    """A heading with nothing under it but instructions ("Part II" on one
    line, "Essay questions" on the next) belongs to the part after it."""
    if earlier is None:
        return later
    merged = dict(later)
    merged["label"] = later["label"] or earlier["label"]
    merged["heading"] = " - ".join(
        h for h in (earlier["heading"], later["heading"]) if h)
    merged["kind"] = later["kind"] or earlier["kind"]
    merged["intro_start"] = earlier["intro_start"]
    merged["preamble"] = False
    return merged


def _attach_answers(answers, drafts, sections):
    """Fill `answer` from the paper's own marking scheme - only where the
    scheme names its question unambiguously, and only into a part of the
    matching kind. Everything else is left empty for the reviewer."""
    def number(d):
        try:
            return int(d["question_number"])
        except (TypeError, ValueError):
            return None

    mcq = [s["label"] for s in sections if s["kind"] == "mcq"]
    other = [s["label"] for s in sections if s["kind"] != "mcq"]

    key = {}
    for m in _MCQ_KEY.finditer(answers):
        key.setdefault(int(m.group(1)), f"({m.group(2)})")
    if key and len(mcq) == 1:
        for d in drafts:
            if d["section"] == mcq[0] and number(d) in key:
                d["answer"] = key[number(d)]

    blocks = list(_ANSWER_BLOCK.finditer(answers))
    if blocks and len(other) == 1:
        worked = {}
        for i, m in enumerate(blocks):
            stop = blocks[i + 1].start() if i + 1 < len(blocks) else len(answers)
            block = answers[m.start():stop]
            # Drop the block's own "Question (01) [15 marks]" line.
            body = block.split("\n", 1)[1].strip() if "\n" in block else ""
            worked.setdefault(int(m.group(1)), body)
        under = {}
        for d in drafts:
            if d.get("parent_seq"):
                under.setdefault(d["parent_seq"], []).append(d)

        def descendants(d):
            out = []
            for child in under.get(d["seq"], []):
                out.append(child)
                out.extend(descendants(child))
            return out

        for d in drafts:
            if d["section"] != other[0] or not worked.get(number(d)):
                continue
            head, per_part = _split_answer_by_part(worked[number(d)])
            kids = descendants(d)
            if not (kids and per_part):
                d["answer"] = worked[number(d)]
                continue
            # The scheme labels a part "(1)(a)"; the draft's own number is
            # the question's printed number plus that same suffix.
            d["answer"] = head or None
            printed = d["question_number"] or ""
            for kid in kids:
                suffix = (kid["question_number"] or "")[len(printed):]
                if per_part.get(suffix):
                    kid["answer"] = per_part[suffix]


def _emit_parts(drafts, parts, parent_seq, prefix, section, depth=1):
    """Add one question's parts to `drafts`, in reading order.

    Stored flat - one draft per part, each naming its parent's seq -
    rather than nested inside the parent, because that is the shape the
    review page renders, the shape the duplicate check reads, and the
    shape EdSider's own bank uses (a parent row plus child rows).
    """
    for part in parts:
        number = f"{prefix}({part['tag']})" if prefix else f"({part['tag']})"
        seq = len(drafts) + 1
        drafts.append({
            "seq": seq,
            "question_number": number,
            "text": part["text"],
            "marks": part["marks"],
            "section": section,
            "answer": None,
            "depth": depth,
            "parent_seq": parent_seq,
            "extraction_confidence": _confidence(part["text"], part["marks"]),
        })
        _emit_parts(drafts, part["parts"], seq, number, section, depth + 1)


def structure(text, style=DEFAULT_SPLIT_STYLE):
    """A paper's full text -> {"sections": [...], "questions": [...]}.

    `style` is a key of SPLIT_STYLES - the reviewer's choice of what
    marks the start of a question in THIS document, because a numbered
    list inside a worked answer is indistinguishable from a numbered
    question.

    Each question: seq (1-based, across the whole paper), question_number
    (as printed, "31", "01"), text, marks, section (its part's label, or
    None for a paper in one piece), answer (from the paper's own marking
    scheme, or None), extraction_confidence.

    Each section: label ("Part I"), heading (as printed), kind ("mcq",
    "structured" or None), instructions, marks_each, questions (a count).

    If nothing splits, returns a single draft holding the whole text at
    low confidence - so the reviewer always has something to split by
    hand rather than an empty screen.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    heads, answers_at, pos = [], None, 0
    for line in text.split("\n"):
        h = _heading(line)
        if h and h["kind"] == "answers":
            answers_at = pos
            break
        # A part title repeated at the top of every page is still the
        # same part.
        if h and not (h["label"] and heads and heads[-1]["label"] == h["label"]):
            h.update(start=pos, body=pos + len(line) + 1)
            heads.append(h)
        pos += len(line) + 1
    stop = len(text) if answers_at is None else answers_at

    spans = [{"label": None, "heading": None, "kind": None,
              "start": 0, "body": 0, "preamble": True}]
    spans += [dict(h, preamble=False) for h in heads]
    for i, sp in enumerate(spans):
        sp["end"] = spans[i + 1]["start"] if i + 1 < len(spans) else stop
        sp["intro_start"] = sp["body"]
        if sp["body"] >= sp["end"]:
            sp["starts"] = []
        elif style == "question_word":
            sp["starts"] = _question_word_starts(text, sp["body"], sp["end"])
        else:
            sp["starts"] = _number_starts(text, sp["body"], sp["end"])

    sections, pending = [], None
    for sp in spans:
        if not sp["starts"]:
            if sp["preamble"]:
                continue  # the paper's own header
            if len(text[sp["body"]:sp["end"]].strip()) < 300:
                pending = _fold(pending, sp)
                continue
            # A part with real text in it but nothing that splits is kept
            # whole, rather than silently dropped.
        if pending:
            sp = _fold(pending, sp)
            pending = None
        sections.append(sp)

    if not sections:
        body = text.strip()
        return {"sections": [], "questions": [{
            "seq": 1,
            "question_number": None,
            "text": body or "(no text could be extracted)",
            "marks": None,
            "section": None,
            "answer": None,
            "depth": 0,
            "parent_seq": None,
            "extraction_confidence": "low",
        }]}

    explicit = [sp["label"] for sp in sections if sp["label"]]
    if explicit or len(sections) > 1:
        roman = any(re.search(r" (?:I{1,3}|IV|V)(?:-[A-D])?$", lab)
                    for lab in explicit)
        for n, sp in enumerate(sections, 1):
            if not sp["label"]:
                sp["label"] = f"Part {_TO_ROMAN.get(n, n) if roman else n}"

    drafts, parts = [], []
    marker = _QUESTION_WORD_START if style == "question_word" else _Q_START
    for sp in sections:
        first = sp["starts"][0][0] if sp["starts"] else sp["end"]
        intro = text[sp["intro_start"]:first].strip()
        each = _MARKS_EACH.search(intro)
        each = int(each.group(1)) if each else None

        pieces = []
        for i, (pos, _n, raw) in enumerate(sp["starts"]):
            end = (sp["starts"][i + 1][0] if i + 1 < len(sp["starts"])
                   else sp["end"])
            body = text[pos:end].strip()
            # Drop the leading "5." / "Q5" / "(5)" / "Question 5:" - it
            # is the printed marker, captured separately as
            # question_number, not part of the question. Leaving it in
            # breaks exact-match de-duplication ("1. Define X" and
            # "7. Define X" hash differently) and makes the numbers guard
            # fire on the question numbers rather than on anything asked.
            lead = marker.match(body)
            if lead:
                body = body[lead.end():].strip()
            pieces.append((raw, body))
        if not pieces:
            pieces.append((None, text[sp["body"]:sp["end"]].strip()))

        kind = sp["kind"] or _kind_hint(intro) or _kind_from(b for _, b in pieces)
        for raw, body in pieces:
            # "Each question carries 15 marks" is the question's mark; the
            # last "[2]" inside it is only its final part's.
            marks = each or _find_marks(body)
            # An MCQ is never split: its options are a list at the left
            # margin that looks exactly like a part list, and an option is
            # not a question with an answer of its own.
            stem, parts_tree = ((body, []) if kind == "mcq"
                                else _split_parts(body))
            parent_seq = len(drafts) + 1
            drafts.append({
                "seq": parent_seq,
                "question_number": raw,
                "text": stem,
                "marks": marks,
                "section": sp["label"],
                "answer": None,
                "depth": 0,
                "parent_seq": None,
                # A question that was split holds the shared stem, which
                # is legitimately short (sometimes empty) - that is not
                # the extractor struggling, so it is not low confidence.
                "extraction_confidence": (
                    "ok" if parts_tree
                    else (_confidence(body, marks) if raw is not None else "low")),
            })
            _emit_parts(drafts, parts_tree, parent_seq, raw or "", sp["label"])
        parts.append({
            "label": sp["label"],
            "heading": sp["heading"],
            "kind": kind,
            "instructions": None if sp["preamble"] else (intro or None),
            "marks_each": each,
            "questions": len(pieces),
        })

    if answers_at is not None:
        _attach_answers(text[answers_at:], drafts, parts)
    return {"sections": parts, "questions": drafts}


def segment(text, style=DEFAULT_SPLIT_STYLE):
    """structure()'s questions alone, in paper order - for callers that
    do not need to know which part each came from."""
    return structure(text, style)["questions"]


# ------------------------------------------------------------------- OCR

# A paper is written in one language, so OCR runs in that one rather
# than all three at once - eng+sin+tam together is slower on every page
# and lets a smudge on an English paper be "read" as a Sinhala letter.
OCR_LANGS = {"eng": "English", "sin": "Sinhala", "tam": "Tamil"}
DEFAULT_OCR_LANG = "eng"

# Tesseract's accuracy falls off below ~200 dpi - subscripts and the
# "(5 marks)" annotations are the first thing to go.
_OCR_DPI = 300

# The usual Windows installer (the UB-Mannheim build) does not add
# tesseract.exe to PATH, so "installed" and "on PATH" are different
# things; without this a teacher who did install it still gets told
# Tesseract is missing.
_TESSERACT_LOCATIONS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
]

_TESSERACT_HINT = (
    "the Tesseract program is not installed - on Windows "
    "`winget install UB-Mannheim.TesseractOCR`, or set TESSERACT_CMD "
    "to the full path of tesseract.exe"
)


def _pytesseract():
    try:
        import pytesseract
    except ImportError as e:
        raise ExtractionUnavailable(
            "pytesseract is not installed - `pip install pytesseract`, "
            "then install the Tesseract program itself"
        ) from e

    cmd = os.environ.get("TESSERACT_CMD")
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    elif shutil.which(pytesseract.pytesseract.tesseract_cmd) is None:
        for candidate in _TESSERACT_LOCATIONS:
            if os.path.isfile(candidate):
                pytesseract.pytesseract.tesseract_cmd = candidate
                break
    return pytesseract


def _prepare_for_ocr(image):
    """Undo the things that make Tesseract misread a perfectly legible
    picture, without "enhancing" anything - its own binarisation beats a
    fixed threshold on uneven phone-camera lighting."""
    from PIL import Image, ImageOps

    # Phone photos are stored sideways with a rotation tag; Tesseract
    # reads the pixels, not the tag.
    image = ImageOps.exif_transpose(image)

    # A PNG screenshot pasted into Word is often transparent. Converted
    # straight to greyscale, transparent becomes black - black text on a
    # black page.
    if image.mode in ("RGBA", "LA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        image = Image.alpha_composite(white, rgba)
    image = image.convert("L")

    # A small screenshot puts text at ~10px tall; Tesseract wants
    # 20-30px. Upscaling adds no detail but it does stop letters merging.
    if image.width < 1000:
        scale = min(4.0, 1000 / image.width)
        image = image.resize(
            (round(image.width * scale), round(image.height * scale)),
            Image.LANCZOS,
        )
    return image


def ocr_image(source, lang=DEFAULT_OCR_LANG):
    """Text from an image via Tesseract. `source` is a path, a file-like
    object, or an already-open PIL image (a rendered PDF page).

    pytesseract is a thin wrapper - it still needs the Tesseract program,
    which pip cannot provide, plus the language data for `lang`. All of
    those failures land here as ExtractionUnavailable with a fix.
    """
    pytesseract = _pytesseract()
    try:
        from PIL import Image
    except ImportError as e:
        raise ExtractionUnavailable(
            "Pillow is not installed - `pip install Pillow`"
        ) from e

    image = source if isinstance(source, Image.Image) else Image.open(source)
    try:
        text = pytesseract.image_to_string(_prepare_for_ocr(image), lang=lang)
    except pytesseract.TesseractNotFoundError as e:
        raise ExtractionUnavailable(_TESSERACT_HINT) from e
    except pytesseract.TesseractError as e:
        if "load" in str(e).lower():
            raise ExtractionUnavailable(
                f"Tesseract has no {OCR_LANGS.get(lang, lang)} language data "
                f"- add {lang}.traineddata to Tesseract's tessdata folder"
            ) from e
        raise
    return text.strip()


# ------------------------------------------------------------------- PDF

# pdfplumber cannot map some ligature glyphs back to letters and prints
# "(cid:N)" instead. 415 is the "ti" ligature - measured on a real upload,
# which read "mathema(cid:415)cs" and "ques(cid:415)ons". Only glyphs
# actually seen are mapped; any other unmapped glyph sends the page to OCR.
_LIGATURES = {"(cid:415)": "ti"}
_UNMAPPED_GLYPH = re.compile(r"\(cid:\d+\)")

# Under this many non-space characters the text layer is not the page.
# Measured on real uploads: the Grade 13 model papers carry only their
# page number as text ("9") on pages that are otherwise one big picture
# of the questions, and the Grade 12 term papers carry nothing at all.
_SPARSE_PAGE = 20

# An embedded picture smaller than this (PDF points, 72 per inch) is a
# logo, bullet or rule rather than question content - not worth OCR.
_MIN_PICTURE_W = 72
_MIN_PICTURE_H = 12


def _fix_ligatures(text):
    for glyph, letters in _LIGATURES.items():
        text = text.replace(glyph, letters)
    return text


def _pdf_page_route(text, pictures):
    """How to read one PDF page:

    'text'    the text layer is the whole page - no OCR
    'page'    the text layer is missing or garbled - render the page
              and OCR all of it (Tesseract also handles the reading
              order of a two-column layout better than stitching would)
    'pictures' the text layer is real but the page also holds pictures -
              keep the exact text, OCR only the pictures into it

    The old rule was "OCR only if the WHOLE document has under 20
    characters", which meant a paper with a typed cover page and every
    question pasted in as a picture was never OCR'd at all.
    """
    if len(re.sub(r"\s", "", text)) < _SPARSE_PAGE or _UNMAPPED_GLYPH.search(text):
        return "page"
    return "pictures" if pictures else "text"


def _pictures_on(page):
    """Bounding boxes of the pictures on a page worth OCR-ing, clipped
    to the page so a picture bleeding off the edge can still be cropped."""
    px0, ptop, px1, pbottom = page.bbox
    boxes = []
    for im in page.images:
        box = (max(im["x0"], px0), max(im["top"], ptop),
               min(im["x1"], px1), min(im["bottom"], pbottom))
        if box[2] - box[0] >= _MIN_PICTURE_W and box[3] - box[1] >= _MIN_PICTURE_H:
            boxes.append(box)
    return _merge_touching(boxes)


def _merge_touching(boxes):
    """Join pictures that are really one picture cut in two.

    Word's PDF export stores a tall pasted picture as separate halves
    that meet exactly - on the real Grade 13 Applied paper, the halves of
    the river question meet at 222.1pt, same column to the tenth of a
    point. A line of text that falls on the seam is split across both
    halves and OCR reads it as noise in each: "Let the boat head upstream
    at an angle θ to the bank" scored 60% character error read per half,
    1.9% read whole. Its condensed-font Part 2 question was the same
    story, 27% versus 0.0% - the "distortion" was never the problem, the
    seam was.
    """
    merged = []
    for box in sorted(boxes, key=lambda b: (b[1], b[0])):
        for i, m in enumerate(merged):
            same_column = abs(box[0] - m[0]) <= 2 and abs(box[2] - m[2]) <= 2
            touching = box[1] <= m[3] + 2 and box[3] >= m[1] - 2
            if same_column and touching:
                merged[i] = (min(m[0], box[0]), min(m[1], box[1]),
                             max(m[2], box[2]), max(m[3], box[3]))
                break
        else:
            merged.append(box)
    return merged


def _text_with_pictures_read(page, boxes, ocr):
    """The text layer, with each picture's text slotted in at the height
    it sits on the page - so a question pasted in as a screenshot lands
    between the question above it and the one below, not at the end
    where segment() would glue it onto the last question."""
    blocks = [(line["top"], _fix_ligatures(line["text"]))
              for line in page.extract_text_lines()]
    for box in boxes:
        text = ocr(page.crop(box).to_image(resolution=_OCR_DPI).original)
        if text.strip():
            blocks.append((box[1], text.strip()))
    blocks.sort(key=lambda b: b[0])
    return "\n".join(t for _, t in blocks)


def read_pdf(path, ocr):
    """(text, note). Each page is read the cheapest way that actually
    captures it - see _pdf_page_route. `ocr` is image -> text.

    If OCR is unavailable, the pages that needed it keep whatever text
    layer they had and the note says how many were not read - one
    missing program should not throw away the pages that did have text.
    """
    try:
        import pdfplumber
    except ImportError as e:
        raise ExtractionUnavailable(
            "pdfplumber is not installed - `pip install pdfplumber`"
        ) from e

    parts = []
    read_by_ocr = not_read = came_back_empty = 0
    reason = None
    with pdfplumber.open(path) as pdf:
        total = len(pdf.pages)
        for page in pdf.pages:
            text = _fix_ligatures(page.extract_text() or "")
            boxes = _pictures_on(page)
            route = _pdf_page_route(text, boxes)
            if route != "text" and reason is None:
                try:
                    if route == "page":
                        text = ocr(page.to_image(resolution=_OCR_DPI).original)
                        # OCR "succeeding" with nothing is its own failure.
                        # Measured on the real Grade 12 term paper - phone
                        # photos of handwritten answer sheets - Tesseract
                        # returned 0 characters, and without this the page
                        # just looked like a short paper with no warning.
                        if len(re.sub(r"\s", "", text)) < _SPARSE_PAGE:
                            came_back_empty += 1
                    else:
                        text = _text_with_pictures_read(page, boxes, ocr)
                    read_by_ocr += 1
                except ExtractionUnavailable as e:
                    reason = str(e)
            if route != "text" and reason is not None:
                not_read += 1
            parts.append(text)

    note = "pdf-text"
    if read_by_ocr:
        note += f" + ocr ({read_by_ocr} of {total} pages)"
    if came_back_empty:
        note += (f" - {came_back_empty} of {total} pages not read: OCR found "
                 f"no text (handwriting or a poor photo, which Tesseract "
                 f"cannot read - or a blank page)")
    if not_read:
        note += f" - {not_read} of {total} pages not read: {reason}"
    return "\n".join(parts).strip(), note


# ------------------------------------------------------------------ Word

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_V = "{urn:schemas-microsoft-com:vml}"
_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"

_ATOM = re.compile(r"\u221a?[\w.]+")


def _group(s):
    """Bracket a sub-expression only when leaving it bare would change
    its meaning: 1/2 and x^2 stay as they are, 1/(x+1) does not."""
    s = s.strip()
    return s if len(s) <= 1 or _ATOM.fullmatch(s) else f"({s})"


def _omml_val(props, name, default):
    if props is None:
        return default
    found = props.find(_M + name)
    if found is None:
        return default
    return found.get(_M + "val", default)


def _omml(el):
    """Word's equation XML (OMML) -> one line of plain text.

    python-docx's paragraph.text skips equations entirely. Measured on
    the real Grade 13 Combined Maths papers (127 and 214 equations each):
    "80 x 1/sqrt2 = 40 sqrt2" came out as "80 x  = 40 sqrt2", and
    "V = sqrt(vx^2 + vy^2)" as just "V = ". Linear form - a/b, x^2, v_x,
    sqrt(...) - is what a teacher would type into a plain text box, so the
    draft stays readable and can still be hashed and embedded.
    """
    tag = el.tag if isinstance(el.tag, str) else ""
    if tag in (_M + "t", _W + "t"):
        return el.text or ""
    if not tag.startswith(_M):
        return "".join(_omml(c) for c in el)
    name = tag[len(_M):]
    if name.endswith("Pr"):
        return ""

    def part(child):
        found = el.find(_M + child)
        return _omml(found).strip() if found is not None else ""

    if name == "f":
        return f"{_group(part('num'))}/{_group(part('den'))}"
    if name == "sSup":
        return f"{part('e')}^{_group(part('sup'))}"
    if name == "sSub":
        return f"{part('e')}_{_group(part('sub'))}"
    if name == "sSubSup":
        return f"{part('e')}_{_group(part('sub'))}^{_group(part('sup'))}"
    if name == "rad":
        degree = part("deg")
        root = f"\u221a{_group(part('e'))}"
        return f"root[{degree}]{_group(part('e'))}" if degree else root
    if name == "d":
        props = el.find(_M + "dPr")
        sep = _omml_val(props, "sepChr", "|")
        inner = sep.join(_omml(e).strip() for e in el.findall(_M + "e"))
        return (_omml_val(props, "begChr", "(") + inner
                + _omml_val(props, "endChr", ")"))
    if name == "nary":
        symbol = _omml_val(el.find(_M + "naryPr"), "chr", "\u222b")
        if part("sub"):
            symbol += "_" + _group(part("sub"))
        if part("sup"):
            symbol += "^" + _group(part("sup"))
        return f"{symbol} {part('e')}"
    if name == "func":
        return f"{part('fName')} {part('e')}"
    if name == "limLow":
        return f"{part('e')}_{_group(part('lim'))}"
    if name == "limUpp":
        return f"{part('e')}^{_group(part('lim'))}"
    if name == "eqArr":
        return "; ".join(_omml(e).strip() for e in el.findall(_M + "e"))
    if name == "m":
        rows = [", ".join(_omml(e).strip() for e in row.findall(_M + "e"))
                for row in el.findall(_M + "mr")]
        return "[" + "; ".join(rows) + "]"
    return "".join(_omml(c) for c in el)


def _docx_walk(el, out, picture):
    """Body XML -> text, in document order. python-docx's own
    `document.paragraphs` skips tables, equations and pictures - on a
    maths paper, that is most of the content."""
    tag = el.tag if isinstance(el.tag, str) else ""
    if tag == _M + "oMath":
        out.append(_omml(el))
        return
    if tag == _W + "t":
        out.append(el.text or "")
        return
    if tag == _W + "tab":
        out.append("\t")
        return
    if tag in (_W + "br", _W + "cr"):
        out.append("\n")
        return
    if tag == _A + "blip":
        out.append(picture(el.get(_R + "embed")))
        return
    if tag == _V + "imagedata":
        out.append(picture(el.get(_R + "id")))
        return
    if tag == _MC + "AlternateContent":
        # Word writes a modern and a legacy copy of the same drawing or
        # text box side by side; reading both would double it.
        choice = el.find(_MC + "Choice")
        if choice is not None:
            _docx_walk(choice, out, picture)
        return
    if tag == _W + "tr":
        # One line per row: an MCQ options grid becomes
        # "A. Colombo | B. Kandy", which segment() leaves attached to
        # the question above it.
        cells = []
        for tc in el.findall(_W + "tc"):
            sub = []
            _docx_walk(tc, sub, picture)
            cells.append(" ".join("".join(sub).split()))
        out.append(" | ".join(c for c in cells if c) + "\n")
        return
    for child in el:
        _docx_walk(child, out, picture)
    if tag == _W + "p":
        out.append("\n")


def read_docx(path, ocr):
    """(text, note). Paragraphs, tables and equations in document order,
    with each embedded picture OCR'd in place - a question pasted into
    Word as a screenshot is otherwise invisible."""
    try:
        import docx
    except ImportError as e:
        raise ExtractionUnavailable(
            "python-docx is not installed - `pip install python-docx`"
        ) from e

    document = docx.Document(str(path))
    read_by_ocr = not_read = 0
    reason = None

    def picture(rel_id):
        nonlocal read_by_ocr, not_read, reason
        part = document.part.related_parts.get(rel_id)
        if part is None:
            return ""
        if reason is None:
            try:
                text = ocr(BytesIO(part.blob)).strip()
                read_by_ocr += 1
                # A picture with no words in it is a diagram the question
                # refers to - keep a marker so the reviewer knows one was
                # there.
                return f"\n{text}\n" if text else "\n[image]\n"
            except ExtractionUnavailable as e:
                reason = str(e)
            except OSError:
                # A format Pillow cannot open (EMF/WMF vector drawings).
                return "\n[image]\n"
        not_read += 1
        return "\n[image - not read]\n"

    out = []
    _docx_walk(document.element.body, out, picture)
    text = re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()

    note = "docx-text"
    if read_by_ocr:
        note += f" + ocr ({read_by_ocr} image{'s' if read_by_ocr != 1 else ''})"
    if not_read:
        note += f" - {not_read} image(s) not read: {reason}"
    return text, note


# --------------------------------------------------------------- extract

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}


def was_incomplete(extractor):
    """True when part of an uploaded file could not be read, so the
    review page can say so instead of letting a half-read paper look
    like a short one."""
    return (extractor.startswith(("unavailable", "failed", "unsupported"))
            or "not read" in extractor)


def extract(path, style=DEFAULT_SPLIT_STYLE, lang=DEFAULT_OCR_LANG, ocr=None,
            filename=None):
    """Uploaded file -> {file_type, extractor, raw_text,
    paper: {grade, subject, title}, sections: [part, ...],
    questions: [draft, ...]} - see structure() for parts and drafts.

    `filename` is the name the file was uploaded under, for whatever the
    header does not say - the app reads a temporary copy whose own name
    says nothing.

    `raw_text` is kept so the document can be re-split later - with a
    different `style`, or simply by a newer extractor - without asking
    for the file again. Re-uploading to pick up an extractor fix is
    exactly the friction this avoids.

    `lang` is the paper's language, for OCR. `ocr` (image -> text)
    replaces Tesseract entirely - tests pass a fake, the same way the
    duplicate check takes an injected embedder.

    Never raises - a missing OCR/reader library, or a corrupt file,
    comes back as a single low-confidence draft and an `extractor` that
    says what happened, so the reviewer can key the questions in by hand
    rather than hitting an error.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if ocr is None:
        def ocr(image):
            return ocr_image(image, lang=lang)

    text = ""
    extractor = "none"
    try:
        if suffix == ".pdf":
            text, extractor = read_pdf(path, ocr)
        elif suffix == ".docx":
            text, extractor = read_docx(path, ocr)
        elif suffix == ".doc":
            # The pre-2007 binary format; python-docx cannot open it.
            extractor = "unsupported:.doc (open it in Word and save as .docx)"
        elif suffix in _IMAGE_SUFFIXES:
            text = ocr(path)
            extractor = "ocr"
        else:
            extractor = f"unsupported:{suffix or 'none'}"
    except ExtractionUnavailable as e:
        extractor = f"unavailable ({e})"
        text = ""
    except Exception as e:
        # A corrupt or password-protected file should not take the whole
        # page down with it.
        extractor = f"failed ({type(e).__name__}: {e})"
        text = ""

    file_type = (
        "pdf" if suffix == ".pdf"
        else "docx" if suffix in (".docx", ".doc")
        else "image" if suffix in _IMAGE_SUFFIXES
        else "other"
    )

    meta = detect_paper_meta(text, filename=filename or path.name)
    parsed = structure(text, style=style)
    for q in parsed["questions"]:
        q.setdefault("grade", meta["grade"])
        q.setdefault("subject", meta["subject"])

    return {
        "file_type": file_type,
        "extractor": extractor,
        "raw_text": text,
        "paper": meta,
        "sections": parsed["sections"],
        "questions": parsed["questions"],
    }
