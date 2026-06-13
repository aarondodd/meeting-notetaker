"""Parse the LLM's "Attendee Context (auto-extracted)" appendix (#63).

Sibling parser to attendee_appendix.py / topic_appendix.py. The LLM
emits per-attendee context observations (passive participation,
unlisted speakers, role dynamics) into a JSON code block. The
Appendix tray renders the parsed list as a table; the strip path
removes the section on save when the user opts in.

Tolerant of malformed input: missing section, missing JSON, bad
JSON, wrong shape -> empty list, never raises. Mirrors the
attendee_appendix posture.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional


log = logging.getLogger(__name__)


@dataclass
class AttendeeContextEntry:
    """One row of the LLM-extracted attendee-context appendix.

    `observation` is a single-sentence free-form string; the LLM is
    instructed to use the same name spelling as the transcript so
    the app can match the observation back to a Contact record.
    """
    name: str
    observation: str = ""


_APPENDIX_HEADING_RE = re.compile(
    # The "(auto-extracted)" suffix is the system-prompt's spec but
    # Claude routinely drops it ("## Attendee Context" alone), and a
    # missing suffix silently dropped the JSON before it ever
    # reached the appendix tray (#102 bug 9). Make the suffix
    # optional; any user-edited section that happens to share the
    # heading without JSON content still degrades to an empty list
    # via the inner JSON-block check, so loosening here is safe.
    r"(##\s+Attendee\s+Context(?:\s*\(auto-extracted\))?\b.*?)(?=^##\s|\Z)",
    re.IGNORECASE | re.DOTALL | re.MULTILINE,
)
_JSON_CODE_BLOCK_RE = re.compile(
    r"```(?:json)?\s*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)


def find_appendix_span(markdown: str) -> Optional[tuple[int, int]]:
    """Return (start, end) of the Attendee Context section, or None."""
    if not markdown:
        return None
    m = _APPENDIX_HEADING_RE.search(markdown)
    if m is None:
        return None
    return (m.start(), m.end())


def parse_attendee_context(markdown: str) -> list[AttendeeContextEntry]:
    """Extract the JSON array of attendee-context entries.

    Returns an empty list on any failure (missing section, missing
    JSON block, unparseable JSON, wrong top-level shape). Entries
    without a non-empty ``name`` field are skipped.
    """
    span = find_appendix_span(markdown)
    if span is None:
        return []
    section = markdown[span[0]:span[1]]
    code = _JSON_CODE_BLOCK_RE.search(section)
    if code is None:
        log.debug("attendee context: no JSON code block in section")
        return []
    try:
        data = json.loads(code.group(1))
    except json.JSONDecodeError:
        log.warning(
            "attendee context: JSON failed to parse; payload=%r",
            code.group(1)[:200],
        )
        return []
    if not isinstance(data, list):
        log.warning(
            "attendee context: top-level JSON was %s, expected list",
            type(data).__name__,
        )
        return []
    out: list[AttendeeContextEntry] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", "") or "").strip()
        if not name:
            continue
        out.append(AttendeeContextEntry(
            name=name,
            observation=str(raw.get("observation", "") or "").strip(),
        ))
    return out


def strip_appendix(markdown: str) -> str:
    """Return ``markdown`` with the Attendee Context appendix removed."""
    span = find_appendix_span(markdown)
    if span is None:
        return markdown
    return (markdown[:span[0]].rstrip() + "\n" + markdown[span[1]:]).rstrip() + "\n"
