#!/bin/sh
set -eu

export VERA_STATE_DIR="${VERA_STATE_DIR:-/var/lib/vera}"
mkdir -p "$VERA_STATE_DIR"
exec uvicorn app:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8080}" \
  --workers 1 --timeout-keep-alive 75
