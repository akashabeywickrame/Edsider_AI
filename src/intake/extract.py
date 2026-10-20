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
import unicodedata
from io import BytesIO
from pathlib import Path

from .ocr_html import html_to_lines, rows_are_questions as _rows_are_questions


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
#
# A bare "(12)" is too - but only at the very end of a line, never
# mid-sentence. A real Grade 12 Sinhala-medium Business Statistics paper
# marks every part this way, with no "marks" word at all (the paper is
# entirely in Sinhala) and no square brackets either: "...(4)\n(ii) ...
# (4)\n". Anchoring to end-of-line is what keeps this from colliding
# with the same "12 cm" / "in 1948" problem the bare-number rule above
# was written to avoid - an in-sentence parenthetical reference number
# essentially never sits at the exact end of its line.
_MARKS_PATTERNS = [
    re.compile(r"\(\s*(\d{1,3})\s*mark[s]?\s*\)", re.IGNORECASE),
    re.compile(r"\(\s*(\d{1,3})\s*mks?\s*\)", re.IGNORECASE),
    re.compile(r"\[\s*(\d{1,3})\s*mark[s]?\s*\]", re.IGNORECASE),
    re.compile(r"\bmark[s]?\s*[:\-]?\s*(\d{1,3})\b", re.IGNORECASE),
    re.compile(r"\b(\d{1,3})\s*mark[s]?\b", re.IGNORECASE),
    re.compile(r"\[\s*(\d{1,3})\s*\]"),
    re.compile(r"\(\s*(\d{1,3})\s*\)[ \t]*$", re.MULTILINE),
    # Sinhala: "(ලකුණු 60 යි)" - "60 marks" - after every part of a real
    # A/L Chemistry paper. OCR also read one as "(ලකුණ 60 යි)", the final
    # vowel sign lost, so it is optional.
    re.compile(r"\(\s*ලකුණු?\s*(\d{1,3})\s*(?:යි|ක්)?\s*\)"),
]
# Patterns that name the word "marks" next to the number. Only these may
# read a value over 100 - an A/L structured question carries 150 - since
# with the word written there is no "12 cm" / "in 1948" to mistake.
_WORDED_MARKS = {0, 1, 2, 3, 4, 7}

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


def _marks_matches(body):
    """Every (start, end, value) _MARKS_PATTERNS finds in body, already
    parsed and range-checked - the shared search _find_marks() and
    _strip_marks_annotation() both build on, so they can never disagree
    about which match is "the" one."""
    hits = []
    for n, pat in enumerate(_MARKS_PATTERNS):
        top = 300 if n in _WORDED_MARKS else 100
        for m in pat.finditer(body):
            value = int(m.group(1))
            if 1 <= value <= top:
                hits.append((m.start(), m.end(), value))
    return hits


def _find_marks(body):
    """The last marks annotation in a question body, 1-100, or None.

    Last rather than first: a multi-part question lists part marks
    ("(a) ... (2 marks) (b) ... (3 marks)") and then, more often than
    not, a total - and the total is the useful one.
    """
    hits = _marks_matches(body)
    if not hits:
        return None
    return max(hits, key=lambda h: h[0])[2]


def _strip_marks_annotation(text):
    """Remove the one marks annotation _find_marks() would read out of
    this text - not every number anywhere in it that merely looks like
    one, which risks deleting content that has nothing to do with a
    score. Meant to run only after that value has already been read
    into its own `marks` field, so nothing is lost - it just moves out
    of the prose and into the structured column where it belongs.

    This matters once a question or its answer is compared against what
    a student actually wrote: a student never writes "[3 marks]" as
    part of their own answer, so leaving it embedded in the stored
    expected answer would make an automated comparison score every
    student's real answer against text they could never have matched.
    """
    if not text:
        return text
    hits = _marks_matches(text)
    if not hits:
        return text
    # Different _MARKS_PATTERNS can match overlapping substrings of the
    # SAME annotation - "[2 marks]" as a whole, and "2 marks" bare
    # inside it - so hits are merged into their full span before
    # picking which one to remove; without this, only the narrower,
    # nested match gets stripped and the brackets it sat inside are
    # left behind ("Definition of a System []").
    spans = []
    for start, end, _value in sorted(hits):
        if spans and start <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], end))
        else:
            spans.append((start, end))
    start, end = spans[-1]
    stripped = text[:start] + text[end:]
    # Removing the annotation can leave the line it sat on with a
    # trailing space, or a leading one on the line after it.
    stripped = re.sub(r"[ \t]+\n", "\n", stripped)
    stripped = re.sub(r"\n[ \t]+", "\n", stripped)
    stripped = re.sub(r"[ \t]{2,}", " ", stripped)
    return stripped.strip()


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
    # With a number ("Question 7", "Question (06)"), that number is all
    # that needs to follow. With none, "question" must be the last thing
    # on its own line (real Grade 13 Combined Maths worked solutions:
    # a bare "Question" heading, numbered questions nowhere in that
    # document at all) - not a mid-sentence, lowercased "question" a
    # line wrap happened to land at the start of a line (a real Grade 12
    # General English paper: "...two sentences /\nquestion in each
    # blank.", where "in each blank" on the same line is what gives it
    # away as prose, not a heading).
    r"^[ \t]*(?:\d{1,3}[ \t]*[.)])?[ \t]*question\b[ \t.:\-]*"
    r"(?:(?P<qn>\d{1,3})|(?=[ \t]*$))",
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
                # Zero-padded questions ("01." "03." "04.") do not jump to a
                # "10." while a padded number in between is still to come:
                # on the real Accounting paper (Word), question 04's own
                # auto-numbered list of errors runs "1." to "10.", "10."
                # reads the same padded or not, and taking it lost the real
                # "06." after it. Only padded numbers count as "still to
                # come" - a list item is never zero-padded, so one inside a
                # later question cannot block a real question.
                if padded and run and n > last_n + 1 and any(
                        k2 == kind and p2 > pos and last_n < n2 < n and n2 < 10
                        and len(r2) > 1 and r2[0] == "0"
                        for p2, n2, r2, k2 in found):
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
    r"^[ \t]*(?:\((?P<btag>[ivxIVX]{1,4}|[a-zA-Z]|[අ-ඖ]|\d{1,2})\)"
    r"|(?P<tag>[ivxIVX]{1,4}|[a-zA-Z]|[අ-ඖ])(?P<close>[.)]))"
    # `$` as well as a following character: an option the reviewer has
    # just added and not yet typed into is "III." alone on its line. It
    # must still count as a marker - otherwise it is folded into the
    # option above it, which is how a new option was lost before.
    r"[ \t]*(?=\S|$)",
    re.MULTILINE,
)

# Same marker shapes as _SUB_MARKER, anchored to "after whitespace"
# instead of "start of a line" - a real Grade 12 Tamil physics paper
# prints a whole MCQ option list on ONE line, space-separated:
# "(i) X (ii) Y (iii) Z (iv) W (v) V", not one option per line like
# every English paper this was first built against. The line anchor
# made every option after the first invisible - only "(i)" ever sat at
# a line start, and one marker alone is not a list (see _sub_starts),
# so these questions never looked like MCQs at all and fell back to
# "descriptive". Used only as split_mcq_options()'s last resort, never
# by _split_parts(): a structured question's own parts rely on the line
# anchor to tell a real part from a parenthetical aside inside a
# sentence, and that distinction matters less here because the
# counting-up requirement (i, ii, iii... in order, not restarting) is
# already what tells a real option list from a stray "(a)" in prose.
_INLINE_SUB_MARKER = re.compile(
    r"(?:^|(?<=\s))(?:\((?P<btag>[ivxIVX]{1,4}|[a-zA-Z]|[අ-ඖ]|\d{1,2})\)"
    r"|(?P<tag>[ivxIVX]{1,4}|[a-zA-Z]|[අ-ඖ])(?P<close>[.)]))"
    r"[ \t]*(?=\S|$)",
    re.MULTILINE,
)

# Bare digits ("1." "2." or "(1)" "(2)") - used only by split_mcq_options()
# as its own last resort, never by _split_parts() or the markers above,
# which deliberately exclude a non-bracketed digit (see _SUB_MARKER's own
# comment: a worked-solution document's numbered SOLUTION STEPS must never
# be mistaken for real parts). An option list carries no such risk once it
# requires four or more items - see split_mcq_options()'s own comment.
_BARE_DIGIT_OPTION_MARKER = re.compile(
    r"(?:^|(?<=\s))(?:\((?P<btag>\d{1,2})\)|(?P<tag>\d{1,2})(?P<close>[.)]))"
    r"[ \t]*(?=\S|$)",
    re.MULTILINE,
)

_ROMAN_SEQUENCE = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"]

# A real Grade 12 Sinhala-medium Business Statistics paper letters a
# structured question's parts with Sinhala's own independent vowels -
# "අ) ... ආ) ... ඉ) ..." - the same role "(a) (b) (c)" plays on an
# English paper. This is their standard dictionary order, U+0D85-U+0D96
# contiguous in Unicode. Roman-numeral sub-parts under them ("(i) (ii)")
# are still printed in Latin script on this same real paper, unchanged.
_SINHALA_VOWELS = [
    "අ", "ආ", "ඇ", "ඈ", "ඉ", "ඊ",
    "උ", "ඌ", "ඍ", "ඎ", "ඏ", "ඐ",
    "එ", "ඒ", "ඓ", "ඔ", "ඕ", "ඖ",
]

# How deep to go. Three levels covers every real paper seen - question,
# its parts, and the parts' own sub-parts ("(d) ... (i) (ii) (iii)").
MAX_SUB_DEPTH = 2

# A part shorter than this, with no marks of its own, is an MCQ option or
# a list item rather than a question to be answered separately. Measured
# against the real papers: the shortest real part is "(a) Define the term
# 'system' as used in computing. [2]" at 48 characters; MCQ options on
# the same paper run 3 to 30.
_MIN_PART_CHARS = 35

# The word that tells _split_parts() a question's own list is choices to
# pick from, not further parts - the real Grade 12 General English
# paper's "Underline the option that best expresses the meaning..." and
# "Underline the most appropriate response...". "Choose"/"select" are
# the same instruction in other words but unmeasured on a real paper so
# far - not added until one is seen.
_UNDERLINE_WORD = re.compile(r"\bunderline\b", re.IGNORECASE)


