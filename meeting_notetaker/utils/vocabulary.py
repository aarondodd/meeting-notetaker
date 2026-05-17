"""Custom vocabulary for faster-whisper hotword biasing.

Plain text file at <data_dir>/vocabulary.txt -- one entry per line, '#'
introduces a comment. Entries get concatenated into a single string and
passed to `model.transcribe(..., hotwords=...)` to bias the decoder
toward proper nouns and corporate terms the model would otherwise miss.

Empty lines and comment lines are dropped; surrounding whitespace is
stripped. Duplicates (case-insensitive) collapse to the first occurrence.

Per-session derivation also lives here: attendee names from the
# Attendees block plus multi-word capitalized phrases pulled out of the
# Agenda block (or any provided text) get added on top of the global list
on each new recording.
"""
from __future__ import annotations

import re
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


# Two passes:
#  Pass 1 -- all-caps + hyphenated tokens (AWS, EDAPA-737, PRDP).
#  Pass 2 -- multi-word capitalized phrases (Snowflake Cortex, Customer
#            Combine), against the text with pass-1 matches blanked out so
#            "Ticket EDAPA-737" doesn't get captured as the multi-word
#            phrase "Ticket EDAPA" (which would shadow the real token).
_ALL_CAPS_RE = re.compile(r"\b[A-Z]{2,}[A-Z0-9-]*\b")
_MULTI_WORD_RE = re.compile(r"\b[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)+\b")


def extract_proper_nouns(text: str) -> list[str]:
    """Pull multi-word capitalized phrases + all-caps tokens out of text.

    Not a real NER -- there will be false positives (sentence-initial
    capitalized phrases, "Monday Sync"), but the cost of an extra hotword
    is minimal compared to the value of catching internal product names.
    Output preserves first-occurrence order and dedupes case-insensitively.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []

    def _add(m: str) -> None:
        key = m.lower()
        if key not in seen:
            seen.add(key)
            out.append(m)

    # Pass 1: claim all-caps tokens before the multi-word branch gets to
    # them; blank out the matched span so a sentence-initial word followed
    # by an all-caps token doesn't get captured as a spurious multi-word
    # phrase ("Ticket EDAPA").
    chars = list(text)
    for m in _ALL_CAPS_RE.finditer(text):
        _add(m.group())
        for i in range(m.start(), m.end()):
            chars[i] = " "
    residue = "".join(chars)

    # Pass 2: multi-word capitalized phrases against the punch-out residue.
    for m in _MULTI_WORD_RE.finditer(residue):
        _add(m.group())

    return out


def derive_session_hotwords(
    global_vocabulary: Iterable[str],
    *,
    attendees: Iterable[str] | None = None,
    agenda: str = "",
) -> list[str]:
    """Combine global vocabulary with attendee names + agenda proper nouns.

    Dedupes case-insensitively while preserving order: global vocab first,
    then attendees, then proper nouns from the agenda text. Empty entries
    are dropped.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(item: str) -> None:
        item = (item or "").strip()
        if not item:
            return
        key = item.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(item)

    for w in global_vocabulary:
        _add(w)
    for a in attendees or ():
        _add(a)
    for n in extract_proper_nouns(agenda):
        _add(n)
    return out
