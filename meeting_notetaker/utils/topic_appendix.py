"""Parse the LLM's "Suggested Topics (auto-extracted)" appendix (#57).

The synthesis prompt includes an app-injected system-prompt fragment
that instructs the LLM to emit a final section titled
``## Suggested Topics (auto-extracted)`` containing a JSON array of
short topic strings. MainApp parses that appendix after paste-back
and merges the LLM-suggested topics with the deterministic
extractor's output before pushing them into the session's topic
suggestion list.

Tolerant of malformed input: missing appendix, missing JSON block,
unparseable JSON, or wrong top-level shape all return an empty list
without raising. Mirrors the attendee_appendix parser's failure
posture.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional


log = logging.getLogger(__name__)


_APPENDIX_HEADING_RE = re.compile(
    # See attendee_context: optional "(auto-extracted)" suffix so
    # the LLM dropping it doesn't silently lose the JSON.
    r"(##\s+Suggested\s+Topics(?:\s*\(auto-extracted\))?\b.*?)(?=^##\s|\Z)",
    re.IGNORECASE | re.DOTALL | re.MULTILINE,
)
_JSON_CODE_BLOCK_RE = re.compile(
    r"```(?:json)?\s*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)


def find_appendix_span(markdown: str) -> Optional[tuple[int, int]]:
    """Return (start, end) of the Suggested Topics section, or None.

    Unlike the Attendee Details appendix (which the prompt asks the
    LLM to put last), the Topics section may not be last -- the
    Attendee Details section may follow it. The regex therefore
    captures up to the next heading at the same level OR end of
    string, whichever comes first.
    """
    if not markdown:
        return None
    m = _APPENDIX_HEADING_RE.search(markdown)
    if m is None:
        return None
    return (m.start(), m.end())


def parse_topic_appendix(markdown: str) -> list[str]:
    """Extract the JSON array of topic strings.

    Returns an empty list when the appendix is missing, the JSON
    block is missing, the JSON is malformed, or the top-level shape
    isn't a list. Entries that aren't strings (or are empty after
    strip()) are skipped.

    Dedupes case-insensitively, preserving the first occurrence's
    casing -- "Q3 Hiring" and "q3 hiring" collapse to one entry.
    """
    span = find_appendix_span(markdown)
    if span is None:
        return []
    section = markdown[span[0]:span[1]]
    code = _JSON_CODE_BLOCK_RE.search(section)
    if code is None:
        log.debug("topic appendix: no JSON code block in section")
        return []
    try:
        data = json.loads(code.group(1))
    except json.JSONDecodeError:
        log.warning(
            "topic appendix: JSON failed to parse; payload=%r",
            code.group(1)[:200],
        )
        return []
    if not isinstance(data, list):
        log.warning(
            "topic appendix: top-level JSON was %s, expected list",
            type(data).__name__,
        )
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in data:
        if not isinstance(raw, str):
            continue
        name = raw.strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def strip_appendix(markdown: str) -> str:
    """Return ``markdown`` with the Suggested Topics appendix removed.

    Used by the strip-on-save path alongside the Attendee Details
    stripper so the saved synthesis stays focused.
    """
    span = find_appendix_span(markdown)
    if span is None:
        return markdown
    return (markdown[:span[0]].rstrip() + "\n" + markdown[span[1]:]).rstrip() + "\n"
