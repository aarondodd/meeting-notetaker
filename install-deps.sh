#!/usr/bin/env bash
# Install all Python dependencies into the local venv.
# Usage:  ./install-deps.sh
set -euo pipefail

cd "$(dirname "$0")"

VENV="${VENV:-.venv}"
PYTHON="${PYTHON:-python3}"

if [ ! -d "$VENV" ]; then
    echo "Creating virtualenv at $VENV..."
    "$PYTHON" -m venv "$VENV"
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

pip install --upgrade pip
pip install -r requirements-dev.txt

echo
echo "Done. Run:  python main.py"
