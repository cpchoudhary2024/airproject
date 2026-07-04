#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# clean.sh — purge regenerable junk so the project never bloats.
#
# Deletes ONLY things that are caches or regenerated automatically:
#   • Python bytecode caches (__pycache__, *.pyc/*.pyo)
#   • macOS .DS_Store droppings
#   • Local runtime analysis outputs (app/data/*)  ← recreated on every upload
#   • Test-run output dirs (test_output*, test_stl*)
#   • Matplotlib / pytest / mypy caches
#   • The virtualenv (.venv)  ← only with --deep; recreate via ./clean.sh --venv
#
# It NEVER touches source code, raw data, reports, or the git repo.
#
# Usage:
#   ./clean.sh          # normal cleanup (keeps .venv)
#   ./clean.sh --deep   # also removes .venv
#   ./clean.sh --venv   # (re)create .venv and install requirements
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")"

recreate_venv() {
  echo "→ Recreating .venv and installing requirements…"
  rm -rf .venv
  python3 -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r requirements.txt
  echo "✓ .venv ready."
}

if [[ "${1:-}" == "--venv" ]]; then
  recreate_venv
  exit 0
fi

before=$(du -sh . 2>/dev/null | cut -f1)

echo "→ Removing Python caches…"
find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} + 2>/dev/null || true
find . \( -name '*.pyc' -o -name '*.pyo' \) -not -path './.venv/*' -delete 2>/dev/null || true

echo "→ Removing .DS_Store files…"
find . -name '.DS_Store' -delete 2>/dev/null || true

echo "→ Removing local runtime outputs (app/data/*)…"
rm -rf app/data/* 2>/dev/null || true

echo "→ Removing test-run output dirs…"
rm -rf test_output test_output_* test_stl_* 2>/dev/null || true

echo "→ Removing tool caches (.pytest_cache, .mypy_cache, .ipynb_checkpoints)…"
find . -type d \( -name '.pytest_cache' -o -name '.mypy_cache' -o -name '.ipynb_checkpoints' \) \
  -not -path './.venv/*' -exec rm -rf {} + 2>/dev/null || true

if [[ "${1:-}" == "--deep" ]]; then
  echo "→ --deep: removing .venv…"
  rm -rf .venv
fi

after=$(du -sh . 2>/dev/null | cut -f1)
echo "✓ Clean complete.  Size: ${before} → ${after}"
