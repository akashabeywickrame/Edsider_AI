"""Load questions from JSON into the database, computing fingerprints."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import store
from src.normalize import content_hash, has_attachments


def main():
    store.init()

    path = ROOT / "data" / "questions.json"
    questions = json.loads(path.read_text(encoding="utf-8"))

    with store.connect() as conn:
        for q in questions:
            q["content_hash"] = content_hash(q["text"])
            q["has_figure"] = has_attachments(q["text"])
            store.save(conn, q)

        total = store.count(conn)

    print(f"\nLoaded {len(questions)} questions from {path.name}")
    print(f"Database now holds {total} questions")
    print(f"Database file: {store.DB_PATH}\n")


if __name__ == "__main__":
    main()
