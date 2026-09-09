"""The guard layer.

Meaning fingerprints have one dangerous failure: questions that are
worded almost identically but test completely different things score
very high.

    "Calculate the AREA of a rectangle 12 cm by 5 cm"
    "Calculate the PERIMETER of a rectangle 12 cm by 5 cm"

One word different, same numbers, and an embedding model scores them
above 0.9. They are not duplicates. Merging them would delete a
perfectly good question.

Each guard looks for one warning sign and lowers the score when it
finds one. Every guard returns a human-readable reason, so the
reviewer can see WHY a pair was demoted rather than being handed a
bare number.
"""

import re

from .normalize import normalize

# How much each warning sign lowers a pair's similarity score.
PENALTIES = {
    "numbers_differ": 0.25,
    "opposite_concepts": 0.40,
    "different_entities": 0.30,
    "metadata_mismatch": 0.15,
}

# Groups of terms that are commonly confused but mean different things.
# If two questions each use a DIFFERENT term from the same group, they
# are almost certainly testing different concepts.
#
# This list is hand-written for now. At scale it should be learned from
# the decision log: when reviewers repeatedly reject a flagged pair,
# look at which words differ between them.
CONTRAST_GROUPS = [
    {"area", "perimeter", "circumference"},
    {"volume", "surface area"},
    {"mean", "median", "mode", "range"},
    {"mass", "weight"},
    {"speed", "velocity", "acceleration"},
    {"maximum", "minimum"},
    {"radius", "diameter"},
    {"respiratory", "digestive", "circulatory", "nervous"},
    {"evaporation", "condensation", "precipitation"},
    {"photosynthesis", "respiration"},
    {"import", "export"},
    {"profit", "loss"},
    {"increase", "decrease"},
]

# Proper nouns worth comparing explicitly. A question about Sri Lanka
# and one about India can be worded identically and mean very different
# things.
KNOWN_ENTITIES = {
    "sri lanka", "india", "china", "japan", "england", "britain",
    "australia", "pakistan", "bangladesh", "nepal", "maldives",
    "colombo", "kandy", "galle", "jaffna", "delhi", "mumbai",
    "london", "tokyo", "beijing",
}


# Numbering, not content. "Refer to Figure 1" and "Refer to Figure 2"
# differ by a label, not by what is being asked.
_REFERENCE = re.compile(
    r"\b(?:figure|fig|table|diagram|chart|graph|map|image|picture|"
    r"question|part|section|page|line|paragraph|no|number)"
    r"\.?\s*\d+(?:\.\d+)?",
    re.IGNORECASE,
)

# A digit bound inside a word is part of a token, not a quantity: CO2,
# H2O, x2. Matching those as numbers made "CO2" and "carbon dioxide"
# look like a numeric conflict.
_NUMBER = re.compile(r"(?<![A-Za-z0-9.])\d+(?:\.\d+)?(?![A-Za-z0-9])")


def extract_numbers(text):
    """Every quantity in the text, as a sorted tuple.

    Sorted so that '12 cm by 5 cm' and '5 cm by 12 cm' compare equal -
    those really are the same question.

    Figure references and digits inside words are stripped first: they
    are labels and formulae, not the numbers the question turns on.
    """
    text = _REFERENCE.sub(" ", text)
    return tuple(sorted(float(x) for x in _NUMBER.findall(text)))


def extract_entities(text):
    """Known place names mentioned in the text."""
    low = text.lower()
    return {e for e in KNOWN_ENTITIES if e in low}


# Whole words only. A plain substring test matched these terms inside
# unrelated words - "arrange" contains "range", "important" contains
# "import", "modern" contains "mode" - and each false match applied the
# largest penalty in the system to a pair that may be a real duplicate.
# Built once at import rather than per comparison.
_CONTRAST_PATTERNS = [
    {term: re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)
     for term in group}
    for group in CONTRAST_GROUPS
]


def find_contrast(text_a, text_b):
    """Return (term_in_a, term_in_b) if they use opposing concepts."""
    for patterns in _CONTRAST_PATTERNS:
        in_a = {t for t, p in patterns.items() if p.search(text_a)}
        in_b = {t for t, p in patterns.items() if p.search(text_b)}
        # Both mention something from this group, but not the same thing.
        if in_a and in_b and in_a != in_b:
            only_a = in_a - in_b
            only_b = in_b - in_a
            if only_a and only_b:
                return sorted(only_a)[0], sorted(only_b)[0]
    return None


