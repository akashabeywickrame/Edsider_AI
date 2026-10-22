"""Copy the papers in the old intake tables into the `intake` schema.

Run once, by hand, when the version with the `intake` and `common` schemas
is deployed - at the same time as that deploy, because the deployed apps
and a local run share one database and the older version keeps writing
to the old tables (intake_documents, intake_questions, ...) until it is
replaced:

    .\\venv\\Scripts\\python.exe scripts\\migrate_intake_schema.py            # see what it would do
    .\\venv\\Scripts\\python.exe scripts\\migrate_intake_schema.py --apply    # do it

Without --apply everything is done and then rolled back, so the report
shows exactly what would be copied. Safe to re-run: a paper already
copied is skipped. The old tables are not touched; drop them by hand once
nothing runs the older version (see CLAUDE.md, "Intake schema").
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import store  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true",
                   help="commit the copy (without it, it is rolled back)")
    args = p.parse_args()

    print(f"Database: {store.display_name()}")
    conn = store.connect()
    try:
        report = store.migrate_old_intake_tables(conn)
        if args.apply:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(f"EdSider list entries copied: {report['lists']}")
    print(f"Papers: {report['documents']}   questions: {report['questions']}   "
          f"comments: {report['comments']}   pictures: {report['images']}")
    print(f"Papers given to their uploader for data entry: {report['operators']}")
    for doc_id, kind, value in report["unmatched"]:
        print(f"  paper #{doc_id}: {kind} {value!r} is not on EdSider's list - left unset")
    for doc_id, keys in report["dropped_settings"]:
        print(f"  paper #{doc_id}: settings not kept: {', '.join(keys)}")
    print("Committed." if args.apply else "Nothing saved - run again with --apply to copy.")


if __name__ == "__main__":
    main()
