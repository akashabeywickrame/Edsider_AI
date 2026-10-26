"""Worked-solutions papers - a question, then "Solution" and the working.

The real Grade 13 Combined Maths "Full Solutions Guide" (intake uploads
#84 Word, #85 PDF) numbers its working "1. Base Case", "2. Assumption",
"3. Inductive Step". Left in the text, those steps were split off as
questions and read as MCQ options; the solution is the answer instead.
The text below is the paper's own lines, its working cut short.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import store
from src.intake import extract
import service

PURE_MATHS = """Subject - Combined Mathematics - I
(Pure Mathematics)
Grade - 13
Term – 1st
Paper – 1
Full
Solutions
Guide
(English
Medium)
SHORT QUESTIONS (ANSWER ALL)
Question 01 (25 Marks)
Prove using mathematical induction that for all n ∈ Z⁺: 1² + 2² + 3² + ... + n² = [n(n+1)(2n+1)] / 6
Solution:
1. Base Case (n=1):
LHS = 1² = 1
2. Assumption: Assume true for n=k.
1² + 2² + ... + k² = [k(k+1)(2k+1)] / 6
3. Inductive Step (n=k+1):
By mathematical induction, result holds ∀ n ∈ Z⁺.
Question 02
Solve for x ∈ R: |2x - 3| ≤ |x + 3|
Solution:
Square both sides:
3x(x - 6) ≤ 0
Solution: 0 ≤ x ≤ 6
Question 3
Find the value of the following limit: lim (x→3) (x² − 9)/(√(x + 1) − 2)
Solution
1. Identify the Indeterminate Form
2. Rationalize the Denominator
= 24
Part 2
Answer any 5 questions.
Part B - Question 01
Problem:
Let b, c (≠ 0) be real numbers. Show that 0 is not a root of the equation x² - bx + c = 0.
Solution
1. Show that x = 0 is not a root
However, the question states that c ≠ 0.
Question 02 (a)
Problem:
1. Sketch the graphs of y = 2|x + 1| - 3 and y = x + 2|x - 1| on the same coordinate plane.
2. Using the graphs, solve the equation: x + 2|x - 1| = 2|x + 1| - 3.
Solution Sketch
Graph 1: y = 2|x + 1| - 3 is a 'V' shaped graph with its vertex at (-1, -3).
Question 02 (b)
Problem:
If a = log2n n, b = log3n 2n, and c = log4n 3n, prove that 1 + abc = 2bc.
Solution
1. Use the Change of Base Formula:
Since LHS = RHS, the identity is proven.
Part B – Question 05
(a) Definite Integral with Substitution
Problem: Prove that: ∫₀¹ (1 + xe^√(1−x²))/√(1−x²) dx = (2e + π − 2)/2
Solution
1. Split the Integral:
(Proved)
 (b) Integration using t = tan θ substitution
