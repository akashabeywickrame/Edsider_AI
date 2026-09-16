#!/usr/bin/env bash
# Start EdSider AI on a Linux host (Azure App Service or any server).
#
#   bash startup.sh        the duplicate-check API (service.py)
#   bash startup.sh ui     the review app (app.py, Streamlit)
#
# App Service keeps /home on a network share, and SQLite's WAL mode -
# which store.connect() switches on so the API can read while something
# else writes - needs shared memory a network share cannot reliably
# provide. So the database is uploaded once to persistent storage and
# copied to local disk on every start, and served from the copy. What the
# app writes (reviewer decisions, uploaded papers) lands in that copy and
# is lost on restart: fine for a pre-production check, not for production,
# which moves to PostgreSQL.
#
# Environment:
#   EDSIDER_DB_SOURCE  where the uploaded database lives
#                      (default /home/data/edsider_real.db)
#   EDSIDER_DB         the local copy the app reads (default /tmp/edsider_real.db)
#   PORT               port to listen on (default 8000)
set -euo pipefail

mode="${1:-api}"
source_db="${EDSIDER_DB_SOURCE:-/home/data/edsider_real.db}"
export EDSIDER_DB="${EDSIDER_DB:-/tmp/edsider_real.db}"
port="${PORT:-8000}"

if [ ! -f "$source_db" ]; then
  echo "startup: no database at $source_db - upload it there, or set EDSIDER_DB_SOURCE" >&2
  exit 1
fi

if [ "$source_db" != "$EDSIDER_DB" ]; then
  echo "startup: copying $source_db to $EDSIDER_DB"
  cp "$source_db" "$EDSIDER_DB"
  # Changes not yet checkpointed into the main file live in the -wal
  # file; without it the copy would be missing them.
  if [ -f "$source_db-wal" ]; then
    cp "$source_db-wal" "$EDSIDER_DB-wal"
  fi
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
