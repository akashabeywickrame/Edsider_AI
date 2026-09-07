"""Tests for the guard layer.

These prove the guards fire on exactly the pairs that need demoting,
and stay silent on genuine duplicates. No model needed - the guards
are pure text rules, which is precisely why they are testable.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import guards


def q(text, grade=None, subject=None):
    return {"text": text, "grade": grade, "subject": subject}


# ---------- Guard 1: numbers differ ----------

def test_different_numbers_are_flagged():
    a = q("Calculate the area of a rectangle with length 12 cm and width 5 cm.")
    b = q("Calculate the area of a rectangle with length 15 cm and width 5 cm.")
    penalty, reasons = guards.check(a, b)
    assert penalty > 0
    assert any("numbers differ" in r for r in reasons)


def test_same_numbers_reordered_are_not_flagged():
    """12 by 5 and 5 by 12 really are the same question."""
    assert guards.extract_numbers("length 12 cm width 5 cm") == \
           guards.extract_numbers("width 5 cm length 12 cm")


def test_identical_text_has_no_penalty():
    a = q("Calculate the area of a rectangle with length 12 cm and width 5 cm.")
    b = q("Calculate the area of a rectangle with length 12 cm and width 5 cm.")
    penalty, reasons = guards.check(a, b)
    assert penalty == 0.0
    assert reasons == []


def test_numbers_on_one_side_only_is_not_a_conflict():
    """Absence of evidence is not evidence of difference - the same rule
    Guard 3 follows. A question referring to a figure carries no numbers,
    and that must not demote it against one that does."""
    a = q("Calculate the area of the rectangle shown in the figure.")
    b = q("Calculate the area of a rectangle with length 12 cm and width 5 cm.")
    penalty, reasons = guards.check(a, b)
    assert not any("numbers differ" in r for r in reasons)


def test_digits_inside_words_are_not_numbers():
    """CO2 is a formula, not a quantity."""
    a = q("Name the gas represented by the formula CO2.")
    b = q("Name the gas represented by the formula carbon dioxide.")
    penalty, reasons = guards.check(a, b)
    assert not any("numbers differ" in r for r in reasons)
    assert guards.extract_numbers("CO2 and H2O") == ()


def test_figure_references_are_not_numbers():
    """'Figure 1' vs 'Figure 2' is a label differing, not the question."""
    a = q("Refer to Figure 1 and describe the water cycle.")
    b = q("Refer to Figure 2 and describe the water cycle.")
    penalty, reasons = guards.check(a, b)
    assert not any("numbers differ" in r for r in reasons)


def test_real_quantities_still_flagged_after_stripping():
    """The stripping must not blind the guard to genuine differences."""
    a = q("Refer to Figure 1. Find the area of a rectangle 12 cm by 5 cm.")
    b = q("Refer to Figure 2. Find the area of a rectangle 15 cm by 5 cm.")
    penalty, reasons = guards.check(a, b)
    assert any("numbers differ" in r for r in reasons)
    assert "12" in reasons[0] and "15" in reasons[0]


# ---------- Guard 2: opposing concepts ----------

def test_area_vs_perimeter_is_flagged():
    """The case the numeric guard CANNOT catch - numbers are identical."""
    a = q("Calculate the area of a rectangle with length 12 cm and width 5 cm.")
    b = q("Calculate the perimeter of a rectangle with length 12 cm and width 5 cm.")
    penalty, reasons = guards.check(a, b)
    assert penalty >= guards.PENALTIES["opposite_concepts"]
    assert any("opposite concepts" in r for r in reasons)


def test_volume_vs_surface_area_is_flagged():
    a = q("Calculate the volume of a cube with sides of 4 cm.")
    b = q("Calculate the surface area of a cube with sides of 4 cm.")
    penalty, reasons = guards.check(a, b)
    assert any("opposite concepts" in r for r in reasons)


def test_mean_vs_median_is_flagged():
    a = q("Find the mean of the following numbers: 4, 8, 15, 16, 23, 42.")
    b = q("Find the median of the following numbers: 4, 8, 15, 16, 23, 42.")
    penalty, reasons = guards.check(a, b)
    assert any("opposite concepts" in r for r in reasons)


def test_respiratory_vs_digestive_is_flagged():
    a = q("What are the main functions of the human respiratory system?")
    b = q("What are the main functions of the human digestive system?")
    penalty, reasons = guards.check(a, b)
    assert any("opposite concepts" in r for r in reasons)


def test_same_concept_word_is_not_flagged():
    """Both mention 'area' - that is agreement, not contrast."""
    a = q("Calculate the area of a rectangle with length 12 cm and width 5 cm.")
    b = q("A rectangle has a length of 12 cm and a width of 5 cm. Find its area.")
    penalty, reasons = guards.check(a, b)
    assert not any("opposite concepts" in r for r in reasons)


def test_contrast_terms_match_whole_words_only():
    """A substring test matched contrast terms inside unrelated words and
    applied the largest penalty in the system to innocent pairs."""
    assert guards.find_contrast(
        "Arrange these numbers in ascending order",
        "Find the mean of these numbers") is None          # range/arrange
    assert guards.find_contrast(
        "Why is photosynthesis important for life",
        "Explain the export of tea from Sri Lanka") is None  # import/important
    assert guards.find_contrast(
        "What does the word mean in this context",
        "Describe the modern transport system") is None      # mode/modern


def test_whole_word_contrast_still_fires():
    """The fix must not silence the guard on the real cases."""
    assert guards.find_contrast(
        "Find the mean of the following numbers",
        "Find the median of the following numbers") is not None
    assert guards.find_contrast(
        "Calculate the range of the data",
        "Calculate the mode of the data") is not None


def test_multi_word_contrast_terms_still_match():
    assert guards.find_contrast(
        "Calculate the surface area of a cube",
        "Calculate the volume of a cube") is not None


# ---------- Guard 3: named entities ----------

def test_sri_lanka_vs_india_is_flagged():
    a = q("What is the capital city of Sri Lanka?")
    b = q("What is the capital city of India?")
    penalty, reasons = guards.check(a, b)
    assert any("different entities" in r for r in reasons)


def test_same_entity_is_not_flagged():
    a = q("In which year did Sri Lanka gain independence from British rule?")
    b = q("State the year in which Sri Lanka became independent from Britain.")
    penalty, reasons = guards.check(a, b)
    assert not any("different entities" in r for r in reasons)


# ---------- Guard 4: metadata ----------

def test_different_subject_is_flagged():
    # The texts differ. Identical text is a separate case and is covered
    # by test_identical_text_survives_a_grade_mismatch - the real bank
    # showed that identical text across subjects is one question filed
    # twice, not two questions.
    a = q("Describe the water cycle.", grade="7", subject="Science")
    b = q("Describe the water cycle in your own words.",
          grade="7", subject="History")
    penalty, reasons = guards.check(a, b)
    assert any("different grade or subject" in r for r in reasons)


def test_identical_text_survives_a_grade_mismatch():
    """Measured on the real bank: 42 exact-duplicate groups span more
    than one grade, because the same question is catalogued at two
    levels. Demoting those hides the duplicate that matters most."""
    a = q("What is the Octal equivalent of 1AB16 (clearly show the steps)",
          grade="10", subject="7")
    b = q("What is the Octal equivalent of 1AB16 (clearly show the steps)",
          grade="11", subject="7")
    penalty, reasons = guards.check(a, b)
    assert penalty == 0.0, f"unexpected penalty: {reasons}"


def test_whitespace_only_difference_survives_a_grade_mismatch():
    """The real pairs differ only by a stray blank line."""
    a = q("Which of the following is an output device?\nPrinter\nMouse",
          grade="10", subject="7")
    b = q("Which of the following is an output device?\n\nPrinter\n Mouse ",
          grade="11", subject="7")
    penalty, reasons = guards.check(a, b)
    assert not any("grade or subject" in r for r in reasons)


def test_different_text_still_flags_a_grade_mismatch():
    """The guard must stay useful where the wording actually differs."""
    a = q("Describe the water cycle.", grade="7", subject="Science")
    b = q("Describe the carbon cycle.", grade="11", subject="Science")
    penalty, reasons = guards.check(a, b)
    assert any("grade or subject" in r for r in reasons)


def test_missing_metadata_does_not_flag():
    """Absent metadata is not evidence of difference."""
    a = q("Describe the water cycle.")
    b = q("Describe the water cycle.")
    penalty, reasons = guards.check(a, b)
    assert not any("grade or subject" in r for r in reasons)


# ---------- genuine duplicates must survive untouched ----------

def test_reworded_duplicate_gets_no_penalty():
    a = q("Explain the process of photosynthesis in green plants.",
          grade="8", subject="Science")
    b = q("How do green plants manufacture their own food?",
          grade="8", subject="Science")
    penalty, reasons = guards.check(a, b)
    assert penalty == 0.0, f"unexpected penalty: {reasons}"


def test_water_cycle_rewording_gets_no_penalty():
    a = q("Describe the water cycle and name its four main stages.",
          grade="7", subject="Science")
    b = q("Name the four main stages of the water cycle and describe each one.",
          grade="7", subject="Science")
    penalty, reasons = guards.check(a, b)
    assert penalty == 0.0, f"unexpected penalty: {reasons}"


# ---------- the ablation: guards must change the outcome ----------

def test_guards_demote_below_threshold():
    """A hard negative scoring 0.95 must drop below the review band."""
    a = q("Calculate the area of a rectangle with length 12 cm and width 5 cm.")
    b = q("Calculate the perimeter of a rectangle with length 12 cm and width 5 cm.")
    raw = 0.95
    penalty, _ = guards.check(a, b)
    assert raw - penalty < 0.85, "guard did not demote the pair far enough"
