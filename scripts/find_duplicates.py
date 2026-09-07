"""Stage 1: find exact duplicates by normalised-text fingerprint.

Zero cost, perfectly accurate, no AI. This catches roughly half of all
duplicates in a typical question bank before anything expensive runs.

Also scores itself against data/ground_truth.json so you can see whether
it found what you planted.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import store

LINE = "-" * 68


def pairs_from_groups(groups):
    """Turn duplicate groups into a set of unordered id pairs."""
    found = set()
    for g in groups:
        ids = sorted(g["ids"].split(","))
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                found.add((ids[i], ids[j]))
    return found


def main():
    with store.connect() as conn:
        total = store.count(conn)
        if total == 0:
            print("\nNo questions in the database. Run scripts/load.py first.\n")
            return

        groups = store.exact_duplicate_groups(conn)
        shapes = store.shape_counts(conn)
        comparable = sum(r["comparable"] for r in shapes)
        redundant = sum(g["copies"] for g in groups) - len(groups)

        print(f"\n{LINE}")
        print("STAGE 1 - EXACT DUPLICATE DETECTION")
        print(LINE)
        print(f"Questions in bank : {total:,}")
        if len(shapes) > 1:
            for r in shapes:
                print(f"  {r['shape']:<14} {r['total']:>7,}  "
                      f"({r['comparable']:,} comparable)")
        print(f"Compared          : {comparable:,}")
        print(f"Duplicate groups  : {len(groups):,}")
        print(f"Redundant copies  : {redundant:,}"
              + (f"  ({redundant / comparable * 100:.1f}% of what was "
                 "compared)" if comparable else ""))
        print(LINE)

        SHOW = 25
        for g in groups[:SHOW]:
            ids = g["ids"].split(",")
            print(f"\n  {g['copies']} copies -> {', '.join(ids[:8])}"
                  f"{' ...' if len(ids) > 8 else ''}")
            print(f"    \"{g['sample'][:70]}\"")
            for qid in ids[:8]:
                row = store.get(conn, qid)
                print(f"      {qid}  by {row['contributor']}  "
                      f"({row['source_file']})")

        if len(groups) > SHOW:
            print(f"\n  ... and {len(groups) - SHOW:,} more groups.")

        if not groups:
            print("\n  No exact duplicates found.")

        # ---- score against the answer key ----
        gt_path = ROOT / "data" / "ground_truth.json"
        if not gt_path.exists():
            print(f"\n{LINE}\n")
            return

        gt = json.loads(gt_path.read_text(encoding="utf-8"))
        expected = {
            tuple(sorted((p["a"], p["b"])))
            for p in gt.get("exact_duplicates", [])
        }

        # The answer key describes the 30-question sample. Scoring a real
        # EdSider import against it would report every real duplicate as
        # "unexpected" and every sample pair as "missed" - noise, not a
        # measurement. Only score when the bank is the one it describes.
        keys = {qid for pair in expected for qid in pair}
        if keys and not any(store.get(conn, qid) for qid in keys):
            print(f"\n{LINE}")
            print("Not scored: data/ground_truth.json describes the sample")
            print("bank, and none of its questions are in this database.")
            print("Real thresholds need verdicts from a teacher - see the")
            print("Confirm / Not-a-duplicate buttons in the app.")
            print(f"{LINE}\n")
            return

        found = pairs_from_groups(groups)

        hits = expected & found
        missed = expected - found
        extra = found - expected

        print(f"\n{LINE}")
        print("SCORED AGAINST data/ground_truth.json")
        print(LINE)
        print(f"  Expected exact duplicates : {len(expected)}")
        print(f"  Correctly found           : {len(hits)}")
        print(f"  Missed                    : {len(missed)}")
        print(f"  Unexpected                : {len(extra)}")

        if missed:
            print("\n  MISSED - normalisation may be too weak:")
            for a, b in sorted(missed):
                print(f"    {a} / {b}")

        if extra:
            print("\n  UNEXPECTED - normalisation may be too aggressive:")
            for a, b in sorted(extra):
                print(f"    {a} / {b}")

        if not missed and not extra:
            print("\n  PASS - exactly the planted duplicates, no more, no fewer.")

        remaining = len(gt.get("reworded_duplicates", []))
        print(f"\n  Still to find: {remaining} reworded duplicates.")
        print("  Those need Stage 2 (embeddings) - that is Task 2.")
        print(f"{LINE}\n")


if __name__ == "__main__":
    main()
