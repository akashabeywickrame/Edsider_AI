"""The full duplicate-detection pipeline.

Stage 1  exact match      normalised hash - free, instant, exact
Stage 2  similarity       meaning fingerprints - catches rewording
Stage 3  guard layer      lowers the score when a warning sign appears

Cheapest technique first, most expensive last.
"""

import json
import re
from collections import defaultdict

import numpy as np

from . import guards
from .embeddings import similarity_matrix
from .normalize import normalize

_LETTERS = re.compile(r"[A-Za-z]")

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


def find_within_assessment_duplicates(assessment_groups, use_guards=True,
                                      threshold=0.95):
    """Find repeats within each assessment (exam paper), not across the
    whole bank.

    `assessment_groups` is {assessment_id: rows} - typically
    store.questions_in_assessment() called once per
    store.assessment_ids(). Each group is compared only against itself
    with find_pairs(), reused rather than reimplemented.

    threshold defaults to 0.95, not REVIEW_THRESHOLD (0.85) - measured,
    not assumed, on the real bank: at 0.85, 650 real papers produced
    34,648 "repeats", 89% of them sitting at 0.85-0.90 and looking like
    "Russian Revolution" MCQ options scored against "French Revolution"
    MCQ options - the same raw-embedding false-positive shape the
    whole-bank sweep has, not genuine repeats. MCQ option text carries
    enough shared vocabulary that guards barely help here (34,648 ->
    27,829 with guards on, at 0.85) - unlike the whole-bank sweep, the
    fix is the threshold, not the guard layer. At 0.95, real spot-checks
    were all genuine: the exact same MCQ, word for word, landing twice
    on one paper.

    use_guards defaults to True anyway, since it still filters out a
    real fraction of the noise (323 -> 194 pairs at 0.95) for no cost -
    the earlier assumption that a small group makes guards pointless
    turned out to only be true of the threshold, not the guards.

    Returns a list of dicts like find_pairs, each also carrying its
    assessment_id, highest score first.
    """
    results = []
    for assessment_id, rows in assessment_groups.items():
        for pair in find_pairs(rows, use_guards=use_guards, threshold=threshold):
            pair["assessment_id"] = assessment_id
            results.append(pair)
    results.sort(key=lambda r: -r["score"])
    return results


def find_shared_passage_groups(rows):
    """Group questions on one paper that share a long passage with each
    other - informational context for a reviewer, not a detection rule.

    This does NOT change any score, demote any pair, or remove anything
    from find_within_assessment_duplicates()'s results. It exists for a
    narrower reason: a paper like the one that prompted it (assessment
    #1127) can carry a whole family of questions built on one shared
    A/B/C/D passage - "which pair shows a result of the first", "which
    pair shows no relationship between the first and second" - and nine
    times out of ten a reviewer seeing two of them side by side under
    "REPEAT" has no way to tell, at a glance, that this is a known
    passage-sharing shape rather than an actual copy-paste mistake. This
    surfaces that shape so the UI can say so.

    Deliberately NOT based on question_number adjacency ("two consecutive
    questions") - numbering has gaps (assessment #1127's own pair is Q31
    and Q33, not Q31/Q32) and, more importantly, two consecutive
    questions matching because someone genuinely pasted the same block
    twice is exactly the mistake this whole project exists to catch.
    Position is corroborating evidence at best; guards.shared_passage()
    finding an actual shared block is what the grouping is built on.

    `rows` is one assessment's group (e.g. one value from
    store.questions_in_assessment()'s per-assessment dict) - dict-like
    rows with at least 'id' and 'text', ideally 'question_number'.

    Returns a list of {"question_ids": [...], "question_numbers": [...]}
    - one entry per group of 2 or more rows connected by a shared
    passage (transitively: A-B and B-C shared means A, B, C all group
    together even if A and C alone fall short of the threshold). Rows
    with no long shared block with anything else are left out entirely.
    """
    rows = list(rows)
    parent = {r["id"]: r["id"] for r in rows}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            if guards.shared_passage(rows[i]["text"], rows[j]["text"]) is not None:
                union(rows[i]["id"], rows[j]["id"])

    members = defaultdict(list)
    for r in rows:
        members[find(r["id"])].append(r)

    return [
        {
            "question_ids": [m["id"] for m in group],
            "question_numbers": sorted(
                m["question_number"] for m in group
                if m.get("question_number") is not None
            ),
        }
        for group in members.values() if len(group) > 1
    ]


