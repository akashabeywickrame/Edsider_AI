"""Tests for the guard layer.

These prove the guards fire on exactly the pairs that need demoting,
and stay silent on genuine duplicates. No model needed - the guards
are pure text rules, which is precisely why they are testable.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import dedupe
from src.dedupe import guards


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


# ---------- Guard 5: shared passage, different question ----------

_PASSAGE = (
    "Read the following passage carefully before answering the question "
    "that follows. Sri Lanka is an island nation in South Asia, known "
    "for its diverse ecosystems ranging from rainforests to coastal "
    "wetlands. The country has a long history of agriculture, with tea, "
    "rubber and coconut forming the backbone of its plantation economy "
    "for well over a century. In recent decades, the government has "
    "invested heavily in irrigation projects to support paddy farming "
    "in the dry zone, while also working to protect remaining forest "
    "cover from further encroachment."
)


def test_same_long_passage_different_question_is_flagged():
    """The exact false positive this guard exists for: the passage
    dominates the raw embedding, but the two questions ask different
    things about it."""
    a = q(_PASSAGE + " What is the main theme of this passage?")
    b = q(_PASSAGE + " What is the author's tone in this passage?")
    penalty, reasons = guards.check(a, b)
    assert penalty >= guards.PENALTIES["shared_passage_different_question"]
    assert any("shares a" in r and "passage" in r for r in reasons)


def test_same_long_passage_same_question_is_not_flagged():
    """A genuine duplicate - the same passage AND the same question -
    must not be punished just for being long."""
    a = q(_PASSAGE + " What is the main theme of this passage?")
    b = q(_PASSAGE + " What is the main theme of this passage?")
    penalty, reasons = guards.check(a, b)
    assert penalty == 0.0, f"unexpected penalty: {reasons}"


def test_same_long_passage_reworded_question_is_not_flagged():
    """A paraphrase of the same actual question, about the same passage,
    is still a duplicate - the residual difference must be small enough
    to survive."""
    a = q(_PASSAGE + " What is the main theme of this passage?")
    b = q(_PASSAGE + " What theme does this passage explore?")
    penalty, reasons = guards.check(a, b)
    assert not any("passage" in r for r in reasons)


def test_short_shared_wording_does_not_trigger_the_passage_guard():
    """Two short questions sharing a common phrase are not a passage
    pair - the guard must stay silent well below its length threshold."""
    a = q("Calculate the area of a rectangle with length 12 cm and width 5 cm.")
    b = q("Calculate the perimeter of a rectangle with length 12 cm and width 5 cm.")
    penalty, reasons = guards.check(a, b)
    assert not any("passage" in r for r in reasons)


def test_two_different_long_passages_are_not_flagged_by_this_guard():
    """No long block is actually shared, so the guard must not fire just
    because both texts happen to be long."""
    a = q(_PASSAGE + " What is the main theme of this passage?")
    other_passage = (
        "The water cycle describes how water moves through the Earth's "
        "atmosphere, surface and underground reservoirs. Evaporation "
        "from oceans and lakes rises as vapour, which then cools and "
        "condenses into clouds. When droplets grow heavy enough, they "
        "fall back to the surface as precipitation, replenishing rivers, "
        "lakes and groundwater before the cycle begins again."
    )
    b = q(other_passage + " What is the main theme of this passage?")
    penalty, reasons = guards.check(a, b)
    assert not any("passage" in r for r in reasons)


def test_shared_passage_is_found_wherever_the_block_starts():
    """shared_passage() skips pairs with no 100-character chunk in
    common before running difflib. That shortcut must never hide a real
    shared block, whatever offset the block starts at on either side."""
    passage = (
        "Read the statements below about the early river valley "
        "civilisations, the way their cities grew along the banks, how "
        "trade routes connected them to distant regions, and how metal "
        "working with copper and bronze developed over several centuries "
        "before the rise of the first empires in the region."
    )
    assert len(passage) > 2 * 100  # past the 200-character minimum
    for pad_a in range(0, 101, 7):
        for pad_b in (0, 13, 50, 99):
            a = "x" * pad_a + passage + " What is the main theme?"
            b = "y" * pad_b + passage + " What is the author's tone?"
            result = guards.shared_passage(a, b)
            assert result is not None, (pad_a, pad_b)
            assert result[0] >= len(passage)


def test_shared_passage_helper_returns_none_below_threshold():
    assert guards.shared_passage("short text a", "short text b") is None


def test_relationship_mcq_tail_is_flagged_even_with_high_word_overlap():
    """Real pair from assessment #1127 (questions 58317/58328), reported
    at 99% similar and not flagged: the shared A/B/C/D passage is
    identical, and the two questions differ only in which relationship
    they ask about - but "which pair shows the second statement being a
    RESULT of the first" and "which pair shows NO RELATIONSHIP between
    the first and second" share enough surrounding words ("which pair
    shows ... the first and second statement(s)") that word overlap
    alone called them the same question (0.44 overlap, under the 0.34
    "differs" floor only because the floor was tuned to a hand-written
    example, not this real one). The CONTRAST_GROUPS entry for this MCQ
    format fixes it independently of that overlap check."""
    passage = (
        "The following shows pairs of statements under A, B, C, and D. "
        "Examine the relationship between the first and second "
        "statements.\n\nA - Respect for Women in Ancient Sri Lanka "
        "B - Economic Exchange in Ancient Sri Lanka C - Environmental "
        "Protection Laws in Ancient Sri Lanka D - Ancient Food Habits\n\n"
        "The opportunity for women to rule, as shown by Queen Chula "
        "Seewali and Queen Leelawathi, reflects the social respect and "
        "trust women held in ancient society.\n\nThe use of metal coins "
        "as a medium of exchange, with regulation by a special officer "
        "called 'Roopadaka,' indicates the importance of an organized "
        "economy.\n\nKings, such as Mihindu IV and Udaya III, passed "
        "laws to protect forests and prevent soil erosion, ensuring "
        "sustainable environmental practices.\n\nThe consumption of "
        "locally grown foods like rice, yams, and vegetables, and the "
        "use of natural spices like coriander and curry leaves, "
        "highlight the local food habits and emphasis on natural "
        "resources."
    )
    a = q(passage + "\n\nWhich pair shows the second statement being "
                     "a result of the first?\nA\nB\nC\nD")
    b = q(passage + "\n\nWhich pair shows no relationship between the "
                     "first and second statements?\nA\nB\nC\nD")
    penalty, reasons = guards.check(a, b)
    assert penalty >= guards.PENALTIES["opposite_concepts"]
    assert any("a result of" in r and "no relationship between" in r
               for r in reasons)
    # Reported live at 99% raw similarity - the penalty must be enough
    # to drop it out of review entirely, not just off the top of the list.
    assert 0.99 - penalty < dedupe.REVIEW_THRESHOLD


def test_shared_passage_helper_finds_the_residuals():
    a = _PASSAGE + " What is the main theme of this passage?"
    b = _PASSAGE + " What is the author's tone in this passage?"
    result = guards.shared_passage(a, b)
    assert result is not None
    size, residual_a, residual_b = result
    assert size >= guards._MIN_SHARED_PASSAGE_CHARS
    assert "main theme" in residual_a
    assert "author's tone" in residual_b


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


# ---------- mining candidate contrast terms from a rejected pair ----------

def test_candidate_contrast_terms_finds_the_distinguishing_words():
    only_a, only_b = guards.candidate_contrast_terms(
        "Calculate the area of a rectangle 12 cm by 5 cm",
        "Calculate the perimeter of a rectangle 12 cm by 5 cm",
    )
    assert "area" in only_a
    assert "perimeter" in only_b
    # Shared words must not appear on either side.
    assert "area" not in only_b
    assert "rectangle" not in only_a
    assert "rectangle" not in only_b


def test_candidate_contrast_terms_drops_common_words():
    only_a, only_b = guards.candidate_contrast_terms(
        "What is the capital of Sri Lanka?",
        "What is the capital of India?",
    )
    assert "what" not in only_a and "what" not in only_b
    assert "the" not in only_a and "the" not in only_b
    assert "sri" in only_a or "lanka" in only_a
    assert "india" in only_b


def test_candidate_contrast_terms_is_empty_for_identical_text():
    only_a, only_b = guards.candidate_contrast_terms(
        "Explain photosynthesis in green plants.",
        "Explain photosynthesis in green plants.",
    )
    assert only_a == []
    assert only_b == []


def test_candidate_contrast_terms_ignores_short_noise_words():
    """Words under 3 letters ('is', 'of', 'a') are not useful signal on
    their own and would drown out real candidates."""
    only_a, only_b = guards.candidate_contrast_terms(
        "Is it a solid or a liquid?",
        "Is it a gas or a plasma?",
    )
    assert all(len(w) >= 3 for w in only_a)
    assert all(len(w) >= 3 for w in only_b)
