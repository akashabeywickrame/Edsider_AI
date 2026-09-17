#!/usr/bin/env bash
# Start EdSider AI on a Linux host (Azure App Service or any server).
#
#   bash startup.sh        the duplicate-check API (service.py)
#   bash startup.sh ui     the review app (app.py, Streamlit)
#
# App Service, left to itself, picks app.py and expects a Flask or Django
# app inside it; ours is Streamlit, and the API is service.py, so the
# start command has to say which one to run.
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
  *)
    echo "startup: unknown mode '$mode' - use 'api' or 'ui'" >&2
    exit 2
    ;;
esac
