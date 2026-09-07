"""The full duplicate-detection pipeline.

Stage 1  exact match      normalised hash - free, instant, exact
Stage 2  similarity       meaning fingerprints - catches rewording
Stage 3  guard layer      lowers the score when a warning sign appears

Cheapest technique first, most expensive last.
"""

import json

import numpy as np

from . import guards
from .embeddings import similarity_matrix

# Score bands. Provisional until tuned against real labelled data.
AUTO_THRESHOLD = 0.92
REVIEW_THRESHOLD = 0.85

# Ceiling on how many candidates check_one will run guards over. Guards
# cost ~0.2 ms each, so this is not about the common case - it bounds a
# query that sits above the threshold against half the bank.
MAX_GUARDED = 250

# How many questions a sweep compares against the bank at once. The full
# n x n grid is 2 GB at 16,000 questions; a block against everything is
# a few tens of MB whatever the bank grows to.
BLOCK = 512


def band(score):
    if score >= AUTO_THRESHOLD:
        return "LIKELY DUPLICATE"
    if score >= REVIEW_THRESHOLD:
        return "NEEDS REVIEW"
    return "DISTINCT"


def _vector(v):
    """Vectors arrive as a JSON string from the database, a list from a
    caller, or an array from the encoder. Accept all three."""
    return json.loads(v) if isinstance(v, str) else v


class VectorIndex:
    """The bank's fingerprints, parsed once and kept as one matrix.

    Vectors come out of SQLite as JSON text, and turning that back into
    numbers measured ~3 ms per row - fine for the 30-question sample,
    but 48 seconds against a real bank of 16,000, on every single call.
    Parsing once and holding float32 makes the same comparison take
    milliseconds and halves the memory.
    """

    __slots__ = ("rows", "matrix")

    def __init__(self, rows):
        self.rows = list(rows)
        self.matrix = np.array(
            [_vector(r["vector"]) for r in self.rows], dtype=np.float32
        ) if self.rows else np.zeros((0, 0), dtype=np.float32)

    def __len__(self):
        return len(self.rows)


def _as_index(rows):
    """Accept a VectorIndex or a plain list of rows."""
    return rows if isinstance(rows, VectorIndex) else VectorIndex(rows)


def find_pairs(rows, use_guards=True, threshold=REVIEW_THRESHOLD,
               progress=False):
    """Compare every question against every other.

    `rows` is a VectorIndex, or dict-like rows each with id, text,
    vector (JSON string or list), and optionally grade and subject.

    Returns a list of dicts, highest score first.

    This is O(n^2) by construction and is the one caller that does not
    scale: 16,000 questions is 128 million pairs. It is the right shape
    for an audit of a bank, and the wrong shape for a save-time check -
    use check_one for that.
    """
    index = _as_index(rows)
    if len(index) < 2:
        return []

    entries = index.rows
    vectors = index.matrix
    n = len(entries)
    floor = threshold - 0.001

    results = []
    # Compare a block of questions against the whole bank at a time. The
    # full n x n grid is fine at 30 questions and 2 GB at 16,000, but a
    # block against everything is a few tens of MB whatever n becomes.
    for start in range(0, n, BLOCK):
        stop = min(start + BLOCK, n)
        sim = vectors[start:stop] @ vectors.T

        for offset in range(stop - start):
            i = start + offset
            # Only j > i, so each pair is considered once.
            ahead = sim[offset, i + 1:]
            if ahead.size == 0:
                continue

            # Guards only ever lower a score, so anything below the
            # threshold now cannot come back up - and skipping it here
            # avoids a guard call per pair, which is what makes a sweep
            # of this size finish at all.
            for k in np.nonzero(ahead >= floor)[0]:
                j = i + 1 + int(k)
                raw = float(ahead[k])
                a, b = entries[i], entries[j]

                penalty = 0.0
                reasons = []
                if use_guards:
                    penalty, reasons = guards.check(a, b)

                final = max(0.0, raw - penalty)
                if final >= threshold:
                    results.append({
                        "a": a["id"],
                        "b": b["id"],
                        "raw_score": raw,
                        "penalty": penalty,
                        "score": final,
                        "reasons": reasons,
                        "verdict": band(final),
                    })

        if progress and start and start % (BLOCK * 8) == 0:
            print(f"  ... {start:,} of {n:,} compared, "
                  f"{len(results):,} pairs so far")

    results.sort(key=lambda r: -r["score"])
    return results


def check_one(question, rows, use_guards=True,
              threshold=REVIEW_THRESHOLD, top_k=5):
    """Check ONE question against the bank.

    `find_pairs` sweeps the whole bank against itself, which is fine for
    an audit but wrong for the thing EdSider actually does: a teacher
    adds one question and wants to know if it already exists. That is
    one question against n, not n against n.

    `question` is dict-like with 'text' and 'vector', optionally 'id',
    'grade' and 'subject'. `rows` are the bank.

    Returns the closest `top_k` matches, highest score first, INCLUDING
    ones the guards demoted below the threshold - a reviewer adding a
    question needs to see "this looked close, and here is why we think
    it is not the same" rather than silence.
    """
    index = _as_index(rows)
    if not len(index):
        return []

    rows = index.rows
    qvec = np.asarray(_vector(question["vector"]), dtype=np.float32)
    scores = index.matrix @ qvec

    # Guards can lower a score but never raise one, so anything already
    # below the threshold cannot climb into contention and is skipped
    # before any guard runs.
    order = np.argsort(-scores)

    results = []
    for i in order:
        raw = float(scores[i])
        # The order is descending, so the first miss ends the search.
        if raw < threshold - 0.001:
            break

        row = rows[i]
        # Re-checking a question already in the bank must not match itself.
        if question.get("id") is not None and row["id"] == question["id"]:
            continue

        penalty, reasons = (guards.check(question, row)
                            if use_guards else (0.0, []))
        final = max(0.0, raw - penalty)

        results.append({
            "match": row,
            "raw_score": raw,
            "penalty": penalty,
            "score": final,
            "reasons": reasons,
            "verdict": band(final),
        })

        # Guard every candidate above the threshold, not just the first
        # top_k of them. Truncating on the RAW score would let a cluster
        # of look-alikes fill every slot and then collapse under their
        # penalties, pushing out the one genuine duplicate - which scored
        # lower raw precisely because it is a rewording rather than a
        # near-copy. Sorting happens after the guards, so truncation must
        # too. A guard check is ~0.2 ms, so the only real cost is in a
        # pathological query, which MAX_GUARDED bounds.
        if len(results) >= MAX_GUARDED:
            break

    results.sort(key=lambda r: -r["score"])
    return results[:top_k]


def search(query_vector, rows, top_k=5):
    """Find the questions most similar in meaning to a query."""
    index = _as_index(rows)
    if not len(index):
        return []

    scores = index.matrix @ np.asarray(query_vector, dtype=np.float32)

    order = np.argsort(-scores)[:top_k]
    return [
        {"question": index.rows[i], "score": float(scores[i])}
        for i in order
    ]
