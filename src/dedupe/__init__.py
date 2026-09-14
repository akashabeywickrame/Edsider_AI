"""The duplicate-detection engine - passes 1 to 3.

Was a flat set of modules under src/ (normalize, embeddings, guards,
dedupe). Moved into its own package when the paper-intake feature was
added, so the two features live side by side (src/dedupe/, src/intake/)
rather than intermixed. normalize.py stayed at src/normalize.py - it is
shared text-handling infrastructure (import, sync, store and intake all
use it), not part of the matching engine itself.

The public surface is re-exported here so callers keep writing
`from src import dedupe; dedupe.find_pairs(...)` exactly as before -
the move is meant to be invisible to everything downstream.
"""

from . import embeddings, guards
from .pipeline import (
    AUTO_THRESHOLD,
    BLOCK,
    MAX_GUARDED,
    REVIEW_THRESHOLD,
    VectorIndex,
    band,
    check_one,
    find_cross_passage_duplicates,
    find_pairs,
    find_shared_passage_groups,
    find_within_assessment_duplicates,
    search,
)

__all__ = [
    "embeddings",
    "guards",
    "AUTO_THRESHOLD",
    "BLOCK",
    "MAX_GUARDED",
    "REVIEW_THRESHOLD",
    "VectorIndex",
    "band",
    "check_one",
    "find_cross_passage_duplicates",
    "find_pairs",
    "find_shared_passage_groups",
    "find_within_assessment_duplicates",
    "search",
]
