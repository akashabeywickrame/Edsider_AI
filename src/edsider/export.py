"""A finished paper as the rows EdSider keeps for one - the `assessment`
schema (assessment, schedule, assessor, part, part_section,
assessment_question, operation, review) and `question.question` /
`question.passage`. See CLAUDE.md, "Sending a paper to EdSider".

Pure: it reads a paper as store.py returns it and writes nothing, so the
preview an administrator checks and the eventual send are the same rows.
Rows name each other by local keys ("part1", "sec2", "q512"), because
EdSider's own ids exist only once a row is written; whoever writes them
replaces each key with the id it got back, in the order given:

    assessment, schedule, assessor, parts, sections, passages,
    questions (a question's parts before the question), links,
    operation, review

Every shape here was read off EdSider's live tables (paper #2141, Grade 12
GIT, 40 MCQs and 5 essays) rather than guessed:
- Text is a JSON list of Draft.js documents, compact, one per stem/option;
  a picture is an atomic block whose IMAGE entity holds a data: URL.
- An MCQ's answer is a weight per option adding to 100 ("0,100,0,0");
  answer_type_id 1 for one correct option, 2 for several.
- A question with parts is a qtype 5 parent (answer "0", sub_questions
  listing its parts' ids) plus one qtype 5 row per part
  (is_subquestion "true"); EdSider has one level of parts, so a part's own
  parts become numbered lines inside it.
- Marks are on the link row: 0 on a parent's, each part's own on its
  link, and every link of one question carries that question's number.
- People are named two ways: the paper, schedule, assessor and links by
  the EdSider user id as text ("314", "CASS314"); the operation, review
  and passage rows by the person's registration.teacher id (staff id).
"""

import hashlib
import json
import re

from ..intake import extract

# The states a paper may arrive in, with assessment.status ids - EdSider
# sets assessment_state and status_id together, always matching.
ARRIVAL_STATES = {"Initial": 1, "Ready": 2, "ReviewCompleted": 8, "ReadyToPublish": 11}
DEFAULT_ARRIVAL = "ReviewCompleted"
# assessment.notation
NOTATION_IDS = {"alpha": 1, "numeric": 2, "roman": 3}
# The only operation EdSider has ever recorded (950 of 950): ProofReading.
OPERATION_ID = 9
REVIEW_COMPLETED = 8
TITLE_MAX = 128
INFO_MAX = 1024
# EdSider's own form saves every duration in minutes (1,982 of 1,993
# schedules use unit 1), so ours are converted rather than copied.
MINUTES_IN = {"minutes": 1, "hours": 60, "days": 1440, "months": 43200}
KIND_TITLE = {"mcq": "Multiple Choice Questions", "structured": "Structured Questions"}
# "Part II - A" / "Part II-B": two sections of one EdSider part.
_SUB_PART = re.compile(r"^(?P<base>.+?)\s*[-–—]\s*(?P<sub>[A-Z])$")
_FIRST_NUMBER = re.compile(r"\d+")
# A part's text that already prints its marks - "[2]", "(4 marks)".
_PRINTED_MARKS = re.compile(r"\[\s*\d+\s*\]|\bmarks?\b", re.IGNORECASE)
_LAST_LABEL = re.compile(r"(\([^()]*\))\s*$")


# ------------------------------------------------------------- Draft.js

def _key(*parts):
    """A Draft.js block key - stable for the same paper, so two previews
    of an unchanged paper are identical."""
    return hashlib.md5("|".join(map(str, parts)).encode("utf-8")).hexdigest()[:5]


def _block(text, key, kind="unstyled"):
    return {"key": key, "text": text, "type": kind, "depth": 0,
            "inlineStyleRanges": [], "entityRanges": [], "data": {}}


