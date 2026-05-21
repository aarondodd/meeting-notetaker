#!/usr/bin/env bash
# Linux / macOS dev build via pyinstaller. The real production build runs on Windows;
# this exists so developers on non-Windows hosts can still produce a smoke binary.
set -euo pipefail

cd "$(dirname "$0")"

VENV="${VENV:-.venv}"
PYTHON="${PYTHON:-python3}"

if [ ! -d "$VENV" ]; then
    "$PYTHON" -m venv "$VENV"
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

pip install --upgrade pip
pip install -r requirements-dev.txt

rm -rf build dist
pyinstaller --noconfirm --clean meeting_notetaker.spec

# Post-build gate: invoke the freshly-built binary with --check-deps in a
# clean environment. If we leave the activated venv leaking through
# (VIRTUAL_ENV, PYTHONPATH, PYTHONHOME), the frozen binary may pick up
# the dev venv's site-packages and mask a missing-from-bundle dependency.
# Strip those vars + reduce PATH to the system default before running.
echo
echo "==> Running post-build dependency self-test (in clean env)..."
EXE="$(pwd)/dist/meeting-notetaker"
if ! env -u VIRTUAL_ENV -u PYTHONHOME -u PYTHONPATH \
       PATH="/usr/local/bin:/usr/bin:/bin" \
       "$EXE" --check-deps; then
    echo
    echo "ERROR: Build produced a binary with MISSING dependencies." >&2
    echo "See report above; add the missing module(s) to meeting_notetaker.spec hiddenimports or collect_all() and rebuild." >&2
    exit 1
fi

echo
echo "Built: $EXE"
