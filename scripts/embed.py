"""Compute and store a meaning fingerprint for every question.

Run this once after loading. Fingerprints are cached in the database
keyed by content hash, so re-running only embeds what is new.

Usage:
    python scripts/embed.py                    every comparable question
    python scripts/embed.py --language English  one language at a time

--language lets English go through the pipeline now and Sinhala/Tamil
wait. Nothing else needs to know about the split: comparable_questions()
joins on embeddings, so an un-embedded language just does not appear in
a duplicate check yet - re-run without --language (or with a different
one) whenever that language is ready to be measured.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import store
from src.embeddings import Embedder, DEFAULT_MODEL


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--language", default=None,
                        help="only embed this language's rows, e.g. English")
    args = parser.parse_args()

    store.init()

    with store.connect() as conn:
        total = store.count(conn)
        if total == 0:
            print("\nNo questions loaded. Run scripts/load.py first.\n")
            return

        pending = store.questions_needing_embedding(
            conn, DEFAULT_MODEL, language=args.language)

        if not pending:
            scope = f" for language={args.language!r}" if args.language else ""
            print(f"\nAll comparable questions{scope} already have "
                  "fingerprints.")
            print(f"Stored fingerprints: {store.embedding_count(conn)}\n")
            return

        scope = f" ({args.language})" if args.language else ""
        print(f"\n{len(pending)} distinct texts need fingerprints{scope} "
              f"(out of {total} questions).\n")

        embedder = Embedder()
        texts = [r["text"] for r in pending]
        vectors = embedder.encode(texts, is_query=False)

        for row, vec in zip(pending, vectors):
            store.save_embedding(conn, row["content_hash"],
                                 embedder.model_name, vec)

        print(f"Stored {len(pending)} fingerprints.")
        print(f"Each is a list of {len(vectors[0])} numbers.")
        print(f"Total in database: {store.embedding_count(conn)}\n")


if __name__ == "__main__":
    main()
