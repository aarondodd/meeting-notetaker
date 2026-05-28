"""Attendee Details appendix parsing + system-prompt injection (#51 Phase 4).

The synthesis prompt includes an app-injected system prompt that
instructs the LLM to emit a final "## Attendee Details
(auto-extracted)" section containing a JSON code block with
per-attendee rich-field data. MainApp parses that appendix on
paste-back + applies the data with fill_empty_only=True so
existing user values are never overwritten.

These tests pin the parser contract (robust to malformed JSON, no
appendix, partial fields) + the prompt-injection mechanism (system
prompts append to render() output when include_system_prompts=True
and an empty resources/system_prompts/ directory is a no-op).
"""
from __future__ import annotations

import pytest

from meeting_notetaker.utils import prompts as prompts_mod
from meeting_notetaker.utils.attendee_appendix import (
    AttendeeAppendixEntry,
    find_appendix_span,
    parse_appendix,
    strip_appendix,
)


# ---- parse_appendix --------------------------------------------------------


def test_parse_appendix_extracts_full_entry():
    """The happy path: heading + json code block + complete entry."""
    md = """
# TL;DR
Some synthesis content.

## Attendee Details (auto-extracted)

```json
[
  {"name": "Bob Jones", "title": "CEO", "company": "Bobco", "email": "bob@bobco.com"}
]
```
""".strip()
    entries = parse_appendix(md)
    assert len(entries) == 1
    e = entries[0]
    assert e.name == "Bob Jones"
    assert e.title == "CEO"
    assert e.company == "Bobco"
    assert e.email == "bob@bobco.com"
    assert e.department == ""
    assert e.phone == ""


def test_parse_appendix_with_multiple_entries():
    md = """## Attendee Details (auto-extracted)

```json
[
  {"name": "Bob", "title": "CEO"},
  {"name": "Mary", "email": "msue@sueco.com"},
  {"name": "Charlie", "department": "Platform"}
]
```
"""
    entries = parse_appendix(md)
    assert [e.name for e in entries] == ["Bob", "Mary", "Charlie"]
    assert entries[0].title == "CEO"
    assert entries[1].email == "msue@sueco.com"
    assert entries[2].department == "Platform"


def test_parse_appendix_missing_returns_empty():
    """Synthesis without an appendix -> empty list, no exception."""
    md = "# TL;DR\nNothing about attendees here."
    assert parse_appendix(md) == []


def test_parse_appendix_missing_json_block_returns_empty():
    """Appendix heading but no JSON code block."""
    md = """## Attendee Details (auto-extracted)

The LLM forgot to include the JSON.
"""
    assert parse_appendix(md) == []


def test_parse_appendix_malformed_json_returns_empty():
    """Malformed JSON -> empty list + log warning (not exception)."""
    md = """## Attendee Details (auto-extracted)

```json
[{this isn't valid json}]
```
"""
    assert parse_appendix(md) == []


def test_parse_appendix_non_array_json_returns_empty():
    """Top-level object instead of array -> empty list."""
    md = """## Attendee Details (auto-extracted)

```json
{"name": "Bob"}
```
"""
    assert parse_appendix(md) == []


def test_parse_appendix_skips_entries_missing_name():
    """An object without `name` is meaningless -- skip it."""
    md = """## Attendee Details (auto-extracted)

```json
[
  {"name": "Bob", "title": "CEO"},
  {"title": "VP without a name"},
  {"name": "", "company": "Anonymous Inc"}
]
```
"""
    entries = parse_appendix(md)
    assert [e.name for e in entries] == ["Bob"]


def test_parse_appendix_case_insensitive_heading():
    """The LLM might capitalize differently; match case-insensitively."""
    md = """## ATTENDEE DETAILS (AUTO-EXTRACTED)

```json
[{"name": "Bob", "title": "CEO"}]
```
"""
    entries = parse_appendix(md)
    assert len(entries) == 1
    assert entries[0].name == "Bob"


def test_parse_appendix_json_without_language_tag():
    """Some LLMs omit the 'json' language tag on code blocks."""
    md = """## Attendee Details (auto-extracted)

```
[{"name": "Bob", "title": "CEO"}]
```
"""
    entries = parse_appendix(md)
    assert len(entries) == 1


# ---- strip_appendix --------------------------------------------------------


def test_strip_appendix_removes_section_cleanly():
    md = """# TL;DR
Synthesis content.

## Attendee Details (auto-extracted)

```json
[{"name": "Bob"}]
```
"""
    stripped = strip_appendix(md)
    assert "Attendee Details" not in stripped
    assert "Synthesis content." in stripped
    # Stripped result ends with one trailing newline, no dangling blank lines.
    assert stripped.endswith("\n")
    assert not stripped.endswith("\n\n\n")


def test_strip_appendix_noop_when_missing():
    md = "# TL;DR\nNothing about attendees."
    assert strip_appendix(md) == md


# ---- find_appendix_span ----------------------------------------------------


def test_find_appendix_span_returns_offsets():
    md = "# TL;DR\nbody.\n\n## Attendee Details (auto-extracted)\n\nstuff"
    span = find_appendix_span(md)
    assert span is not None
    start, end = span
    assert md[start:end].startswith("## Attendee Details")
    assert end == len(md)


def test_find_appendix_span_none_when_missing():
    assert find_appendix_span("# TL;DR\nbody.") is None


# ---- prompts.render() + system-prompt injection ----------------------------


def test_render_appends_system_prompts_by_default():
    """The default render() output contains the system-prompt sentinel
    + the attendee-details appendix instruction."""
    out = prompts_mod.render(
        "user template", session_title="x", session_date="2026-05-15",
        transcript="",
    )
    # Sentinel separates user template from system prompts.
    assert "<!-- mn:system-prompts -->" in out
    # The bundled attendee-details system prompt mentions the heading.
    assert "Attendee Details (auto-extracted)" in out


def test_render_omits_system_prompts_when_opted_out():
    """include_system_prompts=False -> output is just the substituted
    user template, identical to pre-#51 behavior."""
    out = prompts_mod.render(
        "user template", session_title="x", session_date="2026-05-15",
        transcript="", include_system_prompts=False,
    )
    assert out == "user template"
    assert "mn:system-prompts" not in out
