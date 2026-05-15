"""Shared pytest fixtures.

isolated_data_dir steers app state into a tmp directory so tests never touch
the real %APPDATA% / XDG config tree.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Ensure the project root is on sys.path when running `pytest` from the project dir.
_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTETAKER_DATA_DIR", str(tmp_path / "mn"))
    yield tmp_path / "mn"
