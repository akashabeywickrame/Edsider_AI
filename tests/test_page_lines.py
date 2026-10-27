"""Running page lines and labels on a line of their own - two ways a page
break and a PDF's own layout broke paper 6's MCQs (Biosystem Technology,
Grade 13): on one upload, 14 of its 50 MCQs were split into "parts"."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.intake import extract

FOOTER = "{n}                                       Biosystem Technology Grade 13 "


def _paper(lone_labels=False):
    """Paper 6's questions 13-15 across its pages 4 and 5, as that upload
    read them: the footer of every page, question 14's options split by
    the page break, and (lone_labels) each label on its own line."""
    def options(*texts):
        labels = ["I.", "II.", "III.", "IV.", "V."]
        return "\n".join(f"{l} \n{t} " if lone_labels else f"{l} {t}" for l, t in zip(labels, texts))
    q14 = options("By increasing pollution", "By reducing biodiversity",
                  "By balancing ecological, economic and social needs",
                  "By limiting technological advancements", "By ignoring climate change").split("\n")
    cut = 4 if lone_labels else 2
    return "\n".join([
        FOOTER.format(n=3),
        "Part I",
        "13.What is the primary key consideration when implementing mechanization in ",
        "small scale agriculture? ",
        options("Increasing labour requirements", "Matching machinery to farm size and crop needs",
                "Reducing crop yields", "Limiting technology use", "Increasing costs without benefits"),
        "14.How do environmental friendly strategies contribute to sustainable development ",
        "of biological systems? ",
        *q14[:cut],
        "",
        FOOTER.format(n=4),
        " ",
        FOOTER.format(n=5),
        *q14[cut:],
        "15.How does sustainable utilization of forest products contribute to conservation?",
        options("Increases deforestation", "Ensures regeneration and reduces environmental impact",
                "Limits forest use", "Increases illegal logging", "Reduces biodiversity"),
        "",
        FOOTER.format(n=6),
    ])


def _mcq_numbers(text):
    qs = extract.structure(text)["questions"]
    has = {q["parent_seq"] for q in qs if q.get("parent_seq")}
    return [q["question_number"] for q in qs
            if extract.detect_question_type(q["text"], q["seq"] in has) == "mcq"]


def test_the_page_footer_does_not_break_an_option_list():
    text = _paper()
    assert _mcq_numbers(text) == ["13", "14", "15"]
    q14 = extract.structure(text)["questions"][1]
    assert "Biosystem" not in q14["text"]
    assert [o["label"] for o in extract.split_mcq_options(q14["text"])[1]] == [
        "I", "II", "III", "IV", "V"]


def test_labels_on_their_own_line_are_still_options():
    assert _mcq_numbers(_paper(lone_labels=True)) == ["13", "14", "15"]


def test_only_lines_repeating_with_a_page_count_go():
    text = "\n".join([
        "1. Define the term system. (2 marks)",
        "2. Name two input devices. (2 marks)",
        "3. State two uses of a database. (2 marks)",
        "-2-",
        "4. Explain feedback. (2 marks)",
        "-3-",
        "5. Explain a loop. (2 marks)",
        "-4-",
    ])
    kept = extract.strip_running_lines(text)
    assert kept.count("(2 marks)") == 5        # a question's own words stay
    assert "-2-" not in kept and "-4-" not in kept
    # Twice is not a running line - it may be the paper's own.
    assert extract.strip_running_lines("1 Grade 13 Biology paper\nQ\n2 Grade 13 Biology paper") == \
        "1 Grade 13 Biology paper\nQ\n2 Grade 13 Biology paper"


def test_a_bare_number_alone_is_not_joined():
    """"5." alone on its line is a question number as often as an option."""
    assert extract.join_lone_markers("5.\nWhat is a system?") == "5.\nWhat is a system?"
    assert extract.join_lone_markers("(a) \nDefine it.") == "(a) Define it."
