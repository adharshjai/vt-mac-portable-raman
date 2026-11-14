#!/bin/bash
# Create this edition's Python environment (.venv next to this folder's parent) and install the data layer's
# packages from PyPI: datafed, numpy, scikit-learn, joblib and their dependencies (about 215 MB installed).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BASE="${RAMAN_BASE_PYTHON:-}"
if [ -z "$BASE" ]; then
  for cand in /opt/anaconda3/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if [ -x "$cand" ]; then BASE="$cand"; break; fi
  done
fi
[ -n "$BASE" ] || { echo "ERROR: no python3 found; set RAMAN_BASE_PYTHON"; exit 1; }
echo "== creating $ROOT/.venv with $BASE =="
"$BASE" -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/python" -m pip install --quiet --upgrade pip
"$ROOT/.venv/bin/python" -m pip install -r "$ROOT/data_layer/requirements.txt"
echo "== done: $ROOT/.venv/bin/python =="
