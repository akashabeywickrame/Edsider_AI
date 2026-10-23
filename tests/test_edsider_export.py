"""A finished paper as EdSider's rows (src/edsider/export.py). The shapes
asserted here are EdSider's own, read off its live tables: paper #2141's
MCQ answer "0,100,0,0", a parent linked at 0 marks with its parts carrying
the marks under one question number, Draft.js text, "CASS<user id>"."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import store
from src.edsider import export
import service

LISTS = {
    "grade": [{"id": 12, "name": "12", "short_code": "G12", "extra": None}],
    "subject": [{"id": 14, "name": "General Information Technology", "short_code": "GIT",
                 "extra": None}],
    "language": [{"id": 1, "name": "English", "short_code": "EM", "extra": None}],
    "term": [{"id": 2, "name": "Second Term Test", "short_code": "T2", "extra": None}],
    "exam_category": [{"id": 1, "name": "Model - Term", "short_code": "MT", "extra": None}],
    "grading_method": [{"id": 3, "name": "Auto and Self Grading", "short_code": None,
                        "extra": None}],
    "time_unit": [{"id": 1, "name": "minutes", "short_code": None, "extra": None}],
}

SECTIONS = [
    {"label": "Part I", "heading": "Part I", "kind": "mcq",
     "instructions": "Answer all questions.", "marks_each": None, "questions": 2},
    {"label": "Part II", "heading": "Part II", "kind": "structured",
     "instructions": "Answer any one question.", "marks_each": None, "questions": 2},
]


def q(id_, seq, number, text, section, parent=None, depth=0, marks=None, answer=None,
      question_type=None):
    return {"id": id_, "seq": seq, "parent_seq": parent, "depth": depth,
            "question_number": number, "text": text, "section": section, "marks": marks,
            "answer": answer, "question_type": question_type}


QUESTIONS = [
    q(1, 1, "1", "Which device is an output device?\n(1) Mouse\n(2) Printer\n(3) Scanner\n"
                 "(4) Keyboard", "Part I", answer="(2)"),
    q(2, 2, "2", "Which are storage devices?\n(1) Hard disk\n(2) Monitor\n(3) SSD\n(4) Speaker",
      "Part I", answer="1, 3"),
    q(3, 3, "01", "A school library uses a computerized system.", "Part II"),
    q(4, 4, "01(1)", "Answer the following about the system:", "Part II", parent=3, depth=1),
    q(5, 5, "01(1)(a)", "Define the term system.", "Part II", parent=4, depth=2, marks=2,
      answer="An organized collection of parts."),
    q(6, 6, "01(1)(b)", "Draw a block diagram. [3]", "Part II", parent=4, depth=2, marks=3),
    q(7, 7, "01(2)", "Name two input devices used at the counter.", "Part II", parent=3,
      depth=1, marks=4),
    q(8, 8, "02", "Explain what an operating system does.", "Part II", marks=9,
      answer="It manages hardware."),
]

DOC = {
    "id": 9, "filename": "GIT.pdf", "paper_title": "Term 2 - Exam 02", "grade_id": 12,
    "subject_id": 14, "notes": "Use the reading time to go through the paper.",
    "sections": SECTIONS,
    "instruction_groups": [{"section": "Part I", "from": "1", "to": "2", "title": None,
                            "text": "Questions 1 and 2 are about devices."}],
    "edsider_labels": {"medium_id": 1, "term_id": 2, "exam_category_id": 1,
                       "grading_method_id": 3},
    "assessment_settings": {"schedule_at": "2026-11-02T09:30", "time_limit": True,
                            "duration_value": 2, "duration_unit": "hours",
                            "part_notation": "roman",
                            "marking": {"parts": {"Part II": {"answer_count": 1}}}},
}

PEOPLE = {
    "sender": {"edsider_user_id": 314, "edsider_staff_id": 278},
    "data_entry": {"assigned_to": "op@gmail.com", "to_staff_id": 281, "by_staff_id": 278},
    "teacher": {"assigned_to": "t@gmail.com", "to_staff_id": 256, "by_staff_id": 240},
}


def test_draft_documents_are_edsiders_shape():
    doc = export.draft_doc("\n\nFirst line\n\n\n\nSecond line\n", "s", images=["data:x"])
    texts = [(b["type"], b["text"]) for b in doc["blocks"]]
    assert texts == [("unstyled", "First line"), ("unstyled", ""), ("unstyled", "Second line"),
                     ("atomic", " ")]
    assert doc["blocks"][-1]["entityRanges"] == [{"offset": 0, "length": 1, "key": 0}]
    assert doc["entityMap"]["0"] == {"type": "IMAGE", "mutability": "MUTABLE",
                                     "data": {"src": "data:x"}}
    assert export.draft_json([doc]).startswith('[{"blocks":[{"key":')


def test_mcq_answers_are_weights_adding_to_100():
    assert export.mcq_weights(4, [1]) == "0,100,0,0"
    assert export.mcq_weights(4, [0, 2]) == "50,0,50,0"
    assert export.mcq_weights(4, [0, 1, 3]) == "34,33,0,33"


def test_a_finished_paper_becomes_edsiders_rows():
    pkg = export.build_package(DOC, QUESTIONS, LISTS, PEOPLE, exam_number=2)
    assert [p["level"] for p in pkg["problems"] if p["level"] == "error"] == []

    a = pkg["assessment"]
    assert a["title"] == "Grade 12 - General Information Technology - Term 2 - Exam 02"
    assert (a["created_by"], a["assessment_admin"], a["assessment_state"], a["status_id"]) == (
        "314", "314", "ReviewCompleted", 8)
    assert (a["language_id"], a["grading_method_id"], a["part_notation_id"], a["no_of_parts"]) == (
        1, 3, 3, 2)
    assert pkg["schedule"]["exam_time_interval"] == 120          # 2 hours, in minutes
    assert pkg["assessor"]["user_role_id"] == "CASS314"

    part1, part2 = pkg["parts"]
    assert part1["row"]["part_instruction"] == DOC["notes"]
    assert part1["sections"][0]["row"]["section_title"] == "Multiple Choice Questions"
    s2 = part2["sections"][0]["row"]
    assert (s2["allow_optional_questions"], s2["no_of_questions_to_answer"]) == (True, 1)

    rows = {r["key"]: r for r in pkg["questions"]}
    mcq, two = rows["q1"], rows["q2"]
    assert (mcq["qtype"], mcq["answer"], mcq["answer_type_id"]) == (1, "0,100,0,0", 1)
    assert (two["answer"], two["answer_type_id"]) == ("50,0,50,0", 2)
    assert [d["blocks"][0]["text"] for d in json.loads(mcq["content"])] == [
        "Which device is an output device?", "Mouse", "Printer", "Scanner", "Keyboard"]
    assert mcq["title"] == "G12-GIT-EM-T2-EX02-P1-Q01"

    # Question 01: its parts are written before it, the parent lists them.
    order = [r["key"] for r in pkg["questions"]]
    assert order.index("q4") < order.index("q3") and order.index("q7") < order.index("q3")
    assert rows["q3"]["sub_question_keys"] == ["q4", "q7"]
    assert (rows["q3"]["qtype"], rows["q3"]["answer"]) == (5, "0")
    assert rows["q4"]["is_subquestion"] == "true"
    # (a) and (b) become lines inside (1), their marks its marks.
    lines = [b["text"] for b in json.loads(rows["q4"]["content"])[0]["blocks"]]
    assert lines == ["Answer the following about the system:",
                     "(a) Define the term system. (2 marks)", "(b) Draw a block diagram. [3]"]

    links = {l["question_key"]: l["row"] for l in pkg["links"]}
    assert (links["q3"]["mark_allocation"], links["q4"]["mark_allocation"],
            links["q7"]["mark_allocation"]) == (0.0, 5.0, 4.0)
    assert {links[k]["question_number"] for k in ("q3", "q4", "q7")} == {1}
    assert links["q8"]["question_number"] == 2
    assert links["q1"]["mark_allocation"] == 1.0                # MCQ: 1 mark each
    assert all(l["added_by"] == "CASS314" for l in links.values())

    assert pkg["passages"][0]["question_keys"] == ["q1", "q2"]
    assert rows["q1"]["passage_key"] == "psg1"
    assert pkg["operation"] == {"employee_id": 281, "operation_id": 9,
                                "operation_completed": True, "assign_by": 278}
    assert pkg["review"] == {"teacher_id": 256, "status_id": 8, "assign_by": 240}
    assert pkg["summary"]["marks"] == 2 + 5 + 4 + 9


def test_what_edsider_requires_is_reported_before_anything_is_sent():
    doc = dict(DOC, edsider_labels={}, assessment_settings={}, subject_id=None)
    questions = [dict(QUESTIONS[0], answer=None), dict(QUESTIONS[7], marks=None)]
    pkg = export.build_package(doc, questions, LISTS, {"sender": {}})
    errors = " | ".join(p["message"] for p in pkg["problems"] if p["level"] == "error")
    for needed in ("subject", "medium", "exam category", "grading method", "start time",
                   "not on EdSider's staff list", "No correct option", "No marks"):
        assert needed in errors, needed


def test_the_preview_route_names_the_sender_and_reads_the_stored_paper(local_db):
    with store.connect() as conn:
        store.sync_edsider_teachers(conn, [{"email": "admin@gmail.com", "name": "Admin",
                                            "roles": ["MASAD", "TEACH"], "user_id": 314,
                                            "staff_id": 278}])
        doc_id = store.create_intake_document(conn, "GIT.pdf", paper_grade="G12",
                                              paper_subject="General Information Technology",
                                              sections=SECTIONS)
        store.add_extracted_questions(conn, doc_id, [dict(x) for x in QUESTIONS])
    out = service.edsider_preview(doc_id, as_email="admin@gmail.com")
    assert out.package["assessment"]["created_by"] == "314"
    assert out.summary["questions"] == 4 and out.summary["rows"] == 6
    assert any("medium" in p["message"] for p in out.problems)