def _tag_kind(tag):
    """Whether a marker reads as a number, a roman numeral, a Sinhala
    vowel or a Latin letter.

    "i" is both a letter and roman one, and nothing in the marker itself
    settles it - the sequence does, which is why this returns both
    readings and _sub_starts() tries each. A Sinhala vowel is also
    `str.isalpha()`, so it is checked first and kept out of "alpha" -
    `_expected("alpha", ...)` only ever produces a, b, c and would never
    match one anyway, but keeping the two kinds disjoint is clearer."""
    if tag.isdigit():
        return ["digit"]
    lower = tag.lower()
    kinds = []
    if lower in _ROMAN_SEQUENCE:
        kinds.append("roman")
    if tag in _SINHALA_VOWELS:
        kinds.append("sinhala")
    elif len(tag) == 1 and tag.isalpha():
        kinds.append("alpha")
    return kinds


def _expected(kind, index):
    """The marker a counting-up sequence of this kind has at `index`."""
    if kind == "digit":
        return str(index + 1)
    if kind == "roman":
        return _ROMAN_SEQUENCE[index] if index < len(_ROMAN_SEQUENCE) else None
    if kind == "sinhala":
        return _SINHALA_VOWELS[index] if index < len(_SINHALA_VOWELS) else None
    return chr(ord("a") + index) if index < 26 else None


def _sub_starts(body, prefer="first", allowed=None, marker=_SUB_MARKER):
    """(position, end of marker, printed marker) for one question's parts.

    Every style is tried on its own - bracketed or not, and read as
    letters, roman numerals or numbers - and the style that counts up the
    furthest wins, the same reasoning as _number_starts(): a real part
    list counts a, b, c while anything else (a stray "e.g.", a number
    inside the text) does not continue.

    `prefer` breaks a tie between two lists that both count up. "first"
    (the default, for structured parts) takes the one that starts earliest.
    "last" (for MCQ options) takes the one that starts latest - an MCQ's
    options always come after its stem, and a stem can hold its own
    lettered list of statements ("a. ... b. ... c. ...") before the
    options ("I. ... II. ..."). Taking the earliest mis-read those
    statements as the options.

    `marker` is which pattern counts as a marker at all - _SUB_MARKER
    (line-anchored) by default, or _INLINE_SUB_MARKER for a paper that
    prints a whole option list on one line (see its own comment).
    """
    found = []
    for m in marker.finditer(body):
        tag = m.group("btag") or m.group("tag")
        bracketed = m.group("btag") is not None
        style = (bracketed, m.group("close") or ")")
        if allowed is not None and not allowed(tag):
            continue
        found.append((m.start(), m.end(), tag, style))

    best, best_score = [], None
    styles = {s for _, _, _, s in found}
    for style in styles:
        for kind in ("alpha", "roman", "digit", "sinhala"):
            run, index = [], 0
            for pos, end, tag, this_style in found:
                if this_style != style or kind not in _tag_kind(tag):
                    continue
                if kind == "sinhala":
                    # Strictly increasing through the vowel table, not
                    # necessarily consecutive - the real Grade 12 Sinhala
                    # Business Statistics paper letters one question's
                    # parts "අ) ආ) ඉ)", skipping two vowels in between.
                    # Whether that is this paper's own inconsistency or a
                    # real, narrower convention (as English sometimes
                    # skips "I"/"O" in lettered lists) is not something to
                    # guess at from one paper - accepting any forward
                    # movement through the table, rather than demanding
                    # the very next vowel, handles both without needing
                    # to know which it is, while a vowel that does not
                    # move the sequence forward at all still can't start
                    # or extend one.
                    vowel_index = _SINHALA_VOWELS.index(tag)
                    if vowel_index < index:
                        continue
                    index = vowel_index + 1
                else:
                    if tag.lower() != _expected(kind, index):
                        continue
                    index += 1
                run.append((pos, end, tag))
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
            score = ((-run[0][0] if prefer == "first" else run[0][0]),
                     len(run))
            if best_score is None or score > best_score:
                best, best_score = run, score
    return best


# The "(.........)" printed at the end of a question to show where its
# mark or answer goes.
_BLANK_MARKER = re.compile(r"\(\s*(?:\.{3,}|…{2,})\s*\)\s*$")


def _strip_blank_marker(text):
    """The printed "(.........)" at a question's end - the space for its answer,
    not part of the question."""
    return _BLANK_MARKER.sub("", text).rstrip()


# A whole line of nothing but dots, underscores or ellipses is the paper's
# own ruled space for a handwritten answer - not something the question or
# part actually said. _strip_blank_marker above only catches one at the
# very end of a block; a descriptive-answer paper (Grade 13
# Biosystem Technology, Part II-A: four blank lines printed after every
# part) has them buried in the MIDDLE of a part's text too, between its
# instruction and the next part's marker. Left in, that clutter showed up
# in the reviewer's question box as literal dots - the review page already
# gives every part its own text box to type an answer into, so the paper's
# blank lines serve no purpose once extracted.
_BLANK_LINE = re.compile(r"^[ \t]*(?:\.{3,}|…{2,}|_{3,})[ \t]*$", re.MULTILINE)


def _strip_blank_lines(text):
    """Remove the paper's own printed answer-space lines and collapse the
    gaps they leave behind."""
    text = _BLANK_LINE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _split_parts(body, depth=0, mcq_hint=False):
    """A question body -> (stem, [part, ...]), each part the same shape.

    The stem is what comes before the first part - the shared scenario or
    diagram reference every part depends on. Parts nest: a part with its
    own "(i) (ii)" list is split again, down to MAX_SUB_DEPTH.

    `mcq_hint` carries down from the top call: the real Grade 12 General
    English paper's question 6 is "Underline the option that best
    expresses the meaning..." followed by five sentences, each with its
    own lettered (a)-(d) list of paraphrases to pick from. Read as parts,
    those four lettered items became four separate one-line "questions",
    each wanting its own answer box - wrong, since a student picks ONE
    of them, the same as any other MCQ. `mcq_hint`, set once from the
    instruction text no recursive call still has access to, says "this
    question's own wording already told us its lists are choices, not
    parts" - see the option-list guard below, which otherwise has no
    way to tell this shape apart from a genuine two-part question with
    neither part carrying marks.

    Returns ("the whole body", []) when there is nothing to split, so a
    plain question is unaffected.
    """
    if depth >= MAX_SUB_DEPTH:
        return _strip_blank_lines(body), []

    starts = _sub_starts(body)
    if not starts:
        return _strip_blank_lines(body), []

    if depth == 0:
        mcq_hint = bool(_UNDERLINE_WORD.search(body[:starts[0][0]]))

    # Built from the raw text, dots and all - the checks below (an all-blank
    # split, an option list, "is this long enough to be a real part") all
    # key off structure that only shows up in the unstripped text; blank
    # lines are removed only from what actually gets stored, at each
    # return point below.
    pieces = []
    for i, (pos, end, tag) in enumerate(starts):
        stop = starts[i + 1][0] if i + 1 < len(starts) else len(body)
        pieces.append((tag, body[end:stop].strip()))

    # "(1) ........ (2) ........" is one question with two lines to write on -
    # one answer box serves all of them, so nothing here is split into parts.
    # Checked on what is LEFT once each piece's own blank answer-lines are
    # stripped out, not on whether the raw piece merely ends in dots - a
    # real part's own text ends in exactly the same printed blank lines
    # (the Biosystem Part II-A case: "i) Mention..." followed by four
    # ruled lines to write on), and matching on the raw text collapsed
    # those real parts into this same "nothing but blank space" case.
    if all(not _strip_blank_lines(p) for _, p in pieces):
        return _strip_blank_lines(body), []

    # Four or more numbered items with no marks and nothing under any of them
    # are an option list, not parts - the shape of a multiple-choice question
    # numbered (1)-(5). Measured on the Sinhala Part A paper, where its section
    # heading does not read, so this is what keeps its options from becoming
    # sub-questions. A structured part has marks or parts of its own.
    if (len(pieces) >= 4 and all(tag.isdigit() for tag, _ in pieces)
            and not any(_find_marks(p) or _sub_starts(p) for _, p in pieces)):
        return _strip_blank_lines(body), []

    # A list under a question whose own instruction says "underline" (or
    # "choose", "select" - see _UNDERLINE_WORD) is the paper telling us
    # directly that what follows are choices to pick from, not further
    # parts - the signal the digit-only guard above has no way to give
    # for a LETTERED list. Still requires the same "nothing of its own"
    # shape a real part would have - marks, or a further nested list -
    # so a genuinely two-part "underline..." question (rare, unmeasured)
    # is not swept up by wording alone.
    if (mcq_hint and len(pieces) >= 2
            and not any(_find_marks(p) or _sub_starts(p) for _, p in pieces)):
        return _strip_blank_lines(body), []

    # An MCQ's options and a list of choices look exactly like a part
    # list. What tells them apart is that a part is a question in its own
    # right: it has marks of its own, or enough text to be answered.
    real = [p for _, p in pieces
            if len(p) >= _MIN_PART_CHARS or _find_marks(p)]
    if len(real) < 2:
        return _strip_blank_lines(body), []

    parts = []
    for tag, piece in pieces:
        # A marks line on its own after a part's last sub-part is the
        # part's total, not that sub-part's: the real A/L Chemistry paper
        # prints "(ලකුණු 60 යි)" once, under (a)(v), for all of (a).
        rest, total = _trailing_marks_line(piece)
        if total is not None:
            inner_stem, inner_parts = _split_parts(rest, depth + 1, mcq_hint)
            if inner_parts:
                parts.append({"tag": tag, "text": inner_stem, "marks": total,
                              "parts": inner_parts})
                continue
        inner_stem, inner_parts = _split_parts(piece, depth + 1, mcq_hint)
        parts.append({"tag": tag, "text": inner_stem,
                      "marks": _own_marks(piece, inner_stem, inner_parts),
                      "parts": inner_parts})
    return _strip_blank_lines(body[:starts[0][0]]), parts


def _trailing_marks_line(text):
    """(text without its last line, marks) when that last line is nothing
    but a marks annotation; (text, None) otherwise."""
    lines = text.rstrip().split("\n")
    last = lines[-1].strip()
    for start, end, value in _marks_matches(last):
        if start == 0 and end == len(last):
            return "\n".join(lines[:-1]).rstrip(), value
    return text, None


def _is_roman(tag):
    return bool(re.fullmatch(r"[ivxIVX]{1,4}", tag))


