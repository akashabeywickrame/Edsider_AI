"""Stage 1: find exact duplicates by normalised-text fingerprint.

Zero cost, perfectly accurate, no AI. This catches roughly half of all
duplicates in a typical question bank before anything expensive runs -
879 groups, 913 redundant copies, 2.2% of the real bank, measured with
nothing but this.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import store

LINE = "-" * 68


def main():
    with store.connect() as conn:
        total = store.count(conn)
        if total == 0:
            print("\nNo questions in the database. Run scripts/import_edsider.py "
                  "or scripts/sync.py first.\n")
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

        print(f"\n{LINE}\n")


if __name__ == "__main__":
    main()