Problem: Using the substitution t = tan θ, prove the integral from 0 to π/4 equals (5π + 2 ln(18/25))/104.
(c) Reduction Formula for sinⁿ θ
Problem: If In = ∫ sinⁿ θ dθ from 0 to π/2, show that In = (n-1)/n In-2
1. Understand the Integral:
2. Term 1: ∫ sin θ cos² θ dθ.
3. Term 2: I4 = ∫ sin⁴ θ dθ.
4. Final Sum:
Part B - Question 06
(a) Quadratic Equations and Roots
Problem: Let the roots of the equation x² + (1/a)x + b = 0 be α and β.
1. Show that k satisfies the equation bk² - kb - 2b + 1 = 0.
2. Hence, prove that the value of b cannot lie between 0 and 4/9.
Solution:
3. Substitution:
(1 + k)( −1/
a) = -a
1 + k = a²
(b) Polynomials and Remainder Theorem
Problem:
Let f(x) = x³ - x² - x + λ. It is given that (x - λ) is a factor of f(x).
Solution Sketch:
Since (x - λ) is a factor, f(λ) = 0.
"""


def _by_number(parsed, section):
    return {q["question_number"]: q for q in parsed["questions"] if q["section"] == section}


def test_the_working_is_the_answer_and_its_steps_are_not_questions():
    parsed = extract.structure(PURE_MATHS)
    part1 = _by_number(parsed, "Part I")
    # "Question 01", "02", then "3": a line saying "Question" is a question
    # however it is padded.
    assert list(part1) == ["01", "02", "3"]
    q1 = part1["01"]
    assert q1["text"].startswith("Prove using mathematical induction")
    assert "Base Case" not in q1["text"]
    assert q1["answer"].startswith("1. Base Case (n=1):")
    assert q1["answer"].endswith("result holds ∀ n ∈ Z⁺.")
    assert q1["marks"] == 25
    # "Solution: 0 ≤ x ≤ 6" inside the working is still the working.
    assert part1["02"]["answer"].endswith("Solution: 0 ≤ x ≤ 6")
    assert part1["3"]["answer"].endswith("= 24")


def test_a_stray_solutions_line_in_the_header_is_not_a_solution():
    """The PDF's header grid breaks "Full Solutions Guide" over three
    lines. Nothing before the first question is a solution."""
    parsed = extract.structure(PURE_MATHS)
    everything = " ".join([parsed["sections"][0]["instructions"] or "",
                           parsed["paper_instructions"] or ""])
    assert "Guide" in everything and "SHORT QUESTIONS" in everything
    assert not any("Guide" in (q["answer"] or "") for q in parsed["questions"])


def test_question_headings_that_name_a_part_become_parts():
    parsed = extract.structure(PURE_MATHS)
    part_b = _by_number(parsed, "Part B")
    # "Part B - Question 01": a part heading and a question on one line.
    assert part_b["01"]["text"].startswith("Let b, c")
    assert part_b["01"]["answer"].startswith("1. Show that x = 0")
    # "Question 02 (a)" / "Question 02 (b)": question 02's parts (a), (b).
    assert part_b["02(a)"]["parent_seq"] == part_b["02"]["seq"]
    assert part_b["02(a)"]["text"].startswith("1. Sketch the graphs")
    assert part_b["02(a)"]["answer"].startswith("Graph 1:")
    assert part_b["02(b)"]["answer"].endswith("the identity is proven.")
    # "Problem:" is a label, like "Solution".
    assert not any(q["text"].startswith("Problem") for q in parsed["questions"])


def test_working_broken_over_lines_does_not_end_the_solution():
    """"a) = -a" is the end of "(1 + k)(-1/a) = -a", broken by the PDF
    reader - not part (a), which question 06 has printed already."""
    part_b = _by_number(extract.structure(PURE_MATHS), "Part B")
    assert part_b["06(a)"]["answer"].endswith("1 + k = a²")
    assert "a) = -a" not in part_b["06(a)"]["text"]
    assert part_b["06(b)"]["answer"] == "Since (x - λ) is a factor, f(λ) = 0."


def test_numbered_working_with_no_solution_line_is_not_an_mcq():
    """Question 05(c) prints its working with no "Solution" line; four
    numbered lines read as four options. On a worked-solutions paper they
    are working, and the stored type says so."""
    part_b = _by_number(extract.structure(PURE_MATHS), "Part B")
    # The solution of (a) ends at the paper's own " (b)".
    assert part_b["05(a)"]["answer"] == "1. Split the Integral:\n(Proved)"
    assert part_b["05(b)"]["text"].startswith("Integration using t = tan θ")
    q = part_b["05(c)"]
    assert extract.detect_question_type(q["text"]) == "mcq"   # by text alone
    assert q["question_type"] == "descriptive"
    # The API then sends no options for it.
    out = service._question_out({**q, "id": 1, "document_id": 1, "reviewed": False,
                                 "teacher_reviewed": False})
    assert out.mcq is None and out.question_type == "descriptive"


def test_a_real_option_list_on_a_worked_solutions_paper_stays_an_mcq():
    text = (
        "Question 1\n"
        "Which of the following is the derivative of x²?\n"
        "(1) x  (2) 2x  (3) x²  (4) 2\n"
        "Solution\n"
        "d/dx x² = 2x, so the answer is (2).\n"
        "Question 2\n"
        "Find the value of 3 + 4.\n"
        "Solution\n"
        "7\n"
    )
    q1, q2 = extract.structure(text, style="question_word")["questions"]
    assert extract.detect_question_type(q1["text"]) == "mcq"
    assert q1.get("question_type") is None
    assert q1["answer"].startswith("d/dx x²")
    assert (q2["text"], q2["answer"]) == ("Find the value of 3 + 4.", "7")


def test_the_older_word_paper_labels_its_working_answer():
    """The older Combined Maths .docx: "Question: Vectors" heads a
    question, "Question (b)" is its part (b), "Answer" its working - and
    the working's own "(i)" / "(ii)" restart under parts (i) and (ii)."""
    text = (
        "2. Question\n"
        "A fort is situated on top of a hill at a height h above sea level.\n"
        "Answer\n"
        "1. Maximum Horizontal Range Formula\n"
        "(Proved)\n"
        "Question: Vectors\n"
        "What is a vector? If a is a vector, define the vector λa.\n"
        "Answer\n"
        "1. Definitions\n"
        "Vector: A quantity that has both magnitude and direction.\n"
        "Question (b)\n"
        "A1, A2, A3 and A4 are four fixed points in a plane.\n"
        "(i) If the magnitude of R is constant, show that the locus of P is a circle.\n"
        "(ii) If the direction of R is constant, show that the locus of P is a straight line.\n"
        "Answer\n"
        "(i) When the magnitude of R is constant:\n"
        "|R| = constant\n"
        "(ii) When the direction of R is constant:\n"
        "R is parallel to a fixed line.\n"
    )
    qs = extract.structure(text, style="question_word")["questions"]
    by_text = {q["text"].split("\n")[0][:20]: q for q in qs}
    assert by_text["A fort is situated o"]["answer"].endswith("(Proved)")
    vectors = by_text["Vectors"]
    assert vectors["answer"].startswith("1. Definitions")
    assert "A1, A2, A3" in vectors["text"]            # part (b) stays with it
    last = qs[-1]
    assert last["text"].startswith("If the direction of R is constant")
    assert last["answer"].startswith("(i) When the magnitude")
    assert last["answer"].endswith("parallel to a fixed line.")


def test_a_paper_with_no_solution_line_is_left_alone():
    text = "1. Define a system.\n2. Name two input devices.\n"
    assert extract._take_out_solutions(text) == (text, [])
    assert [q["text"] for q in extract.structure(text)["questions"]] == [
        "Define a system.", "Name two input devices."]


def test_a_stored_type_is_kept_and_decides_what_is_shown(local_db):
    # Both parts have a question "01"; Part I's is the induction proof.
    drafts = [q for q in extract.structure(PURE_MATHS)["questions"]
              if (q["section"], q["question_number"]) in (
                  ("Part I", "01"), ("Part B", "05"), ("Part B", "05(c)"))]
    with store.connect() as conn:
        doc_id = store.create_intake_document(conn, "maths.pdf")
        store.add_extracted_questions(conn, doc_id, drafts)
        stored = {q["question_number"]: q for q in store.intake_questions(conn, doc_id)}
    assert stored["05(c)"]["question_type"] == "descriptive"
    assert stored["01"]["question_type"] is None
    assert stored["01"]["answer"].startswith("1. Base Case")