def split_mcq_options(body):
    """An MCQ question's body -> (stem, [{"label": tag, "marker": ...,
    "text": ...}, ...]).

    An MCQ is never split by structure() itself - its stem and options
    stay one piece of text, because the duplicate-check fingerprint needs
    them together (see CLAUDE.md, "MCQ options live inside content").
    This is a separate, presentation-only pass for a review page that
    wants to show each option as its own card: it reuses _sub_starts(),
    the same marker-detection _split_parts() already uses for structured
    question parts, since an option list is the same shape - whatever the
    paper numbers them with (A/B/C/D, 1/2/3/4, (a)/(b)/(c)...), counting
    up correctly and needing at least two real markers to count as a
    list at all. Unlike a part, an option is never split further and
    never gets its own marks or answer - just text and a printed label.

    `marker` is the exact printed text of the marker itself ("(1)", "I.",
    "A)" - whatever this paper actually used), not a reconstruction from
    `label` - a reviewer correcting option text and saving it back as one
    combined body again needs the paper's own punctuation preserved, not
    a guessed-at substitute in some other style.

    Returns (body, []) - not an error - when nothing looks like an
    option list, so a caller can fall back to showing the text as-is.
    """
    # A numbered list (1), 2., ...) is the answer options. Any other list in
    # the question - lettered statements A-E, roman points - is part of the
    # question's text, so it is only used when there is no numbered list at all
    # (Tamil papers number their options (i)-(v)).
    #
    # Each style is tried line-anchored first, then - only if that finds
    # nothing - with _INLINE_SUB_MARKER: a real Grade 12 Tamil physics
    # paper prints its whole option list on one line, space-separated
    # ("(i) X (ii) Y (iii) Z (iv) W (v) V"), where only the first marker
    # ever sits at a line start and one marker alone is never a list.
    starts = (_sub_starts(body, prefer="last", allowed=str.isdigit)
              or _sub_starts(body, prefer="last", allowed=str.isdigit,
                             marker=_INLINE_SUB_MARKER)
              or _sub_starts(body, prefer="last", allowed=_is_roman)
              or _sub_starts(body, prefer="last", allowed=_is_roman,
                             marker=_INLINE_SUB_MARKER))
    if not starts:
        # Bare, non-bracketed digits ("1. 2. 3."), deliberately excluded
        # everywhere above - digits only ever count bracketed, so a
        # worked-solution's own numbered SOLUTION STEPS are never mistaken
        # for real parts or options (see _split_parts()'s own comment). A
        # real Grade 12 Sinhala-medium Business Statistics paper numbers its
        # MCQ options exactly this way, with no brackets and no English
        # "marks"/option word anywhere in the (entirely Sinhala) paper to
        # tell them apart from numbered prose otherwise - only trusted with
        # four or more items, since the one real numbered-steps document
        # measured in this codebase (Combined Maths worked solutions) has
        # just two steps per question; a genuine MCQ's option count is far
        # more reliably 4-5 than a solution's step count is reliably short.
        # Tried before the lettered fallback below, not after - the same
        # paper's "combination" questions ("A. stmt1 B. stmt2 C. stmt3"
        # then "1. A only  2. A and B only...") would otherwise have their
        # lettered STATEMENTS mistaken for the options, the exact thing the
        # lettered fallback's own guard below exists to prevent for a
        # bracketed numbered key - this paper's key just isn't bracketed.
        bare = _sub_starts(body, prefer="last", marker=_BARE_DIGIT_OPTION_MARKER)
        if len(bare) >= 4:
            starts = bare
    # Lettered options (A. B. C. ...) are kept for papers that use them. A question
    # that also carries a numbered answer key - "(1) ABCD (2) ACDE" - is different:
    # its lettered lines are statements to judge, not options.
    if not starts and not re.search(r"\(\s*\d\s*\)", body):
        starts = (_sub_starts(body, prefer="last")
                 or _sub_starts(body, prefer="last", marker=_INLINE_SUB_MARKER))
    if not starts:
        return body.strip(), []
    options = []
    for i, (pos, end, tag) in enumerate(starts):
        stop = starts[i + 1][0] if i + 1 < len(starts) else len(body)
        options.append({"label": tag, "marker": body[pos:end],
                        "text": body[end:stop].strip()})
    # A genuine option is one coherent piece of real answer text - never
    # a fill-in-the-blank gap, and never several separate short items
    # with a blank line between THEM, inside what is supposedly one
    # option. The same "(N) ..." marker shape fits three real Grade 12
    # General English paper questions that are not MCQs at all:
    #   - "(1) ______ an important tournament... (2) ______ the entire
    #     field..." - a blank run right after the marker (see
    #     split_fill_blanks(), what these actually are).
    #   - "(1) Your classmate failed... \"Perhaps you ..................\"
    #     (2) You won...\"I ..............\"" - real prose first, but a
    #     blank run further into the same "option" (a one-sentence
    #     response question, each item wanting a different answer, not
    #     a choice among them).
    #   - "(1) reports\n\nspeech\noration\nstory\n\ndocumentation
    #     (2) origin\n\nnew ending\n..." - no blank run at all, but each
    #     "option" is really 4-5 separate unlabelled words stacked with
    #     blank lines between them (a vocabulary-in-context question,
    #     each numbered word its own item) - a real option is written as
    #     continuous text and never contains a blank line itself; the
    #     blank line always falls BETWEEN options, never inside one.
    if any(_BLANK_RUN.search(o["text"]) or "\n\n" in o["text"] for o in options):
        return body.strip(), []
    return body[:starts[0][0]].strip(), options


# A fill-in-the-blank gap - a run of underscores or dots standing in for a
# word the student writes in, INLINE with real words on both sides. Never
# a whole line by itself: that shape (the paper's own ruled space for a
# longer written answer) is what _strip_blank_lines() already removes
# elsewhere, and the two must not be confused - a blank here always sits
# inside a sentence someone is meant to complete, not after one.
#
# Matched UNCAPPED on purpose - a quantifier cap like {3,50} does not
# exclude a longer run, it only splits it into several <=50-char matches
# that would each still count on their own. The length check that
# actually excludes one happens in Python, in split_fill_blanks() below:
# a real Grade 12 General English paper's actual one-word blanks run
# 4-37 characters ("(1) ______", "(1) ........(handle)"), while its
# "From.......", "Subject......." and similar labelled writing-space
# lines - meant for a sentence or more, not one word, and already missed
# by _strip_blank_lines() because a label sits before the dots on the
# same line - run 103-159. No real blank measured anywhere near that
# length; the gap between the two is wide enough that the exact cutoff
# does not matter much, as long as it sits inside it.
_BLANK_RUN = re.compile(r"(?:_{3,}|\.{3,})")
_MAX_BLANK_CHARS = 50
# The number labelling a blank - found either immediately before it, in
# parentheses ("(1) ______", a real Grade 12 General English paper's
# preposition and verb-form exercises), or starting the line/sentence the
# blank sits inside ("1. The latest discoveries ______ (choices)", the
# same paper's word-choice exercises, numbered per sentence rather than
# per blank). Scanning back for the LAST match before the blank's own
# position handles both shapes without needing to tell them apart.
_BLANK_NUMBER = re.compile(r"\(\s*(\d{1,2})\s*\)|^[ \t]*(\d{1,2})[.)]", re.MULTILINE)
# A genuine blank always sits inside a sentence - a real word within a
# few characters on at least one side. The long writing-space lines on
# this same paper are not purely the horizontal-ellipsis character "…"
# throughout, as they visually look - a real Grade 12 General English
# paper mixes in short runs of literal periods here and there ("...…"),
# and one of those short runs, on its own, is indistinguishable by
# length alone from a real one-word blank. What tells them apart is
# what sits beside it: nothing but more dots, ellipses and blank lines
# beside a writing-space fragment, real letters beside an actual blank.
_WORD_NEARBY = re.compile(r"[A-Za-z]")
_BLANK_CONTEXT_CHARS = 10


def split_fill_blanks(body):
    """(body, [{"number": "1", "pos": int}, ...]) - body is unchanged
    (the whole paragraph IS the question, blanks and all; unlike an MCQ
    option, a blank is never pulled out of the text it lives in), the
    list is just where each numbered gap sits, for the review page to
    show one answer box per blank, positioned the same way split_mcq_
    options() positions each option.

    Needs at least 2 blanks to count - one is an ordinary short-answer
    question that happens to use an underline, not a list to show boxes
    for. Returns (body, []) when nothing qualifies.
    """
    blanks = []
    for m in _BLANK_RUN.finditer(body):
        if m.end() - m.start() > _MAX_BLANK_CHARS:
            continue
        before = body[max(0, m.start() - _BLANK_CONTEXT_CHARS):m.start()]
        after = body[m.end():m.end() + _BLANK_CONTEXT_CHARS]
        if not (_WORD_NEARBY.search(before) or _WORD_NEARBY.search(after)):
            continue
        number = None
        for nm in _BLANK_NUMBER.finditer(body, 0, m.start()):
            number = nm.group(1) or nm.group(2)
        blanks.append({"number": number or str(len(blanks) + 1), "pos": m.start()})
    if len(blanks) < 2:
        return body, []
    return body, blanks


# Up to 4 letters, not 2: a plain digit or single letter label never
# needs more, but a roman numeral does - "iii" and "viii" are real
# option labels (see _ROMAN_SEQUENCE above). A first version of this
# capped at 2 characters, which matched "ii" and "iv" but silently
# failed every longer roman numeral, leaving a real correct answer
# unmarked with no error to notice it by.
_MCQ_ANSWER_LABEL = re.compile(r"^\(?\s*([A-Za-z0-9]{1,4})\s*\)?$")


def mcq_correct_label(answer):
    """The bare option label ('2', 'a', 'iii') an already-extracted
    answer like "(2)", "A" or "(III)" names - matched against
    split_mcq_options()'s own option labels case-insensitively, since an
    answer key and the options themselves do not always agree on case.
    None when it doesn't look like a label at all - a free-text answer
    a data-entry person typed by hand, say.
    """
    if not answer:
        return None
    m = _MCQ_ANSWER_LABEL.match(answer.strip())
    return m.group(1).lower() if m else None


_DURATION = re.compile(
    r"(?:time|duration)\s*(?:allowed)?\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*"
    r"(hours?|hrs?|minutes?|mins?)\b",
    re.IGNORECASE,
)


_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                 "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_ANSWER_COUNT = re.compile(
    r"answer\s+(?:all|any)?\s*(\d+|[a-z]+)?\s*(?:of\s+(?:the\s+)?)?questions",
    re.IGNORECASE,
)
_ONE_MARK = re.compile(r"(?:carr(?:y|ies)|worth|each)\s+(?:one|1)\s+marks?", re.IGNORECASE)


def _answer_count(instructions, question_count):
    """How many of a part's questions a student answers. "Answer all 40
    questions" and "Answer all questions" mean every question; "Answer FOUR
    questions only" means four. None when the instructions say nothing."""
    m = _ANSWER_COUNT.search(instructions or "")
    if not m:
        return None
    word = (m.group(1) or "").lower()
    if not word or word == "all":
        return question_count or None
    if word.isdigit():
        return int(word)
    return _NUMBER_WORDS.get(word)


