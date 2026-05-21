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

# Post-build gate: invoke the freshly-built binary with --check-deps. If
# any dependency is MISSING, fail loudly here rather than producing a
# binary that will silently skip features at runtime.
echo
echo "==> Running post-build dependency self-test..."
EXE="$(pwd)/dist/meeting-notetaker"
if ! "$EXE" --check-deps; then
    echo
    echo "ERROR: Build produced a binary with MISSING dependencies." >&2
    echo "See report above; add the missing module(s) to meeting_notetaker.spec hiddenimports or collect_all() and rebuild." >&2
    exit 1
fi

echo
echo "Built: $EXE"
