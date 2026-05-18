#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found. Install Python 3.10+ from https://www.python.org/downloads/ and re-run."
  exit 1
fi

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

VENV_PY="$(pwd)/.venv/bin/python"

if [ ! -x "$VENV_PY" ]; then
  echo "Virtualenv python not found at $VENV_PY"
  echo "Try deleting .venv and re-running."
  exit 1
fi

"$VENV_PY" -m pip install --upgrade pip
"$VENV_PY" -m pip install -r requirements.txt

PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"

if command -v lsof >/dev/null 2>&1; then
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    for candidate in 8001 8002 8003 8004 8005; do
      if ! lsof -nP -iTCP:"$candidate" -sTCP:LISTEN >/dev/null 2>&1; then
        PORT="$candidate"
        break
      fi
    done
  fi
fi

echo "Starting server at http://$HOST:$PORT"

"$VENV_PY" -m uvicorn app.main:app --reload --host "$HOST" --port "$PORT"
