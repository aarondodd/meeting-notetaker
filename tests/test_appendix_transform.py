"""Render the unified Appendix section from session data (#64)."""
from __future__ import annotations

from meeting_notetaker.utils.appendix_transform import (
    AppendixData,
    build_appendix_markdown,
    collect_from_markdown,
    inject_appendix,
)
from meeting_notetaker.utils.attendee_appendix import AttendeeAppendixEntry
from meeting_notetaker.utils.attendee_context import AttendeeContextEntry
from meeting_notetaker.utils.invite_mentions import InviteMentionEntry
from meeting_notetaker.utils.link_extractor import ExtractedLink


_DATA_FULL = AppendixData(
    attendee_context=[
        AttendeeContextEntry(name="Bob", observation="Listed but passive."),
    ],
    attendee_details=[
        AttendeeAppendixEntry(name="Bob", title="CEO", company="Bobco"),
    ],
    topics=["Q3 hiring", "Backend migration"],
    referenced_attachments=[
        InviteMentionEntry(name="budget deck", context="Q3 spend review"),
    ],
    session_attachments=["meeting-notes.pptx"],
    links=[
        ExtractedLink(url="https://wiki/auth", label="auth", source="notes"),
    ],
)


def test_build_appendix_markdown_renders_each_section():
    out = build_appendix_markdown(_DATA_FULL)
    assert "## Appendix (auto-extracted)" in out
    assert "### Attendee Context" in out
    assert "### Attendee Details" in out
    assert "### Suggested Topics" in out
    assert "### Referenced Attachments" in out
    assert "### Session Attachments" in out
    assert "### Links" in out
    assert "Bob" in out
    assert "Q3 hiring" in out
    assert "meeting-notes.pptx" in out
    assert "[auth](https://wiki/auth)" in out


def test_build_appendix_markdown_omits_empty_sections():
    data = AppendixData(
        attendee_context=[], attendee_details=[],
        topics=["Just one topic"], referenced_attachments=[],
        session_attachments=[], links=[],
    )
    out = build_appendix_markdown(data)
    assert "### Suggested Topics" in out
    assert "### Attendee Context" not in out
    assert "### Attendee Details" not in out
    assert "### Links" not in out


def test_build_appendix_markdown_empty_when_no_data():
    """When nothing parsed, the renderer returns an empty string so
    callers can skip the appendix entirely."""
    data = AppendixData(
        attendee_context=[], attendee_details=[], topics=[],
        referenced_attachments=[], session_attachments=[], links=[],
    )
    assert build_appendix_markdown(data) == ""


def test_md_escape_handles_pipes_in_cells():
    """A pipe in an observation must not break the table layout."""
    data = AppendixData(
        attendee_context=[
            AttendeeContextEntry(
                name="Bob",
                observation="Said: yes | no | maybe",
            ),
        ],
        attendee_details=[], topics=[], referenced_attachments=[],
        session_attachments=[], links=[],
    )
    out = build_appendix_markdown(data)
    # Backslash-escaped pipes survive.
    assert "Said: yes \\| no \\| maybe" in out


def test_inject_appendix_strips_raw_blocks_and_appends_rendered():
    source = """# TL;DR
synthesis prose

## Suggested Topics (auto-extracted)

```json
["Topic A"]
```

## Attendee Details (auto-extracted)

```json
[{"name": "Bob", "title": "CEO"}]
```
"""
    data = collect_from_markdown(
        notes_text=source,
        live_notes_text="",
        session_attachments=[],
    )
    out = inject_appendix(source, data)
    # Raw appendix sections removed.
    assert "Suggested Topics (auto-extracted)" not in out
    assert "Attendee Details (auto-extracted)" not in out
    # Synthesis body survives.
    assert "synthesis prose" in out
    # Rendered appendix appears.
    assert "## Appendix (auto-extracted)" in out
    assert "### Attendee Details" in out
    assert "### Suggested Topics" in out


def test_inject_appendix_no_data_just_strips():
    """Even when the rendered appendix is empty (e.g. user wiped
    all the JSON blocks), the strip step still runs."""
    source = """# TL;DR
body

## Suggested Topics (auto-extracted)

```json
[]
```
"""
    data = collect_from_markdown(notes_text=source)
    out = inject_appendix(source, data)
    assert "Suggested Topics (auto-extracted)" not in out
    assert "## Appendix" not in out
    assert "body" in out


def test_inject_appendix_preserves_user_appendix_heading():
    """A user's own '## Appendix' heading isn't touched; the
    rendered section uses '## Appendix (auto-extracted)' so the
    two coexist."""
    source = """# TL;DR
body

## Appendix

User-written appendix content here.

## Suggested Topics (auto-extracted)

```json
["Topic A"]
```
"""
    data = collect_from_markdown(notes_text=source)
    out = inject_appendix(source, data)
    assert "## Appendix\n" in out  # user heading preserved
    assert "User-written appendix content here." in out
    assert "## Appendix (auto-extracted)" in out


def test_collect_from_markdown_picks_up_links_from_live_notes():
    out = collect_from_markdown(
        notes_text="## Appendix\nrefer to [doc](https://a.example)",
        live_notes_text="raw url: https://b.example",
    )
    urls = {link.url for link in out.links}
    assert "https://a.example" in urls
    assert "https://b.example" in urls
