"""Find the best threshold, with and without the guard layer.

Sweeps every threshold from 0.70 to 0.99 twice - once with guards on,
once off - and reports the best of each. The gap between them is the
value the guard layer adds, measured rather than asserted.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import store, dedupe, evaluate
from src.embeddings import DEFAULT_MODEL

LINE = "=" * 72


def sweep(rows, gt, use_guards):
    best = None
    table = []
    t = 0.70
    while t <= 0.995:
        pairs = dedupe.find_pairs(rows, use_guards=use_guards, threshold=t)
        s = evaluate.score([(p["a"], p["b"]) for p in pairs], gt)
        table.append((round(t, 2), s))
        if best is None or s["f1"] > best[1]["f1"]:
            best = (round(t, 2), s)
        t += 0.01
    return best, table


def main():
    with store.connect() as conn:
        rows = [dict(r) for r in
                store.comparable_questions(conn, DEFAULT_MODEL)]

    if not rows:
        print("\nNo fingerprints. Run scripts/embed.py first.\n")
        return

    gt = evaluate.load_ground_truth()
    if not gt:
        print("\nNo ground_truth.json found.\n")
        return

    truth = evaluate.expected_duplicates(gt)
    print(f"\n{LINE}")
    print("THRESHOLD TUNING")
    print(LINE)
    print(f"Questions        : {len(rows)}")
    print(f"Real duplicates  : {len(truth)}  "
          f"(after transitive closure)")
    print(f"Known negatives  : {len(evaluate.known_negatives(gt))}")

    best_off, table_off = sweep(rows, gt, use_guards=False)
    best_on, table_on = sweep(rows, gt, use_guards=True)

    print(f"\n{LINE}")
    print("BEST SETTING WITHOUT GUARDS")
    print(LINE)
    show(best_off)

    print(f"\n{LINE}")
    print("BEST SETTING WITH GUARDS")
    print(LINE)
    show(best_on)

    # -- the headline comparison, at a fixed sensible threshold --
    fixed = 0.90
    off = next(s for t, s in table_off if t == fixed)
    on = next(s for t, s in table_on if t == fixed)

    print(f"\n{LINE}")
    print(f"SIDE BY SIDE AT THRESHOLD {fixed}")
    print(LINE)
    print(f"{'':24} {'guards OFF':>12} {'guards ON':>12}")
    print(f"{'pairs flagged':24} {off['flagged']:>12} {on['flagged']:>12}")
    print(f"{'real duplicates found':24} "
          f"{off['true_positives']:>12} {on['true_positives']:>12}")
    print(f"{'false alarms':24} "
          f"{off['false_positives']:>12} {on['false_positives']:>12}")
    print(f"{'precision':24} "
          f"{off['precision']:>12.3f} {on['precision']:>12.3f}")
    print(f"{'recall':24} "
          f"{off['recall']:>12.3f} {on['recall']:>12.3f}")
    print(f"{'F1':24} {off['f1']:>12.3f} {on['f1']:>12.3f}")

    removed = off["false_positives"] - on["false_positives"]
    kept = on["true_positives"]
    print()
    print(f"  The guard layer removed {removed} false alarms")
    print(f"  while keeping {kept} of {off['true_positives']} real duplicates.")

    print(f"\n{LINE}")
    print("FULL SWEEP (with guards)")
    print(LINE)
    print(f"{'thresh':>8} {'flagged':>9} {'found':>7} {'false':>7} "
          f"{'prec':>7} {'recall':>7} {'F1':>7}")
    for t, s in table_on:
        mark = "  <-- best" if t == best_on[0] else ""
        print(f"{t:>8.2f} {s['flagged']:>9} {s['true_positives']:>7} "
              f"{s['false_positives']:>7} {s['precision']:>7.3f} "
              f"{s['recall']:>7.3f} {s['f1']:>7.3f}{mark}")

    print(f"\n{LINE}")
    print("To apply the best setting, edit AUTO_THRESHOLD and")
    print("REVIEW_THRESHOLD at the top of src/dedupe.py")
    print(f"{LINE}\n")


def show(best):
    t, s = best
    print(f"  threshold        : {t}")
    print(f"  pairs flagged    : {s['flagged']}")
    print(f"  real found       : {s['true_positives']} of {s['expected']}")
    print(f"  false alarms     : {s['false_positives']}")
    print(f"  precision        : {s['precision']:.3f}")
    print(f"  recall           : {s['recall']:.3f}")
    print(f"  F1               : {s['f1']:.3f}")
    if s["missed"]:
        print(f"  missed           : "
              f"{', '.join(a + '/' + b for a, b in s['missed'][:6])}")


if __name__ == "__main__":
    main()
