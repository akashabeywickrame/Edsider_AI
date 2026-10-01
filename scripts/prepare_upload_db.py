"""Build a compacted copy of a local bank, ready to upload to a server.

The app on Azure App Service reads its database from persistent storage
(`/home/data/edsider_real.db`, see startup.sh) - the file is uploaded
once, by hand, because a 573 MB database of EdSider's real questions
does not belong in git (see .gitignore) and would be stale the day after
it was committed anyway.

This makes the file to upload:

  - a CONSISTENT snapshot, via SQLite's own backup API rather than a
    file copy - a plain copy taken while anything is writing can miss
    whatever is still in the -wal file, or catch a half-written page,
  - with the WAL folded in and switched off, so it is ONE file to
    upload rather than three,
  - VACUUMed, so pages freed by earlier deletes are not uploaded,
  - checked with `PRAGMA integrity_check` before it is handed over.

Nothing is written to the source database, which can stay open
elsewhere while this runs.

    python scripts/prepare_upload_db.py [source] [destination]

Default source `data/edsider_real.db`, default destination
`data/edsider_upload.db` - both under data/, which is gitignored.
"""

import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def prepare(source, destination):
    if destination.exists():
        destination.unlink()

    started = time.perf_counter()
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
        # One file to upload, not three: fold the -wal contents into the
        # main file and leave the copy in the plain journal mode. The app
        # switches WAL back on for itself in store.connect().
        dst.execute("PRAGMA journal_mode=DELETE")
        dst.execute("VACUUM")
        dst.commit()
        counts = {
            table: dst.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("questions", "embeddings", "question_assessments",
                          "decisions", "intake_questions")
        }
        integrity = dst.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        dst.close()
        src.close()

    return counts, integrity, time.perf_counter() - started


def main(argv):
    source = Path(argv[0]) if argv else ROOT / "data" / "edsider_real.db"
    destination = (Path(argv[1]) if len(argv) > 1
                   else ROOT / "data" / "edsider_upload.db")
    if not source.is_file():
        raise SystemExit(f"No database at {source}")

    counts, integrity, elapsed = prepare(source, destination)

    before = source.stat().st_size / 1e6
    after = destination.stat().st_size / 1e6
    print(f"source      {source}  {before:,.0f} MB")
    print(f"upload copy {destination}  {after:,.0f} MB")
    for table, n in counts.items():
        print(f"  {table:<22} {n:>8,} rows")
    print(f"  integrity_check        {integrity}")
    print(f"built in {elapsed:.0f}s")
    if integrity != "ok":
        raise SystemExit("integrity check failed - do not upload this file")


if __name__ == "__main__":
    main(sys.argv[1:])