"""Parse the LLM's "Attendee Details (auto-extracted)" appendix.

Issue #51 Phase 4. The synthesis prompt includes a system-prompt
fragment that instructs the LLM to emit a final section titled
``## Attendee Details (auto-extracted)`` containing a JSON code
block with per-attendee details (title / company / department /
email / phone). MainApp's _handle_synthesis_result calls into
this module after paste-back, parses the appendix if present, and
applies the extracted data to the session's Contacts via
``update_contact_fields(fill_empty_only=True, source=llm)`` so
existing values are never overwritten.

The strip-appendix path (off by default; user can enable in
Settings) removes the appendix from the saved notes.md so the
synthesis stays focused. Default behavior keeps the appendix
visible for transparency -- the user can see what was extracted.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional


log = logging.getLogger(__name__)


@dataclass
class AttendeeAppendixEntry:
    """One row of the LLM-extracted attendee table.

    The LLM is instructed to include any subset of fields it
    identified. Unknown fields come back as empty strings; the
    enrichment caller filters them out.
    """
    name: str
    title: str = ""
    company: str = ""
    department: str = ""
    email: str = ""
    phone: str = ""


# Anchored on the exact heading the system prompt asks the LLM to
# use; case-insensitive for safety in case the model normalizes
# capitalization. The capture group is bounded by the next H2 (or
# end-of-string) so the strip path doesn't accidentally clobber a
# later section -- user-authored notes or the LLM emitting another
# appendix below this one. Originally greedy-to-EOF on the
# assumption "Attendee Details is always last" (issue #51), which
# the topic_suggestions prompt later contradicted by asking the
# LLM to put Topics after Attendee Details (#73 finding #1).
_APPENDIX_HEADING_RE = re.compile(
    r"(##\s+Attendee\s+Details\s*\(auto-extracted\).*?)(?=^##\s|\Z)",
    re.IGNORECASE | re.DOTALL | re.MULTILINE,
)
_JSON_CODE_BLOCK_RE = re.compile(
    r"```(?:json)?\s*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)


def find_appendix_span(markdown: str) -> Optional[tuple[int, int]]:
    """Return (start, end) byte offsets of the appendix section in
    ``markdown``, or None if no appendix is present.

    Span runs from the heading through to the next H2 heading
    (matching the other three appendix parsers' shape) or
    end-of-string -- whichever comes first. Used by the strip path
    to slice the section out of the saved notes when the user
    opts in.
    """
    if not markdown:
        return None
    m = _APPENDIX_HEADING_RE.search(markdown)
    if m is None:
        return None
    return (m.start(), m.end())


def parse_appendix(markdown: str) -> list[AttendeeAppendixEntry]:
    """Extract the JSON code block from an Attendee Details appendix.

    Returns an empty list when the appendix is missing, the JSON
    code block is missing, or the JSON is unparseable. We do NOT
    raise on malformed input -- the LLM might have wandered, and
    the synthesis itself is still useful even when the appendix
    falls back to no-extraction.
    """
    span = find_appendix_span(markdown)
    if span is None:
        return []
    section = markdown[span[0]:span[1]]
    code = _JSON_CODE_BLOCK_RE.search(section)
    if code is None:
        log.debug("attendee appendix: no JSON code block in section")
        return []
    try:
        data = json.loads(code.group(1))
    except json.JSONDecodeError:
        log.warning(
            "attendee appendix: JSON failed to parse; payload=%r",
            code.group(1)[:200],
        )
        return []
    if not isinstance(data, list):
        log.warning(
            "attendee appendix: top-level JSON was %s, expected list",
            type(data).__name__,
        )
        return []
    out: list[AttendeeAppendixEntry] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", "") or "").strip()
        if not name:
            continue
        out.append(AttendeeAppendixEntry(
            name=name,
            title=str(raw.get("title", "") or "").strip(),
            company=str(raw.get("company", "") or "").strip(),
            department=str(raw.get("department", "") or "").strip(),
            email=str(raw.get("email", "") or "").strip(),
            phone=str(raw.get("phone", "") or "").strip(),
        ))
    return out


def strip_appendix(markdown: str) -> str:
    """Return ``markdown`` with the Attendee Details appendix removed.

    Used by the strip-on-save path when the user enables that
    setting. The default keeps the appendix visible. Content after
    the appendix's bounded span (user-authored or other appendix
    sections) is preserved -- the post-#73 regex is no longer
    greedy-to-EOF.
    """
    span = find_appendix_span(markdown)
    if span is None:
        return markdown
    return (markdown[:span[0]].rstrip() + "\n" + markdown[span[1]:]).rstrip() + "\n"