def part_marks(sections):
    """Each part's total, worked out from what the paper says about it:
    questions answered x marks each. A multiple-choice part with no marks
    stated is read as one mark a question - the usual rule for these papers,
    flagged as assumed so the form can show it. None where the paper gives
    neither the count nor the marks, rather than a guess."""
    out = []
    for sec in sections or []:
        kind = sec.get("kind")
        each = sec.get("marks_each")
        assumed = False
        if each is None and kind == "mcq":
            each = 1
            assumed = True
        if each is None and _ONE_MARK.search(sec.get("instructions") or ""):
            each = 1
        count = _answer_count(sec.get("instructions"), sec.get("questions") or 0)
        total = count * each if count and each is not None else None
        out.append({"label": sec.get("label"), "questions": sec.get("questions"),
                    "answer_count": count, "marks_each": each,
                    "assumed_marks": assumed, "total": total})
    return out


def suggest_settings(raw_text, sections):
    """What a paper itself states, for the assessment settings form to fill
    in - and nothing else. A paper that does not print a duration gets none
    back, so the data-entry person enters it rather than receiving a guess.

    Duration is read from a labelled "Time: 2 hours" / "Duration: 120 min"
    line only; a bare "2 hours" in the body is too easily a question's own
    wording to trust.
    """
    out = {"parts": len(sections) if sections else None}
    breakdown = part_marks(sections)
    totals = [b["total"] for b in breakdown]
    out["parts_marks"] = breakdown
    out["total_marks"] = sum(totals) if breakdown and all(t is not None for t in totals) else None
    m = _DURATION.search(raw_text or "")
    if m:
        value = float(m.group(1))
        is_hours = m.group(2).lower().startswith("h")
        out["duration_minutes"] = int(round(value * 60 if is_hours else value))
    else:
        out["duration_minutes"] = None
    return out


QUESTION_TYPES = {"descriptive": "Descriptive", "mcq": "MCQ",
                  "sub": "Sub-questions"}


def detect_question_type(text, has_parts=False):
    """'sub' for a question with parts of its own (a container holding a
    shared stem, never answered directly), 'mcq' for one whose text
    holds an option list, 'fill_blank' for one whose text holds a
    numbered list of blanks to fill in (a real Grade 12 General English
    paper: a whole paragraph with "(1) ______ ... (2) ______..." running
    through it), else 'descriptive' - a question with its own answer and
    no options, blanks or parts. A picture-only MCQ has no text options,
    so it reads as descriptive until a reviewer corrects it.

    `depth` alone no longer forces 'sub' - the real Grade 12 General
    English paper's question 6, "Underline the option that best
    expresses the meaning...", has five depth-1 parts (one per
    sentence), each itself a complete MCQ with its own four lettered
    options and no children of its own. A part with no children is
    exactly as answerable on its own as a depth-0 question; only a part
    that itself has parts - a container, not a leaf - is 'sub'."""
    if has_parts:
        return "sub"
    _stem, options = split_mcq_options(text or "")
    if options:
        return "mcq"
    _stem, blanks = split_fill_blanks(text or "")
    return "fill_blank" if blanks else "descriptive"


# How several correct options are written in one answer: "1, 3", "1 and 3",
# "A/C", "I; III". Split on these, then each piece is read as one label.
_MCQ_ANSWER_SPLIT = re.compile(r"\s*(?:,|/|&|;|\band\b)\s*", re.IGNORECASE)


def mcq_correct_labels(answer):
    """Every correct option label an answer names, in the order written -
    a question can have more than one correct option ("1, 3"). [] when the
    answer names none. A single label gives a one-item list."""
    if not answer:
        return []
    labels = []
    for piece in _MCQ_ANSWER_SPLIT.split(answer.strip()):
        label = mcq_correct_label(piece)
        if label and label not in labels:
            labels.append(label)
    return labels


# "1. for" / "(1) documentation" - one entry of a fill-in-the-blank
# answer key, the shape _attach_answers() recognises in the paper's own
# marking scheme and stores as this question's answer verbatim (unlike
# an MCQ's options, a blank's text is never pulled out of the question
# and reassembled - only its separate answer key is this structured).
_BLANK_ANSWER_LINE = re.compile(
    r"^[ \t]*(?:\(\s*(\d{1,2})\s*\)|(\d{1,2})[.)])[ \t]*(\S.*)$", re.MULTILINE
)


def fill_blank_answers(answer):
    """{"1": "for", "2": "across", ...} from a stored flat answer key -
    the same numbering split_fill_blanks() gives each blank, so a caller
    zips them by number. {} when `answer` is empty or not in this shape
    (an ordinary short-answer question's answer is left as plain text,
    never parsed as if it were a list)."""
    if not answer:
        return {}
    out = {}
    for m in _BLANK_ANSWER_LINE.finditer(answer):
        number = m.group(1) or m.group(2)
        out[number] = m.group(3).strip()
    return out


def _bold_mcq_answer(text, bold_runs):
    """The printed label of the one option in this MCQ's text that a
    bold run identifies as correct - for a paper that marks its answer
    by bolding the right option instead of printing a separate answer
    key. Matched by substring containment rather than exact position:
    read_docx() only hands back bold runs as plain text snippets (see
    its own docstring for why), so "does this option's text contain
    that bold snippet" is the whole check.

    None when nothing is bold, when nothing here looks like an MCQ at
    all, or when a bold snippet matches more than one option - equally
    consistent with "no separate figure caught this bold text" and
    "two options happen to share a short bold word", and there is no
    way to tell those apart, so this stays silent rather than guessing.

    Checked both ways round: selecting a whole option line and bolding
    it captures its printed marker too ("(2) Helium"), but
    split_mcq_options() has already stripped that marker from the
    option's own text ("Helium") by the time this runs - so the bold
    run is the LONGER string here, not the shorter one, as often as not.
    """
    if not bold_runs:
        return None
    _stem, options = split_mcq_options(text)
    if not options:
        return None
    hits = [o["label"] for o in options
            if any(run in o["text"] or o["text"] in run for run in bold_runs)]
    return hits[0] if len(hits) == 1 else None


