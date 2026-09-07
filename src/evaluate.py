"""Scoring against the answer key.

Two things here that matter for correctness:

1. Duplicates are TRANSITIVE. If Q001 = Q002 and Q001 = Q005, then
   Q002 = Q005 must also count as a duplicate, even though nobody
   wrote that pair down. Without this the scorer punishes the system
   for being right.

2. Anything flagged that is neither a known duplicate nor a known
   hard negative is still counted as a false positive. Being unlabelled
   does not make it correct - it just means nobody wrote it down yet.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_ground_truth(path=None):
    path = Path(path) if path else ROOT / "data" / "ground_truth.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def transitive_closure(pairs):
    """Group ids that are all duplicates of each other, then return
    every pair within each group.

    Uses union-find: if A=B and B=C, all three end up in one group.
    """
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        parent[find(x)] = find(y)

    for a, b in pairs:
        union(a, b)

    groups = {}
    for node in parent:
        groups.setdefault(find(node), set()).add(node)

    closed = set()
    for members in groups.values():
        members = sorted(members)
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                closed.add((members[i], members[j]))
    return closed


def expected_duplicates(gt):
    direct = [
        (p["a"], p["b"])
        for key in ("exact_duplicates", "reworded_duplicates")
        for p in gt.get(key, [])
    ]
    return transitive_closure(direct)


def known_negatives(gt):
    return {
        tuple(sorted((p["a"], p["b"]))): p["why"]
        for p in gt.get("hard_negatives", [])
    }


def score(flagged_pairs, gt):
    """flagged_pairs: iterable of (a, b) tuples the system flagged."""
    found = {tuple(sorted(p)) for p in flagged_pairs}
    truth = expected_duplicates(gt)
    negatives = known_negatives(gt)

    true_pos = found & truth
    false_pos = found - truth          # anything not a real duplicate
    labelled_fp = false_pos & set(negatives)
    unlabelled_fp = false_pos - set(negatives)
    missed = truth - found

    precision = len(true_pos) / len(found) if found else 0.0
    recall = len(true_pos) / len(truth) if truth else 0.0
    f1 = (2 * precision * recall / (precision + recall)
          if precision + recall else 0.0)

    return {
        "flagged": len(found),
        "expected": len(truth),
        "true_positives": len(true_pos),
        "false_positives": len(false_pos),
        "labelled_false_positives": sorted(labelled_fp),
        "unlabelled_false_positives": sorted(unlabelled_fp),
        "missed": sorted(missed),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "negatives": negatives,
    }
