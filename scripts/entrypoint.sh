#!/bin/sh
set -e

alembic upgrade head
SCRATCH="${LOCAL_STORAGE_PATH:-/app/data/media}/.scratch"
mkdir -p "$SCRATCH"
export TMPDIR="$SCRATCH"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
