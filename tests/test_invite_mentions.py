"""Parser for the LLM-emitted Referenced Attachments appendix (#64)."""
from __future__ import annotations

from meeting_notetaker.utils.invite_mentions import (
    find_appendix_span,
    parse_invite_mentions,
    strip_appendix,
)


def test_parse_extracts_entries():
    md = """# TL;DR
body

## Referenced Attachments (auto-extracted)

```json
[
  {"name": "budget rollup", "context": "discussed during Q3 review"},
  {"name": "architecture deck", "context": "Bob mentioned slide 4"}
]
```
"""
    entries = parse_invite_mentions(md)
    assert len(entries) == 2
    assert entries[0].name == "budget rollup"
    assert "Q3 review" in entries[0].context


def test_parse_missing_section_returns_empty():
    assert parse_invite_mentions("# TL;DR\nbody") == []


def test_parse_malformed_json_returns_empty():
    md = """## Referenced Attachments (auto-extracted)

```json
{not valid}
```
"""
    assert parse_invite_mentions(md) == []


def test_parse_non_array_returns_empty():
    md = """## Referenced Attachments (auto-extracted)

```json
{"name": "single object"}
```
"""
    assert parse_invite_mentions(md) == []


def test_parse_skips_entries_without_name():
    md = """## Referenced Attachments (auto-extracted)

```json
[
  {"name": "Real attachment"},
  {"context": "no name"},
  {"name": ""}
]
```
"""
    entries = parse_invite_mentions(md)
    assert [e.name for e in entries] == ["Real attachment"]


def test_strip_appendix_removes_section():
    md = """# TL;DR
body

## Referenced Attachments (auto-extracted)

```json
[{"name": "x"}]
```
"""
    out = strip_appendix(md)
    assert "Referenced Attachments" not in out
    assert "body" in out


def test_parse_case_insensitive_heading():
    md = """## REFERENCED ATTACHMENTS (AUTO-EXTRACTED)

```json
[{"name": "x"}]
```
"""
    assert parse_invite_mentions(md)[0].name == "x"


def test_find_span_returns_offsets():
    md = "# H1\n\n## Referenced Attachments (auto-extracted)\n\nstuff"
    span = find_appendix_span(md)
    assert span is not None
    start, end = span
    assert md[start:end].startswith("## Referenced Attachments")
