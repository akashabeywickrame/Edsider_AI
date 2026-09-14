"""The last step of intake: once a person has corrected the extracted
questions, check that batch for repeats before it is allowed near the
bank.

"The batch" is one uploaded paper's worth of questions, so this is the
same problem as "Duplicates within a paper" on an already-imported
assessment - and it reuses the same engine and the same threshold. It
is kept here rather than in src/dedupe/ because it is part of the
intake flow (it needs an embedder passed in, it works on unsaved draft
rows), not part of the matching engine's own surface.
"""

from collections import defaultdict

from .. import dedupe
from ..normalize import content_hash

# Same value, same reason as dedupe.find_within_assessment_duplicates:
# measured on the real bank, 0.85 floods a small group with
# raw-embedding noise (MCQ options sharing vocabulary), 0.95 leaves
# genuine word-for-word repeats.
BATCH_THRESHOLD = 0.95


def check_batch(drafts, embedder, threshold=BATCH_THRESHOLD, use_guards=True):
    """Find repeats among the reviewed questions of one uploaded paper.

    `drafts` is dict-like rows with at least 'id' and 'text', optionally
    'grade' and 'subject' (used by the guard layer). `embedder` is
    injected so a test can pass a fake one - same pattern as the rest of
    this codebase's model-touching code.

    Returns {"exact_groups": [[id, id, ...], ...],
             "similar_pairs":  [<dedupe.find_pairs dict>, ...]}.
    Exact groups are Pass 1 (identical normalised text); similar pairs
    are Pass 2 + the guard layer, and never include a pair already
    covered by an exact group.
    """
    drafts = [d for d in drafts if (d.get("text") or "").strip()]
    if len(drafts) < 2:
        return {"exact_groups": [], "similar_pairs": []}

    by_hash = defaultdict(list)
    for d in drafts:
        by_hash[content_hash(d["text"])].append(d["id"])
    exact_groups = [ids for ids in by_hash.values() if len(ids) > 1]
    in_exact = {i for ids in exact_groups for i in ids}

    texts = [d["text"] for d in drafts]
    vectors = embedder.encode(texts, is_query=False)
    rows = [
        {
            "id": d["id"],
            "text": d["text"],
            "vector": [float(x) for x in v],
            "grade": d.get("grade"),
            "subject": d.get("subject"),
        }
        for d, v in zip(drafts, vectors)
    ]

    similar_pairs = [
        p for p in dedupe.find_pairs(rows, use_guards=use_guards,
                                     threshold=threshold)
        if not (p["a"] in in_exact and p["b"] in in_exact)
    ]

    return {"exact_groups": exact_groups, "similar_pairs": similar_pairs}