def _own_marks(piece, stem, inner_parts):
    """A part's own marks - preferring what the paper states for THIS
    part specifically over a number that actually belongs to one of its
    own children.

    A part with its own nested sub-parts - "(1)" holding "(a)(b)(c)",
    say - has no single marks annotation of its own on a real paper:
    each child carries its own, and (1) is worth their sum. Searching
    the whole piece for "the last marks annotation" is right for a part
    with no children (the existing, unchanged branch below) but wrong
    once there is a nested list underneath it - measured on the real
    GIT A/L paper's question (01), it read part "(1)" as 3 marks (part
    (c)'s own figure) instead of 10 (3+4+3, its three parts' true sum).
    """
    if not inner_parts:
        return _find_marks(piece)
    explicit = _find_marks(stem)
    if explicit is not None:
        return explicit
    child_marks = [p["marks"] for p in inner_parts]
    if all(m is not None for m in child_marks):
        return sum(child_marks)
    # Some child has no mark of its own yet (still being corrected, or
    # the paper genuinely does not print one there) - an incomplete sum
    # would look like a real total when it understates one, so this
    # part is left blank rather than confidently wrong.
    return None


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
    r"|underline the correct"
    # "choose the answer" - the real Grade 12 Sinhala-medium Business
    # Statistics paper's own instructions ("...correct or best-matching
    # answer, choose it"), with no English anywhere in the paper to
    # match on otherwise. "ක්" is an optional indefinite marker. The real
    # A/L Chemistry paper's Part I says it as "පිළිතුර තෝරා ගෙන" ("having
    # chosen the answer") - the same verb in another form.
    r"|පිළිතුර(?:ක්)?"
    r"[ \t]*තෝර(?:න්න|ා)",
    re.IGNORECASE,
)
_STRUCTURED_HINT = re.compile(r"structured|essay", re.IGNORECASE)
_MARKS_EACH = re.compile(
    r"(?:each|every)[ \t]+question[ \t]+carries[ \t]+(\d{1,3})[ \t]+marks?"
    # Sinhala, from the real A/L Chemistry paper: "එක් එක් ප්‍රශ්නයට ලකුණු
    # 150 බැගින්" - "150 marks for each question".
    r"|එක්[ \t]+එක්[ \t]+ප්‍?රශ්නයට[ \t]+ලකුණු[ \t]+(\d{1,3})[ \t]+බැගින්",
    re.IGNORECASE,
)
# An MCQ option - "(1) RAM", "A. Keyboard", "I. Ignoring climate data" -
# and not a lettered part of a structured question, which says what to
# do: the real Biosystem Technology paper's Part II-B is all "a) Explain
# ... b) Describe ...", and read as options the whole part was labelled
# multiple choice.
#
# Roman numerals matter here, not just digits and a-e letters: a real
# Grade 13 Biosystem Technology paper numbers every Part I option
# "I. II. III. IV. V." rather than "(1) (2) (3)..." - missed entirely by
# an earlier version of this pattern, so _kind_from() below never saw
# these questions as MCQs at all. A question whose options happened to
# be short text then correctly stayed whole by luck (too short to look
# like a real structured part - see _MIN_PART_CHARS), but one with
# longer, more descriptive options ("The percentage of ratio between
# the length and the breadth...") got split_parts()'d into five fake
# sub-questions, each stripped of its own "I."/"II." marker as if it
# were a real part's printed number rather than an MCQ option.
#
# Adding roman numerals then broke a DIFFERENT part of the same real
# paper: Part II-A nests roman-numeral sub-questions inside lettered
# parts - "a) i)Mention the main processes...", "b) i) Give examples
# ...". The verb-exclusion lookahead below only recognises a part when
# an instructional verb sits RIGHT AFTER its own marker ("a) Explain..."),
# but here "a)" is immediately followed by ANOTHER marker ("i)Mention",
# no space before the verb) - so the lookahead never got to see
# "Mention" at all, and "a)"/"b)" themselves started counting as MCQ
# option lines. That flipped the whole section's kind to "mcq",
# which skips _split_parts() entirely - so "04" never split into real
# a)/b)/i)/ii)/iii) parts in the database at all; it stayed one
# undivided block, same failure shape as the first bug, one level up.
# Fixed by also rejecting a marker immediately followed by what looks
# like ANOTHER marker - a genuine MCQ option is never followed by a
# nested list of its own. Separately, "Writedown"/"Listdown" (no space)
# are real typos in this same paper that the verb-exclusion list missed
# entirely, since \bwrite\b needs a word boundary "write" does not have
# when run straight into "down".
#
# Anchored to "after whitespace" rather than "start of a line" (like
# _SUB_MARKER/_INLINE_SUB_MARKER) so a real Grade 12 Tamil Physics
# paper, which prints a whole option list on ONE line ("(i) X (ii) Y
# (iii) Z (iv) W (v) V"), is still recognised - the line anchor alone
# only ever saw the first marker, and one marker is never enough to
# count as a list (see below), so these questions never looked like
# MCQs at all and fell back to "descriptive". The nested-marker and
# verb-exclusion lookaheads above are unaffected by this - they still
# reject "a) i)Mention..." and "a) Explain..." exactly as before,
# wherever they sit.
_OPTION_LINE = re.compile(
    r"(?:^|(?<=\s))[ \t]*(?:\([ \t]*(?:[1-5]|[a-eA-E]|[ivxIVX]{1,4})[ \t]*\)"
    r"|(?:[a-eA-E]|[ivxIVX]{1,4})[.)])[ \t]+"
    r"(?!(?:explain|describe|state|name|draw|write(?:down)?|calculate|define"
    r"|list(?:down)?|give|identify|compare|discuss|show|find|prove|outline"
    r"|mention|briefly|what|why|how|which|who|when|where|convert|complete"
    r"|evaluate|suggest|classify|distinguish|justify|analy[sz]e|determine"
    r"|derive|sketch|label)\b)"
    r"(?!\(?(?:[ivxIVX]{1,4}|[a-eA-E])[.)][ \t]*\S)"
    r"\S",
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
                d["answer"] = _strip_marks_annotation(worked[number(d)])
                continue
            # The scheme labels a part "(1)(a)"; the draft's own number is
            # the question's printed number plus that same suffix.
            d["answer"] = _strip_marks_annotation(head) or None
            printed = d["question_number"] or ""
            for kid in kids:
                suffix = (kid["question_number"] or "")[len(printed):]
                if per_part.get(suffix):
                    kid["answer"] = _strip_marks_annotation(per_part[suffix])

    # A fill-in-the-blank question's answer key is a flat numbered list -
    # "1. for\n2. across\n..." or "(1) documentation\n(2) ..." - never
    # "(N)(letter)" part tags, so _split_answer_by_part() above found
    # nothing to split and (when it even ran) just handed the whole block
    # over verbatim. Matched here by POSITION, not by printed number:
    # "Question 1" repeats once per major part on a real Grade 12 General
    # English paper (Part I, then Part II, each restarting its own
    # numbering), so a number alone cannot tell two same-numbered blocks
    # apart - but the scheme and the paper list their questions in the
    # same order throughout, so position can. Only runs when the paper's
    # own top-level questions outnumber-match the scheme's "Question N"
    # blocks exactly, and even then only touches a question that is
    # itself fill-in-the-blank shaped (split_fill_blanks finds 2+ blanks)
    # AND whose matched block is itself a flat numbered list - a
    # vocabulary question asking "underline the closest meaning for each
    # bold word" has no blanks in its own text at all, even though its
    # answer key happens to look like one, and must not be treated as
    # if it were.
    tops = [d for d in drafts if not d.get("parent_seq")]
    if blocks and len(tops) == len(blocks):
        for i, d in enumerate(tops):
            stop = blocks[i + 1].start() if i + 1 < len(blocks) else len(answers)
            block = answers[blocks[i].start():stop]
            body = block.split("\n", 1)[1].strip() if "\n" in block else ""
            lines = [ln for ln in body.split("\n") if ln.strip()]
            if (lines and all(_BLANK_ANSWER_LINE.match(ln) for ln in lines)
                    and len(split_fill_blanks(d["text"])[1]) >= 2):
                d["answer"] = body


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
        # Marks are read out of part["text"] before storage, not after -
        # stripping first would remove the very annotation _own_marks()
        # already used (via _split_parts()) to set part["marks"], but
        # that has already happened by the time _emit_parts() runs, so
        # stripping here is purely cosmetic on an already-decided value.
        text = _strip_marks_annotation(part["text"])
        drafts.append({
            "seq": seq,
            "question_number": number,
            "text": text,
            "marks": part["marks"],
            "section": section,
            "answer": None,
            "depth": depth,
            "parent_seq": parent_seq,
            "extraction_confidence": _confidence(text, part["marks"]),
        })
        _emit_parts(drafts, part["parts"], seq, number, section, depth + 1)


# A free-standing instruction between two numbered questions - "For
# questions 21 to 25, choose the answer that..." - belongs to the
# question that FOLLOWS it, but _number_starts() has no way to know
# that: everything between one marker and the next is handed to the
# question before it, by construction, the same way a part's own
# shared stem works. This is the one case where what comes AFTER a
# paragraph decides which question it belongs to.
#
# Recognised by a blank line on each side (its own paragraph) and how
# it opens - a real bullet character on a clean text layer, or, on a
# scanned paper, a single stray character that cannot legitimately
# start a real word: a Unicode "combining mark" (category Mn/Mc) is
# always attached to the letter before it, never standing alone. That
# covers what Tesseract actually did on a real Grade 13 (A/L) Political
# Science paper (scanned, 10 of 10 pages OCR'd): it read the bullet "•"
# in front of "For questions 21 to 25, choose..." as a single stray
# Sinhala dependent vowel sign, "ේ" - which cannot start a word on its
# own - sitting right where the bullet was, immediately before the
# instruction's first real word. The same check works for Tamil or any
# other script with combining marks, without hard-coding which one.
_BULLET_CHARS = "•‣∙·"
_MAX_INSTRUCTION_CHARS = 300


def _split_trailing_instruction(body):
    """(this question's own text, a trailing paragraph meant for the
    question(s) after it instead - or None when there is no such
    paragraph). See the comment above."""
    if "\n\n" not in body:
        return body, None
    head, tail = body.rsplit("\n\n", 1)
    tail = tail.strip()
    if not tail or len(tail) > _MAX_INSTRUCTION_CHARS:
        return body, None
    first = tail[0]
    if first in _BULLET_CHARS or unicodedata.category(first) in ("Mn", "Mc"):
        return head.strip(), tail
    return body, None


# "Answer questions 20 to 25 using the chart below" - an instruction for a
# GROUP of questions, printed above the first of them, so splitting by
# number leaves it at the end of the question before. Measured on the real
# uploads: Sinhala Business Statistics "36 සිට 39 දක්වා ප්‍රශ්න වලට ...",
# Biology "The responses for questions 41 to 50 should be chosen as
# follows", Political Science "අංක 21 සිට 25 තෙක් වූ ප්‍රශ්නවලට". The
# same papers carry number ranges that are NOT this - "Use about 75-100
# words", "7-8 months", "30-50%", class intervals "10-19", and "(1) - (5)"
# naming a question's own parts - so a line counts only when its range
# starts at the very next question's number AND it says "question" (in
# English, Sinhala or Tamil; the Sinhala ZWJ is optional, OCR drops it).
_GROUP_RANGE = re.compile(
    r"\(?(\d{1,3})\)?\s*(?:to|-|–|—|සිට|முதல்|and)\s*\(?(\d{1,3})\)?", re.IGNORECASE)
_QUESTION_WORD_ANY = re.compile(r"question|ප්‍?රශ්න|வினா|கேள்வி", re.IGNORECASE)
_MAX_GROUP_SPAN = 40
_MAX_PAPER_INSTRUCTIONS = 2000  # a header, not a page of text


def _split_group_instruction(body, next_number):
    """(this question's own text, the group instruction that followed it,
    (first, last) question numbers it covers) - or (body, None, None).
    Everything from the instruction's line on goes with it: what follows
    an instruction like this (a chart, a passage, a legend of answer
    choices) belongs to the questions it introduces, not to this one. The
    question's own first line is never taken."""
    try:
        following = int(next_number)
    except (TypeError, ValueError):
        return body, None, None
    lines = body.split("\n")
    for i, line in enumerate(lines[1:], 1):
        if not _QUESTION_WORD_ANY.search(line):
            continue
        for m in _GROUP_RANGE.finditer(line):
            first, last = int(m.group(1)), int(m.group(2))
            if first == following and first < last <= first + _MAX_GROUP_SPAN:
                kept = "\n".join(lines[:i]).rstrip()
                return kept, "\n".join(lines[i:]).strip(), (first, last)
    return body, None, None


def _looks_like_marking_scheme(text, start):
    """Does the marking scheme - not another real part - begin right
    after this heading? A real Grade 12 General English paper's scheme
    carries no "Answers:"/"Marking Scheme:" label of its own at all - it
    just restates the exam's own structure verbatim, opening with a bare
    "Part I" exactly like the real exam did, so _heading() reads it as
    an ordinary new part and the scheme's "Question 1 / 1. for / 2.
    across / ..." gets read as 50-odd brand new questions instead of
    being left as the answer key it is.

    "Question 1" (bare - real question text is never on the same line,
    see _ANSWER_BLOCK) followed immediately by a flat numbered answer
    line is the tell: a real question ALWAYS has substantial
    instructions right after its own "Question 1" ("Fill in the blanks
    in the following text..."), never a one-word answer.
    """
    lines = [ln for ln in text[start:start + 600].split("\n") if ln.strip()]
    if len(lines) < 2 or not _ANSWER_BLOCK.match(lines[0]):
        return False
    return bool(_BLANK_ANSWER_LINE.match(lines[1]))


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
        if h and h["label"] and _looks_like_marking_scheme(text, pos + len(line) + 1):
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

    # The paper's own opening text - title, time, "Answer all questions" -
    # everything before its first heading or question. Kept as the paper's
    # general instructions: it used to be dropped outright on a paper
    # with no part headings (the Sinhala Business Statistics paper lost
    # its whole "උපදෙස්" block this way).
    pre = spans[0]
    pre_end = pre["starts"][0][0] if pre["starts"] else pre["end"]
    paper_instructions = text[pre["intro_start"]:pre_end].strip()[:_MAX_PAPER_INSTRUCTIONS] or None

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
        return {"sections": [], "instruction_groups": [], "paper_instructions": None,
                "questions": [{
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

    drafts, parts, groups = [], [], []
    marker = _QUESTION_WORD_START if style == "question_word" else _Q_START
    for sp in sections:
        first = sp["starts"][0][0] if sp["starts"] else sp["end"]
        intro = text[sp["intro_start"]:first].strip()
        each = _MARKS_EACH.search(intro)
        each = int(each.group(1) or each.group(2)) if each else None

        pieces = []
        for i, (pos, _n, raw) in enumerate(sp["starts"]):
            end = (sp["starts"][i + 1][0] if i + 1 < len(sp["starts"])
                   else sp["end"])
            body = _strip_blank_marker(text[pos:end].strip())
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

        # An instruction for a group of questions ("answer 20 to 25...")
        # ends up at the end of the question before them - take it off
        # that question and keep it as the group's own (see
        # _split_group_instruction). Before the single-question mover
        # below, which would otherwise glue it onto question 20 alone.
        for i in range(len(pieces) - 1):
            raw, body = pieces[i]
            kept, instruction, span = _split_group_instruction(body, pieces[i + 1][0])
            if instruction:
                pieces[i] = (raw, kept)
                groups.append({"section": sp["label"], "from": pieces[i + 1][0],
                                "to": str(span[1]).zfill(len(pieces[i + 1][0] or "")),
                                "text": instruction})

        # A trailing instruction paragraph was handed to the question
        # before it by construction (see _split_trailing_instruction) -
        # move it to the front of the question it actually applies to.
        for i in range(len(pieces) - 1):
            raw, body = pieces[i]
            kept, instruction = _split_trailing_instruction(body)
            if instruction:
                pieces[i] = (raw, kept)
                next_raw, next_body = pieces[i + 1]
                pieces[i + 1] = (next_raw, instruction + "\n\n" + next_body)

        kind = sp["kind"] or _kind_hint(intro) or _kind_from(b for _, b in pieces)
        for raw, body in pieces:
            # An MCQ is never split: its options are a list at the left
            # margin that looks exactly like a part list, and an option is
            # not a question with an answer of its own.
            stem, parts_tree = ((body, []) if kind == "mcq"
                                else _split_parts(body))
            # "Each question carries 15 marks" is the question's mark
            # when the paper states it once for the whole section; else
            # the same stem-then-sum-of-parts rule _own_marks() uses for
            # every other level - a split question is worth what its
            # parts add up to, not whatever figure happens to sit last
            # in its raw text.
            marks = each or _own_marks(body, stem, parts_tree)
            # Marks are read from the unstripped stem/body above, not
            # after - stripping first would remove the very annotation
            # that reading just used.
            stem = _strip_marks_annotation(stem)
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
                    else (_confidence(stem, marks) if raw is not None else "low")),
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
    return {"sections": parts, "questions": drafts, "instruction_groups": groups,
            "paper_instructions": paper_instructions}


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


def ocr_image(source, lang=DEFAULT_OCR_LANG, psm=None):
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
        # psm picks Tesseract's page layout. The default reads a page of
        # separate blocks; psm 6 (one uniform block) reads the dense typed pages
        # the default missed - measured on the Sinhala paper's question pages,
        # 88 characters by default against 506 with psm 6.
        config = f"--psm {psm}" if psm else ""
        text = pytesseract.image_to_string(_prepare_for_ocr(image), lang=lang, config=config)
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
# A page under this many characters in the default OCR layout is retried in the
# uniform-block layout (see extract()).
_SPARSE_BLOCK = 400

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


def _mupdf_page_text(mu_page):
    """A page's plain text, read with PyMuPDF instead of pdfplumber.

    pdfplumber's underlying engine (pdfminer.six) extracts some Tamil
    conjunct-consonant clusters with their glyphs in the wrong order -
    measured on a real Grade 12 Tamil Physics paper, pure text-layer
    reading, no OCR involved at all: "சர்வதேச" (ர + VIRAMA + வ, a real
    conjunct) came out as "சரவ் தேச" (ர + வ + VIRAMA, plus a stray
    space), silently splitting one word into two wrong ones. Three
    separate conjunct words on the same page all showed the identical
    swap; PyMuPDF read every one of them correctly. Since this is about
    the TEXT CONTENT specifically, only the plain-text and text-lines
    reads are switched to PyMuPDF (this function and
    _mupdf_text_lines) - picture boxes, bold-run font names, and
    rendering a page as an image for OCR stay on pdfplumber, none of
    which showed this problem and all of which this codebase's
    picture/OCR routing already depends on pdfplumber's own object
    model for.
    """
    return mu_page.get_text()


def _mupdf_text_lines(mu_page):
    """[{"top": y, "text": line}, ...], the same shape pdfplumber's
    page.extract_text_lines() returns - see _mupdf_page_text() for why
    this reads through PyMuPDF instead."""
    lines = []
    for block in mu_page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line["spans"]).strip()
            if text:
                lines.append({"top": line["bbox"][1], "text": text})
    return lines


def _text_with_pictures_read(page, mu_page, boxes, ocr):
    """The text layer, with each picture's text slotted in at the height
    it sits on the page - so a question pasted in as a screenshot lands
    between the question above it and the one below, not at the end
    where segment() would glue it onto the last question.

    A picture's OCR output is kept only once it reaches _SPARSE_PAGE
    characters - the same floor a whole page uses to decide whether its
    own text layer is real text at all (_pdf_page_route). Below that, it
    is far more likely to be noise from a diagram or graph (axis labels,
    tick marks, stray lines Tesseract mistook for letters) than genuine
    pasted-in text: a real Grade 12 Tamil Physics paper has two
    questions, each illustrating a velocity-time graph, where Tesseract
    read a handful of nonsense syllables off the picture - "ஓ. ஓ." / "ல
    ல்‌ டி" - that then spliced themselves into the middle of the
    question's own sentence, with nothing to tell a reviewer the words
    were never really there. A genuine pasted-in question (the Grade 13
    Model Pure paper, English, where every question IS a picture) is
    never this short - it is a whole sentence - so this costs nothing
    real while catching exactly the noise case.
    """
    blocks = [(line["top"], _fix_ligatures(line["text"]))
              for line in _mupdf_text_lines(mu_page)]
    for box in boxes:
        text = ocr(page.crop(box).to_image(resolution=_OCR_DPI).original).strip()
        if len(text) >= _SPARSE_PAGE:
            blocks.append((box[1], text))
    blocks.sort(key=lambda b: b[0])
    return "\n".join(t for _, t in blocks)


def _pdf_bold_runs(page):
    """Text of every substantial bold-font word run on this page, in
    reading order - independent of read_pdf()'s own text assembly
    (extract_text() / OCR), so this can never disturb the page-routing
    logic that already works. Same reasoning as _docx_walk()'s
    bold_runs: plain text snippets, not positions, so a caller only
    needs to check "does this option's text contain one of these", not
    track offsets through anything downstream.

    PDF carries no font-weight flag of its own - a bold font's NAME
    usually contains "Bold" ("Arial-BoldMT", "TimesNewRomanPS-BoldMT"),
    the same heuristic other PDF tooling relies on for the same reason.
    A page with no real text layer (pure OCR) has no font names to
    check at all, so this simply finds nothing there - safe to call
    unconditionally on every page rather than only ones read as "text".
    """
    try:
        words = page.extract_words(extra_attrs=["fontname"])
    except Exception:
        return []
    runs, current = [], []
    for w in words:
        if "bold" in (w.get("fontname") or "").lower():
            current.append(w["text"])
        elif current:
            runs.append(" ".join(current))
            current = []
    if current:
        runs.append(" ".join(current))
    return [r for r in runs if len(r.strip()) >= _MIN_BOLD_ANSWER_CHARS]


_NUMBERED_HEAD = re.compile(r"^\s*(\d{1,2})\s*[.)]\s")


def recover_missing_numbers(text):
    """Put back the question numbers OCR dropped from the start of a line.

    Tesseract reads a Sinhala question header such as "4. දේශපාලන..." as the
    words alone, so the number is lost and the question is merged into the one
    before it. A header of that kind is a short line ending in a comma, with
    its first part "(1)" directly under it. Between two numbers that were read,
    if the headers found equal the numbers missing, they are numbered in order;
    any other gap is left as it is, because a guessed number would be worse than
    a merged question the reviewer can see and split.

    Measured on the Grade 12 Political Science paper (force-OCR): 5 of its 6
    gaps in Part A match this rule exactly.
    """
    lines = text.split("\n")
    found = []
    for i, line in enumerate(lines):
        m = _NUMBERED_HEAD.match(line)
        if m:
            found.append((i, int(m.group(1))))
    if len(found) < 2:
        return text

    candidates = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if (not stripped or len(stripped) > 90 or _NUMBERED_HEAD.match(line)
                or stripped[0] in "(.0123456789" or not stripped.endswith((",", ":"))):
            continue
        nxt = next((x.strip() for x in lines[i + 1:] if x.strip()), "")
        if nxt.startswith("(1)"):
            candidates.append(i)

    # Question 1 has nothing before it to measure from - the top of the paper
    # stands in as "question 0", so a dropped first header is found the same way.
    bounded = [(-1, 0)] + found
    changed = False
    for (ia, a), (ib, b) in zip(bounded, bounded[1:]):
        if b <= a + 1:
            continue
        between = [c for c in candidates if ia < c < ib]
        missing = list(range(a + 1, b))
        if len(between) == len(missing):
            for c, n in zip(between, missing):
                lines[c] = f"{n}. {lines[c].lstrip()}"
                changed = True
    return "\n".join(lines) if changed else text


# ---------------------------------------------------------------- tables
# Tables are left out of the extracted text: data entry adds every table
# to its question as a picture, so its cells as text only cluttered the
# question (and fed the duplicate check rows of numbers). What counts as a
# table was measured on every stored paper, not assumed - real ones
# (Political Science's statement-comparison grids, a "C3 plants vs C4
# plants" table, GIT's truth tables) are kept out, but two shapes that look
# like tables are not tables:
#   - a one-column frame around question text (GIT p16 "Defragmenter
#     reorganizes...") - a box, so at least 2 columns are required;
#   - a paper's header laid out as a grid ("Subject - Combined mathematics
#     | Grade - 13 | Term - 1st | Time", top of page 1) - dropping it would
#     lose the grade/subject/title detect_paper_meta() reads from it.
# And a grid covering nearly the whole page is a page border, not a table
# (none seen yet - the largest real table covered 41% of its page).
_TABLE_MIN_ROWS = 2
_TABLE_MIN_COLS = 2
_TABLE_MAX_COVER = 0.8
_HEADER_ZONE = 0.3  # top share of page 1 a header table sits within

# A third shape: questions laid out in a grid ("26. <statement> |
# <statement>", Political Science page 7) - rows_are_questions() in
# ocr_html.py, shared with pasted OCR output and the local reader.


def _data_tables(page, page_index):
    """Where this pdfplumber page's real data tables are, as (x0, top,
    x1, bottom) boxes - see the rules above. Detection works from the
    lines a table is drawn with, so it also finds a table on a page whose
    text is only in the picture (a scan with ruled lines, or a legacy-font
    page OCR reads); a table scanned with no drawn lines at all is not
    found here."""
    try:
        tables = page.find_tables()
    except Exception:
        return []
    width, height = float(page.width), float(page.height)
    top_of_page = float(page.bbox[1])
    found = []
    for table in tables:
        x0, top, x1, bottom = table.bbox
        rows = table.extract()
        columns = max((len(r) for r in rows), default=0)
        if len(rows) < _TABLE_MIN_ROWS or columns < _TABLE_MIN_COLS:
            continue
        if (x1 - x0) * (bottom - top) > _TABLE_MAX_COVER * width * height:
            continue
        if page_index == 0 and bottom - top_of_page <= _HEADER_ZONE * height:
            continue
        if _rows_are_questions([next((c for c in r if c and c.strip()), "") for r in rows]):
            continue
        found.append((x0, top, x1, bottom))
    return found


def _inside_any(box, tables):
    """Whether a picture box's centre falls inside one of the tables."""
    x0, top, x1, bottom = box[:4]
    cx, cy = (x0 + x1) / 2, (top + bottom) / 2
    return any(t[0] <= cx <= t[2] and t[1] <= cy <= t[3] for t in tables)


def _drop_tables_from_text(mu_page, tables):
    """Remove the text inside each table from the PyMuPDF page - in
    memory only, the stored file is never written - so the page's text
    is read without it. Pictures and drawn lines are left alone."""
    import fitz
    for x0, top, x1, bottom in tables:
        mu_page.add_redact_annot(fitz.Rect(x0, top, x1, bottom))
    mu_page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                             graphics=fitz.PDF_REDACT_LINE_ART_NONE)


def _blank_tables(image, page, tables):
    """The page image with each table painted white, so OCR reads the
    rest of the page without it."""
    from PIL import ImageDraw
    sx = image.width / float(page.width)
    sy = image.height / float(page.height)
    ox, oy = float(page.bbox[0]), float(page.bbox[1])
    draw = ImageDraw.Draw(image)
    for x0, top, x1, bottom in tables:
        draw.rectangle([(x0 - ox) * sx, (top - oy) * sy, (x1 - ox) * sx, (bottom - oy) * sy],
                       fill="white")
    return image


def read_pdf(path, ocr, force_ocr=False):
    """(text, note, bold_runs). Each page is read the cheapest way that
    actually captures it - see _pdf_page_route. `ocr` is image -> text.

    `bold_runs` is the text of every substantial bold-font run across
    every page - see _pdf_bold_runs() for why it is text snippets rather
    than positions, and extract() for how it becomes a fallback correct-
    MCQ-answer when a paper marks its answer by bolding the right option
    instead of printing a separate answer key.

    If OCR is unavailable, the pages that needed it keep whatever text
    layer they had and the note says how many were not read - one
    missing program should not throw away the pages that did have text.

    `force_ocr` reads every page as a picture, whatever its text layer says.
    Needed for Sinhala papers whose PDF was typed in a legacy font: the text
    layer holds the font's own codes, not Sinhala, and Tesseract's Sinhala
    reading of the rendered page is the only reliable text (measured on a
    Grade 12 Political Science paper - the text layer read "තේශොලනය" where
    the paper says "දේශපාලනය", and OCR read it correctly).
    """
    try:
        import pdfplumber
    except ImportError as e:
        raise ExtractionUnavailable(
            "pdfplumber is not installed - `pip install pdfplumber`"
        ) from e
    try:
        import fitz
    except ImportError as e:
        raise ExtractionUnavailable(
            "pymupdf is not installed - `pip install pymupdf`"
        ) from e

    parts, bold_runs = [], []
    read_by_ocr = not_read = came_back_empty = tables_skipped = 0
    reason = None
    with pdfplumber.open(path) as pdf, fitz.open(path) as mu_doc:
        total = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            mu_page = mu_doc[i]
            # Text content itself comes from PyMuPDF (see
            # _mupdf_page_text's own note); everything else here - picture
            # boxes, bold-run detection, rendering a page as an image for
            # OCR - stays on pdfplumber, which this codebase's routing
            # already depends on.
            text = _fix_ligatures(_mupdf_page_text(mu_page) or "")
            tables = _data_tables(page, i)
            boxes = [b for b in _pictures_on(page) if not _inside_any(b, tables)]
            bold_runs.extend(_pdf_bold_runs(page))
            # Routed on the page's whole text layer - whether it is real
            # text at all is the question here, and a table's cells are as
            # real as the rest. Only then is the table taken out of it.
            route = "page" if force_ocr else _pdf_page_route(text, boxes)
            if tables:
                tables_skipped += len(tables)
                _drop_tables_from_text(mu_page, tables)
                text = _fix_ligatures(_mupdf_page_text(mu_page) or "")
            if route != "text" and reason is None:
                try:
                    if route == "page":
                        image = page.to_image(resolution=_OCR_DPI).original
                        text = ocr(_blank_tables(image, page, tables) if tables else image)
                        # OCR "succeeding" with nothing is its own failure.
                        # Measured on the real Grade 12 term paper - phone
                        # photos of handwritten answer sheets - Tesseract
                        # returned 0 characters, and without this the page
                        # just looked like a short paper with no warning.
                        if len(re.sub(r"\s", "", text)) < _SPARSE_PAGE:
                            came_back_empty += 1
                    else:
                        text = _text_with_pictures_read(page, mu_page, boxes, ocr)
                    read_by_ocr += 1
                except ExtractionUnavailable as e:
                    reason = str(e)
            if route != "text" and reason is not None:
                not_read += 1
            parts.append(text)

    note = "pdf-text"
    if read_by_ocr:
        note += f" + ocr ({read_by_ocr} of {total} pages)"
    if tables_skipped:
        note += f" - {tables_skipped} table(s) left out (add them as pictures)"
    if came_back_empty:
        note += (f" - {came_back_empty} of {total} pages not read: OCR found "
                 f"no text (handwriting or a poor photo, which Tesseract "
                 f"cannot read - or a blank page)")
    if not_read:
        note += f" - {not_read} of {total} pages not read: {reason}"
    return "\n".join(parts).strip(), note, bold_runs


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


def _run_is_bold(run_el):
    """Whether a <w:r> run element is bold - <w:rPr><w:b/></w:rPr>, with
    an explicit w:val="0"/"false" meaning NOT bold (Word writes that
    when bold is turned off inside an otherwise-bold character style,
    not just <w:b>'s plain absence)."""
    props = run_el.find(_W + "rPr")
    if props is None:
        return False
    b = props.find(_W + "b")
    if b is None:
        return False
    return b.get(_W + "val") not in ("0", "false")


# A bold run shorter than this is a heading, a label like "Note:", or
# emphasis on a single word inside an ordinary sentence - not a paper
# marking one whole MCQ option as the correct answer. Measured against
# the shortest real option text seen in this codebase's own test
# papers ("Helium", "Argon"): every genuine option is at least this long.
_MIN_BOLD_ANSWER_CHARS = 3


# ------------------------------------------------- Word automatic numbering
#
# A numbered or lettered list made with Word's own numbering button has no
# number in its text: "I." / "(a)" / "1." are drawn by Word from
# numbering.xml, so python-docx and our walk read "Ignoring climate data"
# with no marker at all. Measured on the real Word papers: 6 of 7 use it -
# "paper 6.docx" (Grade 13 Biosystem Technology) numbers 245 of its 438
# paragraphs that way, every MCQ option among them, so 0 of its 50 MCQs had
# options and none of Part II's parts split (the PDF of the same paper,
# where the labels are printed text, gave 33 of 50). The labels are put
# back as text, exactly as Word would draw them.

def _roman(n):
    out = ""
    for value, digits in ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"),
                          (90, "xc"), (50, "l"), (40, "xl"), (10, "x"), (9, "ix"),
                          (5, "v"), (4, "iv"), (1, "i")):
        while n >= value:
            out, n = out + digits, n - value
    return out


