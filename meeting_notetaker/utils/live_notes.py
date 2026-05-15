"""Live notes template + attendee parsing.

The user takes notes during a meeting in parallel with the running transcript.
On a new session the live-notes file is seeded with a fixed template:

    # Attendees
    -

    # Agenda

    # Notes

    # Action Items

When the synthesis prompt is generated, the live notes are passed alongside
the transcript so the LLM can merge the user's framing/context with the
transcript-derived detail. Attendees are also parsed out of the bulleted
list under "# Attendees" and made available as a separate placeholder so
the LLM can assign action items to known people instead of "TBD".
"""
from __future__ import annotations

import re
from typing import Iterable


LIVE_NOTES_TEMPLATE = """# Attendees
-\x20

# Agenda

# Notes

# Action Items
"""

_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.*?)\s*$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.+?)\s*$")


def seed_body() -> str:
    """Initial content for a fresh live_notes.md file."""
    return LIVE_NOTES_TEMPLATE


def parse_attendees(body: str) -> list[str]:
    """Extract attendee names from the bulleted list under '# Attendees'.

    Looks for a heading whose text equals 'Attendees' (case-insensitive),
    then collects subsequent bullet lines until the next heading or until
    a non-empty non-bullet line that breaks the list. Empty bullets and
    placeholder dashes are ignored. Trailing comments after '--' or '#'
    on a line are stripped.
    """
    if not body:
        return []
    lines = body.splitlines()
    in_section = False
    out: list[str] = []
    for raw in lines:
        heading = _HEADING_RE.match(raw)
        if heading:
            if in_section:
                break
            if heading.group(1).strip().lower() == "attendees":
                in_section = True
            continue
        if not in_section:
            continue
        bullet = _BULLET_RE.match(raw)
        if bullet:
            name = _strip_trailing_comment(bullet.group(1)).strip()
            if name:
                out.append(name)
            continue
        if raw.strip() == "":
            continue
        # A non-empty non-bullet line ends the attendees list.
        break
    # Dedupe while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for name in out:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(name)
    return unique


def _strip_trailing_comment(value: str) -> str:
    # Allow "- Name -- notes" style annotations; keep only the name.
    for sep in (" -- ", " #"):
        idx = value.find(sep)
        if idx >= 0:
            return value[:idx]
    return value


def format_attendee_list(attendees: Iterable[str]) -> str:
    """Render the attendee list for prompt substitution."""
    names = [n for n in attendees if n]
    if not names:
        return "(none specified)"
    return ", ".join(names)


def has_user_content(body: str) -> bool:
    """True if the user has written anything beyond the seeded template."""
    if not body:
        return False
    stripped = body.strip()
    if not stripped:
        return False
    return stripped != LIVE_NOTES_TEMPLATE.strip()
