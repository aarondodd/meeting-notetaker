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
# Resemblyzer is installed with --no-deps to skip its hard `webrtcvad`
# dependency (no Windows wheel for Python 3.10+). Its actual runtime
# deps are in requirements.txt; see the long comment there.
pip install --no-deps "Resemblyzer>=0.1.4"

rm -rf build dist
pyinstaller --noconfirm --clean meeting_notetaker.spec

echo
echo "Built: $(pwd)/dist/meeting-notetaker"