def _letters(n):
    # Word's own sequence: a..z, then aa..zz, bbb...
    n = max(n, 1)
    return chr(ord("a") + (n - 1) % 26) * ((n - 1) // 26 + 1)


def _format_list_number(n, fmt):
    if fmt == "decimalZero":
        return f"{n:02d}"
    if fmt == "lowerLetter":
        return _letters(n)
    if fmt == "upperLetter":
        return _letters(n).upper()
    if fmt == "lowerRoman":
        return _roman(n)
    if fmt == "upperRoman":
        return _roman(n).upper()
    return str(n)  # decimal, and anything rarer read as a plain number


class _DocxNumbering:
    """Replays Word's list counters in document order and gives each
    numbered paragraph the label Word draws for it ("I.", "(a)", "1.").

    Counters belong to the list definition (abstractNum), so two list
    instances that share one definition continue each other - Word's
    "continue numbering" - unless the instance restarts a level with
    startOverride, which is how "restart at 1" is saved (the next MCQ's
    options start again at I)."""

    def __init__(self, numbering_xml, styles_xml):
        import xml.etree.ElementTree as ET

        def val(el, name, default=None):
            found = el.find(_W + name) if el is not None else None
            return found.get(_W + "val", default) if found is not None else default

        def levels(parent):
            out = {}
            for lvl in parent.findall(_W + "lvl"):
                out[int(lvl.get(_W + "ilvl", "0"))] = {
                    "fmt": val(lvl, "numFmt", "decimal"),
                    "text": val(lvl, "lvlText"),
                    "start": int(val(lvl, "start", "1") or 1),
                    "style": val(lvl, "pStyle"),
                }
            return out

        self.abstract, self.nums, self.styles = {}, {}, {}
        if numbering_xml:
            root = ET.fromstring(numbering_xml)
            for a in root.findall(_W + "abstractNum"):
                self.abstract[a.get(_W + "abstractNumId")] = levels(a)
            for n in root.findall(_W + "num"):
                restarts, replaced = {}, {}
                for o in n.findall(_W + "lvlOverride"):
                    ilvl = int(o.get(_W + "ilvl", "0"))
                    start = val(o, "startOverride")
                    if start is not None:
                        restarts[ilvl] = int(start)
                    replaced.update(levels(o))
                self.nums[n.get(_W + "numId")] = {
                    "abstract": val(n, "abstractNumId"), "restarts": restarts,
                    "replaced": replaced}
        if styles_xml:
            root = ET.fromstring(styles_xml)
            for s in root.findall(_W + "style"):
                p_pr = s.find(_W + "pPr")
                num_pr = p_pr.find(_W + "numPr") if p_pr is not None else None
                self.styles[s.get(_W + "styleId")] = {
                    "num": val(num_pr, "numId"), "ilvl": val(num_pr, "ilvl"),
                    "based": val(s, "basedOn")}
        self.counters = {}
        self.started = set()

    def _style_numbering(self, style_id):
        seen = set()
        while style_id and style_id not in seen:
            seen.add(style_id)
            style = self.styles.get(style_id)
            if style is None:
                return None, None
            if style["num"] is not None:
                return style["num"], style["ilvl"]
            style_id = style["based"]
        return None, None

    def label(self, paragraph):
        p_pr = paragraph.find(_W + "pPr")
        if p_pr is None:
            return ""
        style_el = p_pr.find(_W + "pStyle")
        style_id = style_el.get(_W + "val") if style_el is not None else None
        num_pr = p_pr.find(_W + "numPr")
        num_id = ilvl = None
        if num_pr is not None:
            n, i = num_pr.find(_W + "numId"), num_pr.find(_W + "ilvl")
            num_id = n.get(_W + "val") if n is not None else None
            ilvl = i.get(_W + "val") if i is not None else None
        if num_id is None and style_id:
            num_id, style_ilvl = self._style_numbering(style_id)
            ilvl = ilvl if ilvl is not None else style_ilvl
        num = self.nums.get(num_id)
        if num_id in (None, "0") or num is None:
            return ""  # numId 0 is Word's "numbering removed here"
        lvls = dict(self.abstract.get(num["abstract"], {}))
        lvls.update(num["replaced"])
        if ilvl is None:
            ilvl = next((k for k, v in lvls.items() if style_id and v["style"] == style_id), 0)
        ilvl = int(ilvl)
        level = lvls.get(ilvl)
        if level is None:
            return ""
        counters = self.counters.setdefault(num["abstract"], {})
        if num_id not in self.started:
            self.started.add(num_id)
            for restart_lvl, start in num["restarts"].items():
                counters[restart_lvl] = start - 1
                for deeper in [k for k in counters if k > restart_lvl]:
                    del counters[deeper]
        counters[ilvl] = counters[ilvl] + 1 if ilvl in counters else level["start"]
        for deeper in [k for k in counters if k > ilvl]:
            del counters[deeper]
        if level["fmt"] in ("bullet", "none") or not level["text"]:
            return ""

        def number(match):
            k = int(match.group(1)) - 1
            lv = lvls.get(k) or {"fmt": "decimal", "start": 1}
            return _format_list_number(counters.get(k, lv["start"]), lv["fmt"])

        return re.sub(r"%(\d)", number, level["text"]).strip()


# Question 1's number at the start of a line - before it, a table is the
# paper's header (subject / grade / term), which is kept.
_FIRST_QUESTION = re.compile(r"(?m)^\s*(?:Q(?:uestion)?\.?\s*)?\(?0?1\s*[.):]")


def _is_docx_data_table(tbl, text_so_far):
    """A Word table to leave out: 2+ rows and 2+ columns (a one-column
    table is a box around question text), not questions laid out in rows
    (_rows_are_questions), and after question 1 has begun (before it, a
    table is the paper's header) - the same rules as a PDF's
    _data_tables(), with "before question 1" standing in for "top of page
    1", since a Word file has no fixed pages."""
    rows = tbl.findall(_W + "tr")
    columns = max((len(r.findall(_W + "tc")) for r in rows), default=0)
    if len(rows) < _TABLE_MIN_ROWS or columns < _TABLE_MIN_COLS:
        return False
    first_cells = []
    for row in rows:
        cells = ["".join(t.text or "" for t in tc.iter(_W + "t")) for tc in row.findall(_W + "tc")]
        first_cells.append(next((c for c in cells if c.strip()), ""))
    if _rows_are_questions(first_cells):
        return False
    return bool(_FIRST_QUESTION.search(text_so_far))


def _docx_walk(el, out, picture, bold_runs=None, skipped=None, numbering=None):
    """Body XML -> text, in document order. python-docx's own
    `document.paragraphs` skips tables, equations and pictures - on a
    maths paper, that is most of the content.

    `bold_runs`, when given a list, collects the text of every bold run
    encountered - not where it sits (bold_runs is presentation-only
    metadata, not offsets into `out`, so nothing downstream has to
    track position through the "\\n{3,}" collapse and final strip()
    read_docx() applies afterwards). This is how an MCQ whose correct
    option is marked by bolding it, rather than a separate answer key,
    gets identified - see extract()'s use of mcq_correct_label() and
    split_mcq_options() together with this.
    """
    tag = el.tag if isinstance(el.tag, str) else ""
    if tag == _W + "tbl" and _is_docx_data_table(el, "".join(out)):
        # Left out - see _data_tables() for the rule and why. `skipped`
        # counts them for read_docx's note.
        if skipped is not None:
            skipped.append(1)
        return
    if tag == _M + "oMath":
        out.append(_omml(el))
        return
    if tag == _W + "r" and _run_is_bold(el):
        local = []
        for child in el:
            _docx_walk(child, local, picture, bold_runs, skipped, numbering)
        text = "".join(local)
        out.append(text)
        if bold_runs is not None and len(text.strip()) >= _MIN_BOLD_ANSWER_CHARS:
            bold_runs.append(text.strip())
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
            _docx_walk(choice, out, picture, bold_runs, skipped, numbering)
        return
    if tag == _W + "tr":
        # One line per row: an MCQ options grid becomes
        # "A. Colombo | B. Kandy", which segment() leaves attached to
        # the question above it. Bold tracking does not follow into a
        # table cell - real MCQ options in the papers seen so far are
        # always plain paragraphs, never a table, so this is a
        # deliberate scope limit, not an oversight.
        cells = []
        for tc in el.findall(_W + "tc"):
            sub = []
            _docx_walk(tc, sub, picture, skipped=skipped, numbering=numbering)
            cells.append(" ".join("".join(sub).split()))
        out.append(" | ".join(c for c in cells if c) + "\n")
        return
    if tag == _W + "p" and numbering is not None:
        # Word's own list label ("I.", "(a)", "1.") - not in the text; see
        # _DocxNumbering.
        label = numbering.label(el)
        if label:
            out.append(label + " ")
    for child in el:
        _docx_walk(child, out, picture, bold_runs, skipped, numbering)
    if tag == _W + "p":
        out.append("\n")


def _docx_numbering(path):
    """The file's list definitions and paragraph styles, for
    _DocxNumbering - read straight from the .docx (a zip), since
    python-docx exposes neither in a form that says how a list counts.
    None when the file cannot be read that way; the walk then simply adds
    no labels, as before."""
    import zipfile
    try:
        with zipfile.ZipFile(str(path)) as z:
            names = set(z.namelist())
            numbering = z.read("word/numbering.xml") if "word/numbering.xml" in names else None
            styles = z.read("word/styles.xml") if "word/styles.xml" in names else None
        if numbering is None:
            return None
        return _DocxNumbering(numbering, styles)
    except (zipfile.BadZipFile, KeyError, OSError, ValueError, SyntaxError):
        return None


def read_docx(path, ocr):
    """(text, note, bold_runs). Paragraphs, tables and equations in
    document order, with each embedded picture OCR'd in place - a
    question pasted into Word as a screenshot is otherwise invisible.

    `bold_runs` is the text of every substantial bold run in the
    document - see _docx_walk()'s own docstring for why it is text
    snippets rather than positions, and extract() for how it becomes a
    fallback correct-MCQ-answer when a paper marks its answer by
    bolding the right option instead of printing a separate answer key.
    """
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

    out, bold_runs, skipped = [], [], []
    _docx_walk(document.element.body, out, picture, bold_runs, skipped, _docx_numbering(path))
    text = re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()

    note = "docx-text"
    if read_by_ocr:
        note += f" + ocr ({read_by_ocr} image{'s' if read_by_ocr != 1 else ''})"
    if not_read:
        note += f" - {not_read} image(s) not read: {reason}"
    if skipped:
        note += f" - {len(skipped)} table(s) left out (add them as pictures)"
    return text, note, bold_runs


# --------------------------------------------------------------- extract

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}


