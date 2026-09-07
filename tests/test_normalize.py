"""Tests for text normalisation and fingerprinting.

These are the tests that prove Stage 1 works. Run them before anything
else - if normalisation is wrong, everything downstream is wrong too.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.normalize import normalize, content_hash, extract_text, has_attachments


# ---------- formatting differences must collapse ----------

def test_case_is_ignored():
    assert content_hash("Calculate the Area") == content_hash("calculate the area")


def test_extra_whitespace_is_ignored():
    assert content_hash("Calculate  the   area") == content_hash("Calculate the area")


def test_punctuation_is_ignored():
    assert content_hash("Calculate the area.") == content_hash("Calculate the area")


def test_leading_and_trailing_space_ignored():
    assert content_hash("  Calculate the area  ") == content_hash("Calculate the area")


def test_tabs_and_newlines_collapse():
    assert content_hash("Calculate\tthe\narea") == content_hash("Calculate the area")


# ---------- genuinely different questions must NOT collide ----------

def test_area_and_perimeter_differ():
    a = "Calculate the area of a rectangle with length 12 cm and width 5 cm."
    b = "Calculate the perimeter of a rectangle with length 12 cm and width 5 cm."
    assert content_hash(a) != content_hash(b)


def test_different_numbers_differ():
    a = "Calculate the area of a rectangle with length 12 cm and width 5 cm."
    b = "Calculate the area of a rectangle with length 15 cm and width 5 cm."
    assert content_hash(a) != content_hash(b)


def test_reworded_question_is_not_an_exact_match():
    """Stage 1 should NOT catch these - that is Stage 2's job."""
    a = "Calculate the area of a rectangle with length 12 cm and width 5 cm."
    b = "A rectangle has a length of 12 cm and a width of 5 cm. Find its area."
    assert content_hash(a) != content_hash(b)


# ---------- text extraction handles both storage formats ----------

def test_plain_text_passes_through():
    assert extract_text("hello world") == "hello world"


def test_none_becomes_empty_string():
    assert extract_text(None) == ""


def test_rich_text_json_is_unwrapped():
    raw = '[{"blocks":[{"key":"b86vu","text":"ans 2 update ","type":"unstyled"}],"entityMap":{}}]'
    assert extract_text(raw) == "ans 2 update"


def test_rich_text_multiple_blocks_are_joined():
    raw = '[{"blocks":[{"text":"line one"},{"text":"line two"}],"entityMap":{}}]'
    assert extract_text(raw) == "line one\nline two"


def test_malformed_json_falls_back_to_plain_text():
    assert extract_text("{not actually json") == "{not actually json"


def test_same_text_different_formatting_gives_same_hash():
    """The whole point: rich-text styling must not affect the fingerprint."""
    plain = "ans 2 update"
    styled = ('[{"blocks":[{"text":"ans 2 update ",'
              '"inlineStyleRanges":[{"offset":6,"length":6,"style":"fontsize-13"}]}],'
              '"entityMap":{}}]')
    assert content_hash(plain) == content_hash(styled)


# ---------- attachments make text extraction incomplete ----------

def test_no_attachments_in_plain_text():
    assert has_attachments("just words") is False


def test_entity_map_is_detected():
    raw = '[{"blocks":[{"text":"See the diagram"}],"entityMap":{"0":{"type":"IMAGE"}}}]'
    assert has_attachments(raw) is True


# ---------- Sinhala and Tamil must survive normalisation ----------

def test_sinhala_vowel_signs_are_preserved():
    """Python's \\w alone strips these combining marks, corrupting the text."""
    assert normalize("ලකුණු 05") == "ලකුණු 05"


def test_tamil_characters_are_preserved():
    assert normalize("மதிப்பெண் 05") == "மதிப்பெண் 05"


def test_sinhala_unicode_forms_match():
    """Same visible text, different byte sequences, must match after NFC."""
    import unicodedata
    text = "ශ්‍රී ලංකා"
    nfd = unicodedata.normalize("NFD", text)
    assert content_hash(text) == content_hash(nfd)
