"""Instructions at every level a paper prints them: the whole paper (its
opening text), and a group of questions ("answer 20 to 25 using the chart
below") - see intake.extract.structure() and _split_group_instruction.
The examples are the shapes found on the real uploads; the ranges that
must NOT become group instructions are real too (see the rule's notes)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.intake import extract
import service
from service import DocumentUpdate, PastedPaperIn, create_document_from_text, update_document


def _mcq(n, stem):
    return f"{n}. {stem}\n(1) one\n(2) two\n(3) three\n(4) four\n"


PAPER = (
    "Grade 12 Business Studies - Term 1\nTime: 2 hours\n"
    "Answer all the questions.\n\n"
    + "".join(_mcq(n, f"Question {n} asks something about business, in enough words.")
              for n in range(17, 20))
    + "Choose the correct answer by reading the below 20 to 25 questions.\n"
    + "".join(_mcq(n, f"Question {n} asks about the passage, in enough words.") for n in range(20, 26))
)


def test_a_group_instruction_is_taken_off_the_question_before_it():
    parsed = extract.structure(PAPER, style="number")
    assert parsed["instruction_groups"] == [{
        "section": None, "from": "20", "to": "25",
        "text": "Choose the correct answer by reading the below 20 to 25 questions."}]
    q19 = next(q for q in parsed["questions"] if q["question_number"] == "19")
    assert "Choose the correct answer" not in q19["text"]
    # Question 19 keeps its own four options, nothing glued on.
    assert len(extract.split_mcq_options(q19["text"])[1]) == 4


def test_the_papers_opening_text_becomes_its_instructions():
    parsed = extract.structure(PAPER, style="number")
    assert parsed["paper_instructions"].startswith("Grade 12 Business Studies")
    assert "Answer all the questions." in parsed["paper_instructions"]


def test_a_sinhala_group_instruction_is_found():
    # The real line from the Sinhala Business Statistics paper.
    text = (_mcq(34, "පහත ප්‍රශ්නයට නිවැරදි පිළිතුර තෝරන්න, ප්‍රමාණවත් වචන සහිතව.")
            + _mcq(35, "පහත ප්‍රශ්නයට නිවැරදි පිළිතුර තෝරන්න, ප්‍රමාණවත් වචන සහිතව.")
            + "36 සිට 39 දක්වා ප්‍රශ්න වලට පිළිතුරු සැපයීම සඳහා පහත වෘත්ත පත්‍ර සටහන භාවිතා කරන්න.\n"
            + "".join(_mcq(n, "සටහන ඇසුරෙන් පිළිතුර තෝරන්න, ප්‍රමාණවත් වචන සහිතව.") for n in range(36, 40)))
    groups = extract.structure(text, style="number")["instruction_groups"]
    assert [(g["from"], g["to"]) for g in groups] == [("36", "39")]


def test_number_ranges_that_are_not_group_instructions_are_left_alone():
    # Each from a real paper: a word count, a span of time, a percentage, and
    # a range that does not start at the next question.
    for line in ("Use about 75-100 words in your answer.",
                 "Growth is fastest at around 7-8 months of age.",
                 "This cuts water use by 30-50% compared to before.",
                 "Answer questions 40 to 45 after this one."):
        text = (_mcq(19, "Question 19 asks something with enough words in it.")
                + line + "\n"
                + _mcq(20, "Question 20 asks something with enough words in it."))
        parsed = extract.structure(text, style="number")
        assert parsed["instruction_groups"] == [], line
        q19 = next(q for q in parsed["questions"] if q["question_number"] == "19")
        assert line in q19["text"], line


def test_a_pasted_paper_stores_its_groups_and_instructions(local_db):
    doc = create_document_from_text(PastedPaperIn(text=PAPER, title="Business"), identity="x")
    assert [(g.from_, g.to) for g in doc.instruction_groups] == [("20", "25")]
    assert doc.notes.startswith("Grade 12 Business Studies")


def test_data_entry_can_add_edit_and_remove_group_instructions(local_db):
    doc = create_document_from_text(PastedPaperIn(text=PAPER, title="Business"), identity="x")
    groups = [g.model_dump(by_alias=True) for g in doc.instruction_groups]
    groups.append({"section": None, "from": "17", "to": "19", "title": "Part 1",
                   "text": "Answer these three."})
    updated = update_document(doc.id, DocumentUpdate(instruction_groups=groups))
    assert [(g.from_, g.to, g.title) for g in updated.instruction_groups] == [
        ("20", "25", None), ("17", "19", "Part 1")]
    # Stored under "from", the same name the extractor writes.
    assert service.store.intake_instruction_groups(
        service.store.get_intake_document(service.store.connect().__enter__(), doc.id))[1]["from"] == "17"
    emptied = update_document(doc.id, DocumentUpdate(instruction_groups=[]))
    assert emptied.instruction_groups == []
