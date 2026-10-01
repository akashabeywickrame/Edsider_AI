#!/usr/bin/env bash
# Start EdSider AI on a Linux host (Azure App Service or any server).
#
#   bash startup.sh          the duplicate-check API (service.py)
#   bash startup.sh ui       the data-entry review app (app.py, Streamlit)
#   bash startup.sh teacher  the teacher review app (teacher_app.py)
#   bash startup.sh assign   the paper-assignment app (masd_app.py)
#
# App Service, left to itself, picks app.py and expects a Flask or Django
# app inside it; ours is Streamlit, and the API is service.py, so the
# start command has to say which one to run. teacher_app.py and
# masd_app.py are deliberately separate deployments, not pages inside
# app.py - a data-entry person's app.py session should never be able to
# reach the teacher's screen, or the reverse, and a hidden sidebar entry
# in one shared app would not have guaranteed that the way running them
# as genuinely separate processes (their own App Service, their own URL)
# does.
#
# The database is PostgreSQL, reached over the network, so there is
# nothing to copy or unpack here any more - EDSIDER_DB is a connection
# string and both processes read the same live database. That also ends
# the old limitation of the SQLite build, where the app worked on a copy
# in /tmp and every decision a reviewer recorded was lost on restart.
#
# Environment:
#   EDSIDER_DB      PostgreSQL connection string (required)
#                   postgresql://user:password@host:5432/edsider_ai
#   EDSIDER_SCHEMA  schema to use, default public
#   PORT            port to listen on (default 8000)
set -euo pipefail

mode="${1:-api}"
port="${PORT:-8000}"

if [ -z "${EDSIDER_DB:-}" ]; then
  echo "startup: EDSIDER_DB is not set - it must be a PostgreSQL" >&2
  echo "         connection string, e.g." >&2
  echo "         postgresql://edsider_ai:PASSWORD@host:5432/edsider_ai" >&2
  exit 1
fi

case "$mode" in
  api)
    exec python -m uvicorn service:app --host 0.0.0.0 --port "$port"
    ;;
  ui)
    exec python -m streamlit run app.py \
      --server.port "$port" --server.address 0.0.0.0 --server.headless true
    ;;
  teacher)
    exec python -m streamlit run teacher_app.py \
      --server.port "$port" --server.address 0.0.0.0 --server.headless true
    ;;
  assign)
    exec python -m streamlit run masd_app.py \
      --server.port "$port" --server.address 0.0.0.0 --server.headless true
    ;;
  *)
    echo "startup: unknown mode '$mode' - use 'api', 'ui', 'teacher' or 'assign'" >&2
    exit 2
    ;;
esac
