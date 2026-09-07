"""Stage 2: find reworded duplicates using meaning fingerprints.

Exact matching (Stage 1) only catches identical text. This catches
questions that mean the same thing but are worded differently.

IMPORTANT: this stage deliberately has NO safety checks yet. Expect it
to wrongly flag some pairs - area vs perimeter, mean vs median, and so
on. Seeing those false alarms is the point. Task 3 fixes them.
"""

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import store, evaluate
from src.embeddings import DEFAULT_MODEL, similarity_matrix

LINE = "-" * 70

# Provisional bands. These are guesses until tuned against real data -
# which is exactly what the scoring at the bottom is for.
AUTO_THRESHOLD = 0.92
REVIEW_THRESHOLD = 0.85


def band(score):
    if score >= AUTO_THRESHOLD:
        return "LIKELY DUPLICATE"
    if score >= REVIEW_THRESHOLD:
        return "NEEDS REVIEW"
    return "DISTINCT"


def main():
    with store.connect() as conn:
        rows = store.comparable_questions(conn, DEFAULT_MODEL)

    if not rows:
        print("\nNo fingerprints found. Run scripts/embed.py first.\n")
        return

    ids = [r["id"] for r in rows]
    texts = {r["id"]: r["text"] for r in rows}
    vectors = np.array([json.loads(r["vector"]) for r in rows])

    sim = similarity_matrix(vectors)

    # Collect every pair above the review threshold.
    flagged = []
    n = len(ids)
    for i in range(n):
        for j in range(i + 1, n):
            score = float(sim[i][j])
            if score >= REVIEW_THRESHOLD:
                flagged.append((ids[i], ids[j], score))
    flagged.sort(key=lambda x: -x[2])

    total_pairs = n * (n - 1) // 2

    print(f"\n{LINE}")
    print("STAGE 2 - SIMILARITY MATCHING (no safety checks yet)")
    print(LINE)
    print(f"Questions compared : {n}")
    print(f"Pairs examined     : {total_pairs}")
    print(f"Pairs flagged      : {len(flagged)}")
    print(LINE)

    gt = evaluate.load_ground_truth()
    known_dupes = evaluate.expected_duplicates(gt) if gt else set()
    negatives = evaluate.known_negatives(gt) if gt else {}

    for a, b, score in flagged:
        key = tuple(sorted((a, b)))
        if key in known_dupes:
            mark = "CORRECT   "
        elif key in negatives:
            mark = "WRONG !!! "
        else:
            mark = "unlabelled"

        print(f"\n  {mark} {a} / {b}   {score:.3f}   {band(score)}")
        print(f"    A: {texts[a][:64]}")
        print(f"    B: {texts[b][:64]}")
        if key in negatives:
            print(f"    -> these are NOT duplicates: {negatives[key]}")

    if not gt:
        print(f"\n{LINE}\n")
        return

    # ---- score it ----
    s = evaluate.score([(a, b) for a, b, _ in flagged], gt)

    print(f"\n{LINE}")
    print("SCORED AGAINST data/ground_truth.json")
    print(LINE)
    print(f"  Real duplicates found   : {s['true_positives']} of {s['expected']}")
    print(f"  False alarms            : {s['false_positives']}")
    print(f"  Missed                  : {len(s['missed'])}")
    print()
    print(f"  Precision : {s['precision']:.3f}   (of what we flagged, how much was right)")
    print(f"  Recall    : {s['recall']:.3f}   (of the real duplicates, how many we found)")
    print(f"  F1        : {s['f1']:.3f}")

    if s["missed"]:
        print("\n  MISSED:")
        for a, b in s["missed"]:
            i, j = ids.index(a), ids.index(b)
            print(f"    {a} / {b}   scored {sim[i][j]:.3f}")

    if s["false_positives"]:
        print(f"\n{LINE}")
        print("  THIS IS THE PROBLEM THE GUARD LAYER SOLVES")
        print(LINE)
        print(f"  {s['false_positives']} pairs flagged that are NOT duplicates.")
        print("  Run scripts/tune.py to see what the guards do to this number.")

    print(f"{LINE}\n")


if __name__ == "__main__":
    main()