def was_incomplete(extractor):
    """True when part of an uploaded file could not be read, so the
    review page can say so instead of letting a half-read paper look
    like a short one."""
    return (extractor.startswith(("unavailable", "failed", "unsupported"))
            or "not read" in extractor)


# html_to_lines() - OCR output written as HTML (Surya's, pasted from the
# local reader) to plain lines, formulas and equations as readable text -
# lives in ocr_html.py, which the local reader ships an identical copy of.


def extract_text(text, style=DEFAULT_SPLIT_STYLE, filename=None):
    """Text someone read elsewhere (the local OCR tool on a data-entry
    computer, or copied straight out of a document) -> the same result
    shape extract() gives an uploaded file, so it is stored and reviewed
    exactly like one. Everything after reading is shared: structure(),
    detect_paper_meta(), parts, MCQ options, answers. What is not: the
    OCR-only number repair (whoever read it already did the OCR) and
    bolded-answer detection (pasted text carries no bold)."""
    text = html_to_lines(text or "").strip()
    meta = detect_paper_meta(text, filename=filename)
    parsed = structure(text, style=style)
    for q in parsed["questions"]:
        q.setdefault("grade", meta["grade"])
        q.setdefault("subject", meta["subject"])
    return {
        "file_type": "text",
        "extractor": "pasted text",
        "raw_text": text,
        "paper": meta,
        "sections": parsed["sections"],
        "instruction_groups": parsed["instruction_groups"],
        "paper_instructions": parsed["paper_instructions"],
        "questions": parsed["questions"],
    }