def draft_doc(text, seed, images=()):
    """One Draft.js document: a block per line of `text` (blank lines at
    either end dropped, runs of them kept as one), then each picture as
    an atomic block - the shape EdSider's own editor saves. A picture is
    its src, or (src, label): a label the paper prints under it ("A") is a
    line of its own under the picture."""
    lines = [line.rstrip() for line in (text or "").replace("\r\n", "\n").split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    kept = []
    for line in lines:
        if line.strip() or (kept and kept[-1].strip()):
            kept.append(line)
    blocks = [_block(line, _key(seed, "t", i)) for i, line in enumerate(kept)]
    entity_map = {}
    for i, picture in enumerate(images):
        src, label = picture if isinstance(picture, tuple) else (picture, None)
        n = len(entity_map)
        block = _block(" ", _key(seed, "i", i), "atomic")
        block["entityRanges"] = [{"offset": 0, "length": 1, "key": n}]
        blocks.append(block)
        entity_map[str(n)] = {"type": "IMAGE", "mutability": "MUTABLE", "data": {"src": src}}
        if label:
            blocks.append(_block(label, _key(seed, "l", i)))
    if not blocks:
        blocks.append(_block("", _key(seed, "e")))
    return {"blocks": blocks, "entityMap": entity_map}


def draft_json(docs):
    """The stored form: compact JSON of a list of documents."""
    return json.dumps(docs, ensure_ascii=False, separators=(",", ":"))


def mcq_weights(option_count, correct_indexes):
    """EdSider's MCQ answer: a whole-number weight per option, adding to
    100 - "0,100,0,0", or "50,50,0,0" for two correct ones. Any remainder
    of an uneven split goes to the first correct option."""
    weights = [0] * option_count
    if correct_indexes:
        share, extra = divmod(100, len(correct_indexes))
        for n, i in enumerate(correct_indexes):
            weights[i] = share + (extra if n == 0 else 0)
    return ",".join(str(w) for w in weights)


# --------------------------------------------------------------- helpers

def _find(lists, kind, item_id):
    if item_id is None:
        return None
    return next((x for x in lists.get(kind, []) if x["id"] == item_id), None)


def _question_no(question_number):
    m = _FIRST_NUMBER.search(question_number or "")
    return int(m.group()) if m else None


def _part_label(question_number):
    """"01(1)(a)" -> "(a)": a part's own printed label, kept on the line it
    becomes once flattened into its parent part."""
    m = _LAST_LABEL.search(question_number or "")
    return m.group(1) if m else ""


# The largest whole number EdSider's integer columns hold.
_MAX_MINUTES = 2_147_483_647


def _minutes(value, unit):
    if value in (None, ""):
        return None
    return int(round(float(value) * MINUTES_IN.get(unit or "minutes", 1)))


def paper_title(doc, lists):
    """EdSider's naming: "Grade 12 - General Information Technology -
    First Term Test" - the paper's own title last, unless it already says
    all of it."""
    grade = _find(lists, "grade", doc.get("grade_id"))
    subject = _find(lists, "subject", doc.get("subject_id"))
    own = (doc.get("paper_title") or "").strip()
    pieces = []
    if grade and f"grade {grade['name']}".lower() not in own.lower():
        pieces.append(f"Grade {int(grade['name'])}" if grade["name"].isdigit() else grade["name"])
    if subject and subject["name"].lower() not in own.lower():
        pieces.append(subject["name"])
    if own:
        pieces.append(own)
    return " - ".join(pieces) or (doc.get("filename") or "")


def _code(doc, label, lists, exam_number, part_no, question_no):
    """The question title code, e.g. G12-GIT-EM-T2-EX02-P2-Q05: grade,
    subject, medium and term short codes, exam, part, question."""
    ids = {"grade": doc.get("grade_id"), "subject": doc.get("subject_id"),
           "language": label["medium_id"], "term": label["term_id"]}
    pieces = [(_find(lists, kind, ids[kind]) or {}).get("short_code")
              for kind in ("grade", "subject", "language", "term")]
    pieces = [p for p in pieces if p]
    if exam_number:
        pieces.append(f"EX{int(exam_number):02d}")
    pieces.append(f"P{part_no}")
    pieces.append(f"Q{question_no:02d}" if question_no is not None else "Q")
    return "-".join(pieces)


def _group_sections(sections):
    """Our parts as EdSider parts holding sections: "Part II - A" and
    "Part II - B" become one part "Part II" with two sections; any other
    part is a part with one section. A paper in one piece is one of each."""
    groups = []
    for s in sections or [{"label": None, "heading": None, "kind": None, "instructions": None}]:
        m = _SUB_PART.match(s.get("label") or "")
        base = m.group("base").strip() if m else (s.get("label") or "Part I")
        if m and groups and groups[-1]["title"] == base:
            groups[-1]["sections"].append(s)
        else:
            groups.append({"title": base, "sub": bool(m), "sections": [s]})
    return groups


# ----------------------------------------------------------------- build

def build_package(doc, questions, lists, people, images=None, image_src=None,
                  arrival_state=DEFAULT_ARRIVAL, exam_number=None):
    """Every EdSider row for one paper, plus the problems found on the way.

    doc        store.get_intake_document()
    questions  store.intake_questions()
    lists      store.edsider_lookups() - EdSider's own lists
    people     {"sender": staff row of whoever sends it, "data_entry": ...,
               "teacher": ...} - see store.document_people(); each with
               edsider_user_id / edsider_staff_id
    images     {question_id: [image row]} - store.question_images_for_document()
    image_src  image row -> the data: URL to embed (the preview passes a
               short stand-in, so it never loads every picture)

    Problems are {"level": "error" | "warning", "message", "question_id"};
    any error means the paper cannot be sent as it stands."""
    problems = []

    def problem(level, message, q=None):
        problems.append({"level": level, "message": message,
                         "question_id": q["id"] if q else None,
                         "question_number": q.get("question_number") if q else None})

    images = images or {}
    image_src = image_src or (lambda img: f"image:{img['id']}")
    settings = doc.get("assessment_settings") or {}
    labels = doc.get("edsider_labels") or {}
    label = {k: labels.get(k) for k in ("medium_id", "term_id", "year_id", "exam_category_id",
                                       "difficulty_id", "source_id", "author_id",
                                       "grading_method_id")}
    if arrival_state not in ARRIVAL_STATES:
        raise ValueError(f"not a state a paper can arrive in: {arrival_state!r}")

    for key, kind, what in (("grade_id", "grade", "grade"), ("subject_id", "subject", "subject")):
        if not _find(lists, kind, doc.get(key)):
            problem("error", f"The paper has no {what} from EdSider's list.")
    for key, what in (("medium_id", "medium"), ("exam_category_id", "exam category"),
                      ("grading_method_id", "grading method")):
        if label[key] is None:
            problem("error", f"Choose the paper's {what} (For EdSider).")

    sender = people.get("sender") or {}
    uid, staff_id = sender.get("edsider_user_id"), sender.get("edsider_staff_id")
    if uid is None:
        problem("error", "The person sending is not on EdSider's staff list, so EdSider has "
                         "no user to record as the paper's creator and chief assessor.")
    uid_text = str(uid) if uid is not None else None

    title = paper_title(doc, lists)
    if len(title) > TITLE_MAX:
        problem("error", f"The title is {len(title)} characters; EdSider allows {TITLE_MAX}.")
    if not exam_number:
        problem("warning", "No exam number - question codes are made without the EX part.")

    # ----------------------------------------------------- the paper
    groups = _group_sections(doc.get("sections"))
    notes = (doc.get("notes") or "").strip()
    assessment = {
        "title": title, "assessment_admin": uid_text, "created_by": uid_text,
        "proctor_requirement": False,
        "assessment_state": arrival_state, "status_id": ARRIVAL_STATES[arrival_state],
        "language_id": label["medium_id"], "difficulty_id": label["difficulty_id"],
        "category_id": None, "attempt_no_id": 1, "grade_id": doc.get("grade_id"),
        "author_id": label["author_id"], "chapter_id": None,
        "subject_id": doc.get("subject_id"), "term_id": label["term_id"],
        "year_id": label["year_id"], "privilege_id": 1,
        "exam_category_id": label["exam_category_id"], "source_id": label["source_id"],
        # The paper's general instructions go on its first part, where
        # EdSider shows instructions; this stays for anything else.
        "additional_info": "", "template_id": None, "no_of_parts": len(groups),
        "part_notation_id": NOTATION_IDS.get(settings.get("part_notation")),
        "is_restart": bool(settings.get("restart_option")),
        "is_pause": bool(settings.get("pause_option")), "pause_until": 0,
        "is_multiple": bool(settings.get("multiple_papers")),
        "grading_method_id": label["grading_method_id"],
        # EdSider's default terms for the paper's medium
        # (assessment_term_condition), read when it is sent.
        "terms_content": None,
    }

    if not settings.get("schedule_at"):
        problem("error", "Set the assessment date and time (Assessment settings) - "
                         "EdSider requires a start time.")
    minutes_unit = next((u["id"] for u in lists.get("time_unit", []) if u["name"] == "minutes"), 1)
    schedule = {
        "start_time": settings.get("schedule_at"),
        "exam_time_interval": _minutes(settings.get("duration_value"), settings.get("duration_unit")),
        "exam_time_interval_unit_id": minutes_unit,
        "buffer_time": _minutes(settings.get("access_buffer_value"),
                                settings.get("access_buffer_unit")),
        "buffer_time_unit_id": minutes_unit,
        "is_set_time_limit": bool(settings.get("time_limit")),
        "scheduled_by": uid_text,
    }
    if settings.get("time_limit") and schedule["exam_time_interval"] is None:
        problem("error", "The paper has a time limit but no duration.")
    # EdSider keeps both as whole minutes in an integer column; a figure
    # outside it fails the whole send (paper #55's buffer of 6,000,000,000,
    # typed in Assessment settings) - caught here, named, before writing.
    for column, setting in (("exam_time_interval", "duration"),
                            ("buffer_time", "access buffer time")):
        minutes = schedule[column]
        if minutes is not None and not 0 <= minutes <= _MAX_MINUTES:
            problem("error", f"The {setting} in Assessment settings is {minutes:,} minutes; "
                             f"EdSider takes 0 to {_MAX_MINUTES:,}. Correct it there.")
    assessor = {"user_role_id": f"CASS{uid_text}" if uid_text else None, "role_code": "CASS",
                "user_id": uid_text, "assigned_by": uid_text, "status": "Active"}

    # --------------------------------------------- parts and sections
    # Marks per question and "answer N of M": the marks plan data entry
    # saved, else what the paper's own text says (extract.part_marks -
    # "Each question carries 15 marks", "Answer FOUR questions only").
    plan = (settings.get("marking") or {}).get("parts") or {}
    stated = {m["label"]: m for m in extract.part_marks(doc.get("sections"))}
    by_parent = {}
    for q in questions:
        by_parent.setdefault(q["parent_seq"], []).append(q)
    top = [q for q in questions if q["parent_seq"] is None]

    # Each part names EdSider's own part for the paper's notation and its
    # place - Roman's second is "II" (assessment.part 8). EdSider's
    # paper-details screen skips a part without one; its own papers set it
    # whenever the paper has a notation.
    notation_id = NOTATION_IDS.get(settings.get("part_notation"))
    part_ids = {}
    for p in lists.get("part", []):
        if notation_id and (p.get("extra") or {}).get("notation_id") == notation_id:
            part_ids.setdefault(len(part_ids) + 1, p["id"])

    parts, section_of_label = [], {}
    for pi, group in enumerate(groups, 1):
        if notation_id and pi not in part_ids:
            problem("error", f"EdSider has no part {pi} in this paper's part notation.")
        part = {"key": f"part{pi}", "row": {
            "part_id": part_ids.get(pi), "part_title": group["title"], "display_order": pi,
            "no_of_sections": len(group["sections"]), "created_by": uid,
            "allow_instruction": False, "part_instruction": None}, "sections": []}
        if pi == 1 and notes:
            part["row"].update(allow_instruction=True, part_instruction=notes)
        for si, s in enumerate(group["sections"], 1):
            sec_key = f"sec{pi}_{si}"
            here = [q for q in top if (q.get("section") or None) == (s.get("label") or None)]
            planned = plan.get(s.get("label") if s.get("label") is not None else "_") or {}
            read = stated.get(s.get("label")) or {}
            to_answer = planned.get("answer_count")
            if to_answer is None:
                to_answer = read.get("answer_count")
            each = planned.get("marks_each")
            if each is None and read.get("marks_each") is not None:
                each = read["marks_each"]
                if read.get("assumed_marks"):
                    problem("warning", f"{s.get('label') or 'The paper'}: no marks are printed for "
                                       "its MCQs, so 1 mark each is used.")
            optional = to_answer is not None and 0 < to_answer < len(here)
            title_text = (s.get("label") if group["sub"]
                          else KIND_TITLE.get(s.get("kind")) or s.get("heading") or group["title"])
            part["sections"].append({"key": sec_key, "row": {
                "section_title": title_text, "notation_id": None, "display_order": si,
                "no_of_questions": len(here), "status_id": 1, "created_by": uid,
                "allow_optional_questions": optional,
                "no_of_required_questions": 0 if optional else None,
                "no_of_questions_to_answer": to_answer if optional else None,
                "allow_instruction": bool(s.get("instructions")),
                "section_instruction": s.get("instructions")}})
            section_of_label[s.get("label") or None] = (sec_key, pi, each)
        parts.append(part)

    first_section = parts[0]["sections"][0]["key"]
    first_each = section_of_label.get(next(iter(section_of_label), None), (None, None, None))[2]

    # ------------------------------------------------------ questions
    common = {"grade_id": doc.get("grade_id"), "subject_id": doc.get("subject_id"),
              "language_id": label["medium_id"], "term_id": label["term_id"],
              "year_id": label["year_id"], "author_id": label["author_id"],
              "source_id": label["source_id"], "difficulty_id": label["difficulty_id"],
              "privilege_id": 1, "category_id": label["exam_category_id"],
              "chapter_id": None, "tags": "", "created_by": uid_text,
              "is_content_blob": "false", "is_answer_blob": "false", "is_passage": False,
              "guidence": draft_json([draft_doc("", "guidance")])}

    def pictures(q, slot):
        return [(image_src(img), dict(img).get("label")) for img in images.get(q["id"], [])
                if img["slot"] == slot]

    rows, links = [], []
    numbers_seen = {}
    for q in top:
        sec_key, part_no, each = section_of_label.get(q.get("section") or None, (None, None, None))
        if sec_key is None:
            sec_key, part_no, each = first_section, 1, first_each
            problem("warning", "This question is in no part of the paper; it goes in the first section.", q)
        number = _question_no(q.get("question_number"))
        if number is None:
            number = numbers_seen.get(sec_key, 0) + 1
            problem("warning", "No question number could be read; numbered in order.", q)
        if number in numbers_seen.get(f"{sec_key}#all", set()):
            problem("warning", f"Question number {number} appears twice in one section.", q)
        numbers_seen.setdefault(f"{sec_key}#all", set()).add(number)
        numbers_seen[sec_key] = max(numbers_seen.get(sec_key, 0), number)
        code = _code(doc, label, lists, exam_number, part_no, number)
        kids = by_parent.get(q["seq"], [])

        if not kids:
            row = _single_question(q, code, common, pictures, problem)
            rows.append(row)
            marks = q.get("marks") if q.get("marks") is not None else each
            if marks is None:
                problem("error", "No marks - set them on the question or for its part.", q)
            links.append(_link(row["key"], sec_key, number, marks, uid_text))
            continue

        child_keys = []
        child_marks_total = 0
        for child in kids:
            row, marks = _part_row(child, by_parent, code, common, pictures, problem)
            rows.append(row)
            child_keys.append(row["key"])
            if marks is None:
                problem("error", "No marks on this part.", child)
            else:
                child_marks_total += marks
            links.append(_link(row["key"], sec_key, number, marks, uid_text))
        if q.get("marks") is not None and child_marks_total and q["marks"] != child_marks_total:
            problem("warning", f"The question says {q['marks']} marks but its parts add up to "
                               f"{child_marks_total}; the parts' marks are sent.", q)
        if len(child_keys) > 18:
            problem("warning", "More than 18 parts - check EdSider's list of part ids fits.", q)
        parent = dict(common, key=f"q{q['id']}", source_question_id=q["id"], title=code, qtype=5,
                      content=draft_json([draft_doc(q.get("text"), f"q{q['id']}",
                                                    pictures(q, "question"))]),
                      answer="0", answer_type_id=None, is_subquestion=None,
                      sub_question_keys=child_keys, passage_key=None)
        rows.append(parent)
        # The parent's own link comes after its parts', as EdSider saves them.
        links.append(_link(parent["key"], sec_key, number, 0, uid_text))

    # ------------------------------------------------------- passages
    passages = []
    by_key = {r["key"]: r for r in rows}
    for gi, g in enumerate(doc.get("instruction_groups") or [], 1):
        sec_key = section_of_label.get(g.get("section") or None, (first_section,))[0]
        lo, hi = _question_no(str(g.get("from"))), _question_no(str(g.get("to")))
        key = f"psg{gi}"
        covered = []
        for link in links:
            if link["section_key"] == sec_key and lo is not None and hi is not None \
                    and lo <= link["row"]["question_number"] <= hi:
                by_key[link["question_key"]]["passage_key"] = key
                covered.append(link["question_key"])
        span = f"Q{g.get('from')} - Q{g.get('to')}"
        passages.append({"key": key, "question_keys": covered, "row": {
            # Written as "ASMNT<assessment id> Q28 - Q31", EdSider's own form.
            "title": span, "description": span,
            "content": draft_json([draft_doc(g.get("text"), key)]),
            "passage_type_id": 1, "status_id": 1, "created_by": staff_id}})
        if not covered:
            problem("warning", f"The instructions for {span} match no question.")

    # ---------------------------------------- who did the work, in EdSider
    operation = review = None
    entry = people.get("data_entry")
    if entry and entry.get("to_staff_id"):
        operation = {"employee_id": entry["to_staff_id"], "operation_id": OPERATION_ID,
                     "operation_completed": True,
                     "assign_by": entry.get("by_staff_id") or staff_id}
    elif entry:
        problem("warning", f"{entry['assigned_to']} did the data entry but is not on EdSider's "
                           "staff list, so EdSider will not show who did it.")
    # A paper is reviewed by two teachers - one review row each.
    reviews = []
    for teacher in people.get("teachers") or ([people["teacher"]] if people.get("teacher") else []):
        if teacher.get("to_staff_id"):
            reviews.append({"teacher_id": teacher["to_staff_id"], "status_id": REVIEW_COMPLETED,
                            "assign_by": teacher.get("by_staff_id") or staff_id})
        else:
            problem("warning", f"{teacher['assigned_to']} reviewed the paper but is not on "
                               "EdSider's staff list, so EdSider will not show who reviewed it.")
    review = reviews[0] if reviews else None

    if not rows:
        problem("error", "The paper has no questions.")

    marks_by_section = {}
    for link in links:
        marks_by_section[link["section_key"]] = (marks_by_section.get(link["section_key"], 0)
                                                 + (link["row"]["mark_allocation"] or 0))
    for part in parts:
        for sec in part["sections"]:
            sec["marks"] = marks_by_section.get(sec["key"], 0)

    return {
        "assessment": assessment, "schedule": schedule, "assessor": assessor,
        "parts": parts, "passages": passages, "questions": rows, "links": links,
        "operation": operation, "review": review, "reviews": reviews, "problems": problems,
        "summary": {
            "title": title, "arrival_state": arrival_state, "parts": len(parts),
            "sections": sum(len(p["sections"]) for p in parts),
            "questions": len(top), "rows": len(rows), "links": len(links),
            "passages": len(passages), "marks": sum(marks_by_section.values()),
            "errors": sum(1 for p in problems if p["level"] == "error"),
            "warnings": sum(1 for p in problems if p["level"] == "warning"),
        },
    }


def _link(question_key, section_key, number, marks, uid_text):
    return {"question_key": question_key, "section_key": section_key, "row": {
        "added_by": f"CASS{uid_text}" if uid_text else None,
        "mark_allocation": float(marks) if marks is not None else None,
        "question_number": number, "is_optional": False}}


def _single_question(q, code, common, pictures, problem):
    """A whole question with no parts: an MCQ (qtype 1) or anything else
    (qtype 2 - descriptive and fill-in-the-blank, as EdSider stores both)."""
    text = q.get("text") or ""
    has_parts = False
    kind = q.get("question_type") or extract.detect_question_type(text, has_parts)
    seed = f"q{q['id']}"
    row = dict(common, key=seed, source_question_id=q["id"], title=code,
               is_subquestion=None, sub_question_keys=None, passage_key=None)
    if kind == "mcq":
        stem, options = extract.split_mcq_options(text)
        if not options:
            problem("error", "Marked as MCQ, but no options were found in its text.", q)
        else:
            labels = extract.mcq_correct_labels(q.get("answer"))
            correct = [i for i, o in enumerate(options) if o["label"].lower() in labels]
            if not correct:
                problem("error", "No correct option is marked.", q)
            docs = [draft_doc(stem, seed, pictures(q, "question"))]
            for i, o in enumerate(options):
                docs.append(draft_doc(o["text"], f"{seed}o{i}", pictures(q, f"option:{o['label']}")))
            return dict(row, qtype=1, content=draft_json(docs),
                        answer=mcq_weights(len(options), correct),
                        answer_type_id=2 if len(correct) > 1 else 1)
    return dict(row, qtype=2,
                content=draft_json([draft_doc(text, seed, pictures(q, "question"))]),
                answer=draft_json([draft_doc(q.get("answer"), seed + "a", pictures(q, "answer"))]),
                answer_type_id=None)


def _part_row(child, by_parent, code, common, pictures, problem):
    """One part of a question, as an EdSider sub-question. Its own parts
    ((i), (ii) ...) become numbered lines inside it - EdSider has one level
    - with their marks added up to the part's. Returns (row, marks)."""
    seed = f"q{child['id']}"
    text_lines = [child.get("text") or ""]
    answer_lines = [child.get("answer") or ""]
    marks = child.get("marks")
    grand = by_parent.get(child["seq"], [])
    if grand:
        problem("warning", "This part has parts of its own; EdSider keeps one level, so they "
                           "become numbered lines inside it.", child)
        sub_total, missing = 0, False
        for g in grand:
            lead = _part_label(g.get("question_number"))
            text = g.get("text") or ""
            # Each line keeps its own marks visible once they are added up
            # into the part's, unless the paper's text already prints them.
            if g.get("marks") is not None and not _PRINTED_MARKS.search(text):
                text += f" ({g['marks']} marks)"
            text_lines.append(f"{lead} {text}".strip())
            if g.get("answer"):
                answer_lines.append(f"{lead} {g['answer']}".strip())
            if g.get("marks") is None:
                missing = True
            else:
                sub_total += g["marks"]
        if not missing:
            # What a teacher marks is the lines, so their total is the part's.
            if marks is not None and marks != sub_total:
                problem("warning", f"This part says {marks} marks but its lines add up to "
                                   f"{sub_total}; {sub_total} is sent.", child)
            marks = sub_total
    if (child.get("question_type") or extract.detect_question_type(child.get("text"), bool(grand))) \
            == "mcq":
        problem("warning", "An MCQ inside a question's parts goes to EdSider as text - its "
                           "parts cannot be MCQs.", child)
    row = dict(common, key=seed, source_question_id=child["id"], title=code, qtype=5,
               content=draft_json([draft_doc("\n".join(text_lines), seed,
                                             pictures(child, "question"))]),
               answer=draft_json([draft_doc("\n".join(a for a in answer_lines if a), seed + "a",
                                            pictures(child, "answer"))]),
               answer_type_id=None, is_subquestion="true", sub_question_keys=None,
               passage_key=None)
    return row, marks
