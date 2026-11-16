#!/bin/bash
# Create this edition's Python environment (.venv next to this folder's parent) and install the data layer's
# packages from PyPI: datafed, numpy, scikit-learn, joblib, the Globus SDK (globus-sdk, for uploads to ARC)
# and their dependencies (about 230 MB installed). On an existing .venv it adds what is missing, such as the
# Globus SDK, and removes nothing.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ -x "$ROOT/.venv/bin/python" ]; then
  echo "== adding what is missing to $ROOT/.venv =="
else
  BASE="${RAMAN_BASE_PYTHON:-}"
  if [ -z "$BASE" ]; then
    for cand in /opt/anaconda3/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
      if [ -x "$cand" ]; then BASE="$cand"; break; fi
    done
  fi
  [ -n "$BASE" ] || { echo "ERROR: no python3 found; set RAMAN_BASE_PYTHON"; exit 1; }
  echo "== creating $ROOT/.venv with $BASE =="
  "$BASE" -m venv "$ROOT/.venv"
fi
"$ROOT/.venv/bin/python" -m pip install --quiet --upgrade pip
# requirements-mac.txt is requirements.txt plus the Globus SDK. --prefer-binary takes the newest version that
# has a wheel for this Mac: on an Intel Mac that is the cryptography wheel the Globus SDK can use, not a build
# from source.
REQUIREMENTS="$ROOT/data_layer/requirements-mac.txt"
[ -f "$REQUIREMENTS" ] || REQUIREMENTS="$ROOT/data_layer/requirements.txt"
"$ROOT/.venv/bin/python" -m pip install --prefer-binary -r "$REQUIREMENTS"
echo "== done: $ROOT/.venv/bin/python =="