# Words too common to distinguish anything - every question shares them,
# so they would drown out real candidates. Deliberately short: this is a
# heuristic that surfaces CANDIDATES for a person to review, not a rule
# that fires on its own, so erring toward keeping a borderline word in
# costs nothing - a person filters the rest.
_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "of", "in", "on", "to", "and", "or", "for", "with", "by", "from",
    "at", "as", "this", "that", "these", "those", "its", "it", "not",
    "what", "which", "how", "why", "when", "where", "who", "does", "do",
    "did", "has", "have", "had", "will", "would", "should", "can",
    "given", "following", "above", "below", "each", "one", "two",
    "into", "than", "then", "your", "you", "their", "there", "about",
}

_CANDIDATE_WORD = re.compile(r"[A-Za-z]{3,}")


def candidate_contrast_terms(text_a, text_b):
    """Words present in one question's text but not the other's -
    candidates for CONTRAST_GROUPS, not new rules by themselves.

    Meant to be called when a reviewer marks a pair the system flagged
    as similar "Not a duplicate" - the two texts scored close enough to
    be shown together, so whatever vocabulary differs between them is
    a plausible reason they are NOT the same question, the same
    reasoning find_contrast() already applies to the hand-written
    CONTRAST_GROUPS list, just without a list to check against yet.

    Nothing here is applied automatically - "the system suggests, a
    person decides" applies just as much to a new guard term as it does
    to a merge. This only records what MIGHT distinguish a pair, for a
    person to confirm later before it becomes a real CONTRAST_GROUPS
    entry (see CLAUDE.md, "known limitations").

    Returns (only_in_a, only_in_b) - two sorted lists of words.
    """
    words_a = {w.lower() for w in _CANDIDATE_WORD.findall(text_a)} - _STOPWORDS
    words_b = {w.lower() for w in _CANDIDATE_WORD.findall(text_b)} - _STOPWORDS
    return sorted(words_a - words_b), sorted(words_b - words_a)


def check(question_a, question_b):
    """Run every guard on a pair.

    Both arguments are dict-like with at least 'text', and optionally
    'grade' and 'subject'.

    Returns (total_penalty, [reasons]).
    """
    reasons = []
    penalty = 0.0

    text_a = question_a["text"]
    text_b = question_b["text"]

    # Guard 1 - the numbers differ.
    # BOTH sides must carry numbers, for the same reason Guard 3 below
    # requires an entity on both sides: absence is not evidence of
    # difference. "Calculate the area of the rectangle shown in the
    # figure" has no numbers at all, and penalising it against "...12 cm
    # by 5 cm" demotes what may well be the same question.
    nums_a = extract_numbers(text_a)
    nums_b = extract_numbers(text_b)
    if nums_a and nums_b and nums_a != nums_b:
        penalty += PENALTIES["numbers_differ"]
        reasons.append(
            f"numbers differ ({_fmt(nums_a)} vs {_fmt(nums_b)})"
        )

    # Guard 2 - opposing concepts.
    # This catches what Guard 1 cannot: area vs perimeter with the SAME
    # numbers. The numeric guard is blind to that pair.
    contrast = find_contrast(text_a, text_b)
    if contrast:
        penalty += PENALTIES["opposite_concepts"]
        reasons.append(f"opposite concepts ({contrast[0]} vs {contrast[1]})")

    # Guard 3 - different named entities.
    # BOTH sides must name an entity. If only one does, that is absence
    # of evidence, not evidence of difference: "independence from
    # British rule" and "independent from Britain" are the same
    # question, and only one of them matches the entity list.
    # A conflict means EACH side names something the other does not.
    # One side simply mentioning an extra place is not a conflict.
    ents_a = extract_entities(text_a)
    ents_b = extract_entities(text_b)
    only_a = ents_a - ents_b
    only_b = ents_b - ents_a
    if only_a and only_b:
        penalty += PENALTIES["different_entities"]
        reasons.append(
            f"different entities ({_fmt_set(only_a)} vs {_fmt_set(only_b)})"
        )

    # Guard 4 - metadata says these belong to different contexts.
    #
    # Silent when the text is character-identical after normalising.
    # Metadata cannot make two questions different when they ARE the same
    # question: on the real bank 42 exact-duplicate groups span more than
    # one grade and 17 more than one subject, because the same question
    # was catalogued at two levels. That is a tagging inconsistency, and
    # demoting the pair hides exactly the duplicate worth finding.
    ga, gb = question_a.get("grade"), question_b.get("grade")
    sa, sb = question_a.get("subject"), question_b.get("subject")
    if ((ga and gb and ga != gb) or (sa and sb and sa != sb)) \
            and normalize(text_a) != normalize(text_b):
        penalty += PENALTIES["metadata_mismatch"]
        reasons.append(f"different grade or subject ({ga}/{sa} vs {gb}/{sb})")

    return penalty, reasons


def _fmt(nums):
    if not nums:
        return "none"
    return ", ".join(f"{n:g}" for n in nums)


def _fmt_set(items):
    return ", ".join(sorted(items)) if items else "none"
