"""Parser for the LLM-emitted Attendee Context appendix (#63)."""
from __future__ import annotations

from meeting_notetaker.utils.attendee_context import (
    find_appendix_span,
    parse_attendee_context,
    strip_appendix,
)


def test_parse_extracts_entries():
    md = """# TL;DR
body

## Attendee Context (auto-extracted)

```json
[
  {"name": "Bob", "observation": "Listed but did not actively participate."},
  {"name": "Dana", "observation": "Referenced but not in the attendees list."}
]
```
"""
    entries = parse_attendee_context(md)
    assert len(entries) == 2
    assert entries[0].name == "Bob"
    assert "actively participate" in entries[0].observation
    assert entries[1].name == "Dana"


def test_parse_missing_section_returns_empty():
    assert parse_attendee_context("# TL;DR\nbody") == []


def test_parse_malformed_json_returns_empty():
    md = """## Attendee Context (auto-extracted)

```json
[{not valid}]
```
"""
    assert parse_attendee_context(md) == []


def test_parse_skips_entries_without_name():
    md = """## Attendee Context (auto-extracted)

```json
[
  {"name": "Bob", "observation": "ok"},
  {"observation": "no name"},
  {"name": "", "observation": "empty"}
]
```
"""
    entries = parse_attendee_context(md)
    assert [e.name for e in entries] == ["Bob"]


def test_parse_handles_following_section():
    """Attendee Context may not be the last appendix; the span
    stops at the next ## heading instead of running to EOF."""
    md = """## Attendee Context (auto-extracted)

```json
[{"name": "Bob"}]
```

## Attendee Details (auto-extracted)

```json
[{"name": "Bob", "title": "CEO"}]
```
"""
    entries = parse_attendee_context(md)
    assert [e.name for e in entries] == ["Bob"]
    # The title field from the OTHER appendix doesn't leak in.
    assert entries[0].observation == ""


def test_parse_case_insensitive_heading():
    md = """## ATTENDEE CONTEXT (AUTO-EXTRACTED)

```json
[{"name": "Bob"}]
```
"""
    assert parse_attendee_context(md)[0].name == "Bob"


def test_strip_appendix_removes_section():
    md = """# TL;DR
body

## Attendee Context (auto-extracted)

```json
[{"name": "Bob"}]
```
"""
    out = strip_appendix(md)
    assert "Attendee Context" not in out
    assert "body" in out


def test_find_span_returns_offsets():
    md = "# H1\n\n## Attendee Context (auto-extracted)\n\nstuff"
    span = find_appendix_span(md)
    assert span is not None
    start, end = span
    assert md[start:end].startswith("## Attendee Context")


# ---- heading tolerance: optional (auto-extracted) (#102 bug 9) ---------


def test_parses_heading_without_auto_extracted_suffix():
    """Claude routinely drops the "(auto-extracted)" suffix from the
    heading even though the prompt asks for it. Without this
    tolerance the JSON silently vanishes into the appendix tray."""
    from meeting_notetaker.utils.attendee_context import parse_attendee_context
    src = (
        "## Summary\n\nBody.\n\n"
        "## Attendee Context\n\n"
        "```json\n[{\"name\":\"Alice\",\"observation\":\"Led the discussion.\"}]\n```\n"
    )
    out = parse_attendee_context(src)
    assert len(out) == 1
    assert out[0].name == "Alice"
    assert "Led the discussion" in out[0].observation


def test_parses_heading_with_auto_extracted_suffix_still():
    """The original spec'd form must still parse so existing notes
    files don't regress."""
    from meeting_notetaker.utils.attendee_context import parse_attendee_context
    src = (
        "## Attendee Context (auto-extracted)\n\n"
        "```json\n[{\"name\":\"Bob\",\"observation\":\"Quiet.\"}]\n```\n"
    )
    out = parse_attendee_context(src)
    assert len(out) == 1
    assert out[0].name == "Bob"