def find_cross_passage_duplicates(children, use_guards=True, threshold=0.95,
                                  min_letters=10, max_parents=4):
    """Find a sub-question reused under a DIFFERENT passage/parent - the
    one case a comprehension set's own structure cannot rule out by
    itself.

    `children` is store.linked_children_with_embeddings() - comparable
    sub-questions, each carrying a `parent_id`. Two failure modes run in
    opposite directions here, which is why this is a separate function
    from find_pairs() rather than a call to it with no changes:

    - Two sub-questions under the SAME parent are SUPPOSED to differ - a
      comprehension set of six questions on one passage is not six
      duplicates of each other. Comparing them would recreate exactly
      the false-positive storm that concatenating passage + question
      produces (measured: 0.978-0.987 for genuinely different questions
      sharing one passage) - so pairs sharing a parent_id are never
      compared at all, not merely demoted.
    - The same sub-question reused under a DIFFERENT parent_id, on a
      different paper, is a genuine repeat and today nothing else checks
      for it - sub-questions are excluded from every other check in this
      codebase (COMPARABLE requires shape='standalone') because a
      sub-question means nothing without its parent for context. Here,
      the parent_id itself is what makes the comparison safe: it is not
      being used to judge meaning, only to guarantee two sub-questions
      being compared did not come from the same six-question set.

    Three numbers here are measured against the real bank, not assumed -
    the first version of this shipped with threshold=REVIEW_THRESHOLD
    (0.85) and no filters, and that was wrong the same way the
    within-assessment check's first version was wrong:

    - min_letters drops a "sub-question" that is not real question text
      at all - an MCQ option letter ("c__________"), a raw number, a
      fill-in-the-blank stub. 2,241 of 16,967 real linked children (13%)
      fell under 10 real letters, and they dominated the highest-scoring
      "matches" (score 1.0, because the TEXT is identical, not because
      the content means anything - "5" matching "5" is not a duplicate).
    - max_parents drops a sub-question text recurring under more than
      this many DIFFERENT parents - the sub-question equivalent of the
      already-documented "Fill in the blanks" parent-row redundancy: a
      generic prompt every comprehension paper of a kind asks ("What is
      the title of the poem?"), not a one-off copy-paste duplicate.
    - threshold still matters after both filters: at 0.85 on the
      filtered real data, still 171,032 matches, 92% sitting in the
      0.85-0.90 band - the same raw-embedding-noise shape the
      whole-bank and within-assessment sweeps both have. At 0.95, 2,055
      pairs, and ten real random spot-checks were all genuine - exact
      repeats, and legitimate rewordings ("Explain the process of
      credit creation by commercial banks." vs "Explain how commercial
      banks 'create money' through the credit creation process.").

    Returns a list of dicts like find_pairs, highest score first.
    """
    children = [r for r in children
               if len(_LETTERS.findall(r["text"])) >= min_letters]

    by_norm = defaultdict(set)
    for r in children:
        by_norm[normalize(r["text"])].add(r["parent_id"])
    generic = {t for t, parents in by_norm.items() if len(parents) > max_parents}
    children = [r for r in children if normalize(r["text"]) not in generic]

    by_id = {r["id"]: r for r in children}
    pairs = find_pairs(children, use_guards=use_guards, threshold=threshold)
    return [
        p for p in pairs
        if by_id[p["a"]]["parent_id"] != by_id[p["b"]]["parent_id"]
    ]


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
