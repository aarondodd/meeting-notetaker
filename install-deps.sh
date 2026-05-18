#!/usr/bin/env bash
# Install all Python dependencies into the local venv.
# Usage:  ./install-deps.sh
#
# Two-step install because Resemblyzer (speaker embedding) pulls original
# webrtcvad as a hard dep, and that package has no Windows wheel for
# Python 3.10+. Installing Resemblyzer with --no-deps skips that
# resolution; the deps it actually uses at runtime (librosa, scipy,
# torch, numpy, webrtcvad-wheels) are already pinned in requirements.txt.
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
pip install --no-deps "Resemblyzer>=0.1.4"

echo
echo "Done. Run:  python main.py"
