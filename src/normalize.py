"""Text normalisation and fingerprinting.

Stage 1 of the duplicate-detection pipeline. No AI involved - this is
deterministic, instant, free, and perfectly accurate for exact matches.
"""

import re
import json
import hashlib
import unicodedata


def extract_text(value):
    """Get plain text out of a question field, whatever format it arrives in.

    EdSider may store questions as plain text or as rich-text JSON
    (Draft.js format), and may hold a mix of both if the editor was
    changed at some point. This handles either without the caller
    needing to know which.
    """
    if value is None:
        return ""

    if isinstance(value, str):
        s = value.strip()
        # Plain text is by far the common case - return it immediately.
        if not s.startswith(("{", "[")):
            return s
        try:
            value = json.loads(s)
        except json.JSONDecodeError:
            # Looked like JSON but is not. Treat as plain text.
            return s

    # Draft.js raw format: a list of content states, each holding blocks.
    states = value if isinstance(value, list) else [value]
    return "\n".join(
        block.get("text", "")
        for state in states
        for block in state.get("blocks", [])
    ).strip()


def has_attachments(value):
    """True if the rich-text content references an image, link or equation.

    When this is true the text alone does NOT fully represent the
    question, so it should be flagged for a human rather than treated
    as cleanly extracted.
    """
    if isinstance(value, str):
        s = value.strip()
        if not s.startswith(("{", "[")):
            return False
        try:
            value = json.loads(s)
        except json.JSONDecodeError:
            return False
    if value is None:
        return False
    states = value if isinstance(value, list) else [value]
    return any(state.get("entityMap") for state in states)


def normalize(text):
    """Reduce text to a canonical form for comparison.

    Two questions that differ only in formatting must produce identical
    output here. Two questions that differ in meaning must not.
    """
    text = extract_text(text)

    # NFC matters for Sinhala and Tamil: the same visible character can
    # have more than one byte representation, and without this they
    # would fail to match each other.
    text = unicodedata.normalize("NFC", text)

    text = text.lower()

    # Collapse all whitespace, including non-breaking spaces and tabs.
    text = re.sub(r"\s+", " ", text)

    # Strip punctuation, but keep the Sinhala (0D80-0DFF) and Tamil
    # (0B80-0BFF) unicode blocks. Python's \w alone drops their vowel
    # signs and viramas, which would corrupt the text.
    text = re.sub(r"[^\w\s඀-෿஀-௿]", "", text)

    return text.strip()


def content_hash(text):
    """Stable fingerprint. Same normalised text always gives the same hash."""
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()
