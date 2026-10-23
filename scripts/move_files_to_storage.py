"""Move uploaded papers and pictures out of the database into file storage.

Files used to be kept in PostgreSQL itself; src/files.py now keeps them in
Azure Blob Storage (EDSIDER_BLOB_CONNECTION_STRING), and the database only
where each one is. This moves the ones still held in the database:

    .\\venv\\Scripts\\python.exe scripts\\move_files_to_storage.py            # list what would move
    .\\venv\\Scripts\\python.exe scripts\\move_files_to_storage.py --apply    # move them

One file at a time, each committed on its own and read back before its
bytes are cleared from the database, so stopping part-way leaves every
file readable from one place or the other. Safe to run again.

Run it only once every app that opens papers has the same storage
settings: dev, pre-prod and a local run share one database, so an app
without them can no longer open a moved file.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import files, store  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true", help="move them (without it, only list)")
    args = p.parse_args()

    print(f"Database: {store.display_name()}")
    print(f"Files go to: {files.describe()}")
    with store.connect() as conn:
        todo = store.files_in_database(conn)
    total = sum(size or 0 for _, _, size in todo)
    papers = sum(1 for kind, _, _ in todo if kind == "paper")
    print(f"Still in the database: {papers} paper file(s), {len(todo) - papers} picture(s), "
          f"{total / 1e6:.1f} MB")
    if not args.apply:
        print("Nothing moved - run again with --apply.")
        return

    moved = 0
    for kind, item_id, size in todo:
        with store.connect() as conn:
            store.move_file_to_storage(conn, kind, item_id)
        moved += 1
        print(f"  {kind} {item_id}: {(size or 0) / 1e6:.2f} MB moved ({moved}/{len(todo)})")
    print(f"Moved {moved} file(s). PostgreSQL reuses the freed space itself; "
          "VACUUM FULL on the two tables returns it to the server sooner.")


if __name__ == "__main__":
    main()
