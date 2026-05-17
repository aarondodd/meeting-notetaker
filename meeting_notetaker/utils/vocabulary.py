"""Custom vocabulary for faster-whisper hotword biasing.

Plain text file at <data_dir>/vocabulary.txt -- one entry per line, '#'
introduces a comment. Entries get concatenated into a single string and
passed to `model.transcribe(..., hotwords=...)` to bias the decoder
toward proper nouns and corporate terms the model would otherwise miss.

Empty lines and comment lines are dropped; surrounding whitespace is
stripped. Duplicates (case-insensitive) collapse to the first occurrence.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .paths import vocabulary_path


_DEFAULT_SEED = """# Custom vocabulary for Meeting Notetaker
#
# One word or phrase per line. Lines starting with '#' are comments.
# These hints bias the transcription model toward proper nouns,
# acronyms, and corporate terms it would otherwise mis-hear.
#
# Examples (delete and replace with your own):
# Snowflake Cortex
# Informatica MDM
# EDAPA-737
# Plantronics Voyager

"""


def seed_vocabulary_file() -> Path:
    """Create vocabulary.txt with a seed comment if it doesn't exist. Returns the path."""
    path = vocabulary_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_DEFAULT_SEED, encoding="utf-8")
    return path


def parse_vocabulary(text: str) -> list[str]:
    """Parse lines into a list of hotwords. Strips comments, blanks, dedupes case-insensitively."""
    seen_lower: set[str] = set()
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key = line.lower()
        if key in seen_lower:
            continue
        seen_lower.add(key)
        out.append(line)
    return out


def load_vocabulary() -> list[str]:
    """Load and parse the vocabulary file. Returns [] if missing or unreadable."""
    path = vocabulary_path()
    if not path.exists():
        return []
    try:
        return parse_vocabulary(path.read_text(encoding="utf-8"))
    except OSError:
        return []


def join_hotwords(hotwords: Iterable[str]) -> str:
    """Concatenate hotwords for faster-whisper's `hotwords` param (single string)."""
    return " ".join(h.strip() for h in hotwords if h.strip())
