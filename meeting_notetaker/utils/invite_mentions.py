"""Parse the LLM's "Referenced Attachments (auto-extracted)" appendix.

Sibling parser to attendee_appendix.py / topic_appendix.py /
attendee_context.py. The LLM extracts mentions of shared documents,
files, or invite attachments from the meeting transcript. The
Appendix tray surfaces these alongside the user's actual session
attachments so the user can quickly tell which referenced items
are missing.

Tolerant of malformed input: missing section, missing JSON, bad
JSON, wrong shape -> empty list, never raises.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional


log = logging.getLogger(__name__)


@dataclass
class InviteMentionEntry:
    """One row of the LLM-extracted referenced-attachments appendix.

    `name` is the LLM's best guess at the document name (not
    necessarily the exact filename). `context` is a one-sentence
    note on how it came up in conversation.
    """
    name: str
    context: str = ""


_APPENDIX_HEADING_RE = re.compile(
    r"(##\s+Referenced\s+Attachments\s*\(auto-extracted\).*?)(?=^##\s|\Z)",
    re.IGNORECASE | re.DOTALL | re.MULTILINE,
)
_JSON_CODE_BLOCK_RE = re.compile(
    r"```(?:json)?\s*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)


def find_appendix_span(markdown: str) -> Optional[tuple[int, int]]:
    """Return (start, end) of the Referenced Attachments section."""
    if not markdown:
        return None
    m = _APPENDIX_HEADING_RE.search(markdown)
    if m is None:
        return None
    return (m.start(), m.end())


def parse_invite_mentions(markdown: str) -> list[InviteMentionEntry]:
    """Extract the JSON array of referenced-attachment entries.

    Returns an empty list on any failure. Entries without a
    non-empty ``name`` field are skipped.
    """
    span = find_appendix_span(markdown)
    if span is None:
        return []
    section = markdown[span[0]:span[1]]
    code = _JSON_CODE_BLOCK_RE.search(section)
    if code is None:
        log.debug("invite mentions: no JSON code block in section")
        return []
    try:
        data = json.loads(code.group(1))
    except json.JSONDecodeError:
        log.warning(
            "invite mentions: JSON failed to parse; payload=%r",
            code.group(1)[:200],
        )
        return []
    if not isinstance(data, list):
        log.warning(
            "invite mentions: top-level JSON was %s, expected list",
            type(data).__name__,
        )
        return []
    out: list[InviteMentionEntry] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", "") or "").strip()
        if not name:
            continue
        out.append(InviteMentionEntry(
            name=name,
            context=str(raw.get("context", "") or "").strip(),
        ))
    return out


def strip_appendix(markdown: str) -> str:
    """Return ``markdown`` with the Referenced Attachments section removed."""
    span = find_appendix_span(markdown)
    if span is None:
        return markdown
    return (markdown[:span[0]].rstrip() + "\n" + markdown[span[1]:]).rstrip() + "\n"
