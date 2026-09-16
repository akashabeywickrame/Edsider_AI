"""Compute and store a meaning fingerprint for every question.

Run this once after loading. Fingerprints are cached in the database
keyed by content hash, so re-running only embeds what is new.

Usage:
    python scripts/embed.py                    every comparable question
    python scripts/embed.py --language English  one language at a time
    python scripts/embed.py --children          linked sub-questions instead

--language lets English go through the pipeline now and Sinhala/Tamil
wait. Nothing else needs to know about the split: comparable_questions()
joins on embeddings, so an un-embedded language just does not appear in
a duplicate check yet - re-run without --language (or with a different
one) whenever that language is ready to be measured.

--children is a separate pass, not a broader filter on the default one:
a sub-question is only worth fingerprinting once store.
link_children_to_parents() has given it a parent_id (see
store.COMPARABLE_CHILD) - run scripts/sync.py or import_edsider.py
first, since that is what sets parent_id.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import store
from src.dedupe.embeddings import Embedder, DEFAULT_MODEL


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--language", default=None,
                        help="only embed this language's rows, e.g. English")
    parser.add_argument("--children", action="store_true",
                        help="embed linked sub-questions instead of "
                             "standalone rows")
    args = parser.parse_args()

    store.init()

    with store.connect() as conn:
        total = store.count(conn)
        if total == 0:
            print("\nNo questions loaded. Run scripts/import_edsider.py "
                  "or scripts/sync.py first.\n")
            return

        finder = (store.children_needing_embedding if args.children
                 else store.questions_needing_embedding)
        pending = finder(conn, DEFAULT_MODEL, language=args.language)

        kind = "linked sub-questions" if args.children else "comparable questions"
        if not pending:
            scope = f" for language={args.language!r}" if args.language else ""
            print(f"\nAll {kind}{scope} already have fingerprints.")
            print(f"Stored fingerprints: {store.embedding_count(conn)}\n")
            return

        scope = f" ({args.language})" if args.language else ""
        print(f"\n{len(pending)} distinct texts need fingerprints{scope} "
              f"({kind}, out of {total} questions).\n")

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
