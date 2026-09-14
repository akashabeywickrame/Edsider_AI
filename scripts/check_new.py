"""Check ONE new question against the bank before it is added.

This is the check EdSider actually needs. The app's "Find duplicates"
page sweeps the whole bank against itself, which answers "what
duplicates already exist". This answers the question a teacher asks at
save time: "is the thing I just wrote already in here?"

The difference matters at scale. A sweep of 20,000 questions is 200
million comparisons. Checking one question against 20,000 is 20,000 -
and Pass 1 below usually settles it without any comparison at all.

Usage:
    python scripts/check_new.py "How do plants make their own food?"
    python scripts/check_new.py --file draft.json
    python scripts/check_new.py "..." --grade 8 --subject Mathematics
    python scripts/check_new.py "..." --add --id Q031

Options:
    --file PATH     read the question text from a file instead of argv
    --grade G       grade, so the metadata guard can use it
    --subject S     subject, likewise
    --top N         how many near matches to show (default 5)
    --threshold T   similarity floor (default: dedupe.REVIEW_THRESHOLD)
    --no-guards     turn the safety checks off, to see what they cost
    --add           add the question if, and only if, nothing was flagged
    --id ID         the id to store it under; required with --add
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import store, dedupe
from src.dedupe.embeddings import DEFAULT_MODEL
from src.normalize import content_hash, extract_text, has_attachments

LINE = "-" * 70


def parse_args():
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("text", nargs="?")
    p.add_argument("--file")
    p.add_argument("--grade")
    p.add_argument("--subject")
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--threshold", type=float, default=dedupe.REVIEW_THRESHOLD)
    p.add_argument("--no-guards", dest="guards", action="store_false")
    p.add_argument("--add", action="store_true")
    p.add_argument("--id")
    p.add_argument("-h", "--help", action="store_true")
    return p.parse_args()


def read_question(args):
    """Return (raw_value, text). The raw value is kept because rich text
    carries figure information that the plain text alone loses."""
    if args.file:
        path = Path(args.file)
        if not path.exists():
            print(f"\nFile not found: {path}\n")
            return None, None
        raw = path.read_text(encoding="utf-8")
        # A JSON file may be a whole question record rather than bare text.
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return raw, extract_text(raw)
        if isinstance(data, dict) and "text" in data:
            args.grade = args.grade or data.get("grade")
            args.subject = args.subject or data.get("subject")
            args.id = args.id or data.get("id")
            raw = data["text"]
        return raw, extract_text(raw)

    return args.text, extract_text(args.text)


def main():
    args = parse_args()
    if args.help or (not args.text and not args.file):
        print(__doc__)
        return

    raw, text = read_question(args)
    if not text:
        print("\nNo question text found.\n")
        return

    new_hash = content_hash(text)
    question = {
        "id": args.id,
        "text": text,
        "grade": args.grade,
        "subject": args.subject,
    }

    with store.connect() as conn:
        total = store.count(conn)
        exact = store.find_by_hash(conn, new_hash, exclude_id=args.id)

    print(f"\n{LINE}")
    print("CHECKING ONE QUESTION AGAINST THE BANK")
    print(LINE)
    print(f"Question  : {text[:200]}")
    print(f"Grade     : {args.grade or '-'}")
    print(f"Subject   : {args.subject or '-'}")
    print(f"Bank size : {total} questions")
    if has_attachments(raw):
        print("Note      : contains an image or equation - the text alone")
        print("            does not fully represent this question.")
    print(LINE)

    # ---- Pass 1: exact match. Free, and usually decisive. ----
    print("\nPASS 1 - exact match (hash lookup, no AI)")
    if exact:
        for row in exact:
            print(f"  EXACT DUPLICATE of {row['id']}")
            print(f"    {row['text'][:120]}")
        print("\n  Stopping here. An exact match needs no embedding, so")
        print("  the expensive passes never run.")
        print(f"\n{LINE}")
        print("VERDICT: EXACT DUPLICATE - do not add.")
        print(f"{LINE}\n")
        return
    print("  No exact match.")

    # ---- Passes 2 and 3: fingerprint, then guards. ----
    with store.connect() as conn:
        rows = [dict(r) for r in
                store.comparable_questions(conn, DEFAULT_MODEL)]

    if not rows:
        print("\nNo fingerprints in the bank. Run scripts/embed.py first.\n")
        return

    from src.dedupe.embeddings import Embedder
    embedder = Embedder()
    # passage, not query: the bank was embedded as passages, and a
    # duplicate check compares like with like. The query prefix belongs
    # to search, where a short query meets a longer document.
    question["vector"] = embedder.encode(text, is_query=False)[0]

    matches = dedupe.check_one(
        question, rows,
        use_guards=args.guards,
        threshold=args.threshold,
        top_k=args.top,
    )

    label = "with safety checks" if args.guards else "safety checks OFF"
    print(f"\nPASS 2 + 3 - meaning fingerprint, {label}")
    print(f"  Compared against {len(rows)} fingerprinted questions.")

    if not matches:
        print(f"  Nothing above {args.threshold:.2f}. This question looks new.")
    for n, m in enumerate(matches, start=1):
        row = m["match"]
        print(f"\n  {n}. {m['verdict']:<18} {m['score']:.3f}   {row['id']}")
        print(f"       {row['text'][:110]}")
        print(f"       grade {row['grade'] or '-'} - "
              f"{row['subject'] or '-'} - {row['topic'] or 'untagged'}")
        if m["reasons"]:
            print(f"       score lowered from {m['raw_score']:.3f} "
                  f"by the safety checks:")
            for r in m["reasons"]:
                print(f"         - {r}")

    flagged = [m for m in matches if m["score"] >= args.threshold]

    print(f"\n{LINE}")
    if not flagged:
        print("VERDICT: NO DUPLICATE FOUND - safe to add.")
        if matches:
            print(f"         {len(matches)} question(s) scored high before the")
            print("         safety checks, and every one was ruled out above.")
    else:
        top = flagged[0]
        print(f"VERDICT: {top['verdict']} of {top['match']['id']} "
              f"({top['score']:.3f}) - review before adding.")
    print(LINE)

    # ---- Optional add. A person still decides; this only refuses. ----
    if args.add:
        if flagged:
            print("\nNot added. Something was flagged, and nothing is added")
            print("automatically over a flag - a reviewer decides that.\n")
            return
        if not args.id:
            print("\nNot added. --add needs --id to store the question under.\n")
            return
        with store.connect() as conn:
            store.save(conn, {
                "id": args.id,
                "text": text,
                "grade": args.grade,
                "subject": args.subject,
                "content_hash": new_hash,
                "has_figure": has_attachments(raw),
            })
            store.save_embedding(conn, new_hash, embedder.model_name,
                                 question["vector"])
            print(f"\nAdded {args.id}. Bank now holds {store.count(conn)} "
                  "questions.\n")
    else:
        print()


if __name__ == "__main__":
    main()