def extract(path, style=DEFAULT_SPLIT_STYLE, lang=DEFAULT_OCR_LANG, ocr=None,
            filename=None, force_ocr=False):
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
            text = ocr_image(image, lang=lang)
            # A dense typed page can come back nearly empty in the default layout
            # mode - try the uniform-block mode and keep whichever read more.
            if force_ocr and len(re.sub(r"\s", "", text)) < _SPARSE_BLOCK:
                retry = ocr_image(image, lang=lang, psm=6)
                if len(re.sub(r"\s", "", retry)) > len(re.sub(r"\s", "", text)):
                    text = retry
            return text

    text = ""
    extractor = "none"
    bold_runs = []
    try:
        if suffix == ".pdf":
            text, extractor, bold_runs = read_pdf(path, ocr, force_ocr=force_ocr)
        elif suffix == ".docx":
            text, extractor, bold_runs = read_docx(path, ocr)
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

    if force_ocr:
        text = recover_missing_numbers(text)
    meta = detect_paper_meta(text, filename=filename or path.name)
    parsed = structure(text, style=style)
    for q in parsed["questions"]:
        q.setdefault("grade", meta["grade"])
        q.setdefault("subject", meta["subject"])
        # Some papers never print a separate answer key at all - the
        # correct option is just bolded in place. Only a fallback: a
        # real answer key, already read by _attach_answers() above,
        # always wins. MCQ is never split, so only a depth-0 question
        # can be one at all.
        if not q.get("answer") and not q.get("depth"):
            label = _bold_mcq_answer(q.get("text") or "", bold_runs)
            if label:
                q["answer"] = f"({label})"

    return {
        "file_type": file_type,
        "extractor": extractor,
        "raw_text": text,
        "paper": meta,
        "sections": parsed["sections"],
        "instruction_groups": parsed["instruction_groups"],
        "paper_instructions": parsed["paper_instructions"],
        "questions": parsed["questions"],
    }
