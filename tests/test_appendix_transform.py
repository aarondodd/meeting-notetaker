"""Render the unified Appendix section from session data (#64)."""
from __future__ import annotations

from meeting_notetaker.utils.appendix_transform import (
    AppendixData,
    build_appendix_markdown,
    collect_from_markdown,
    inject_appendix,
    strip_all_appendices,
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
    # v0.7.5: appendix heading promoted to H1, sub-sections to H2,
    # so the appendix sits as a top-level peer of # Attendees /
    # # Decisions / # Notes / etc. from the bundled prompt rather
    # than nesting under # Open Questions.
    assert "# Appendix (auto-extracted)" in out
    assert "## Attendee Context" in out
    assert "## Attendee Details" in out
    assert "## Suggested Topics" in out
    assert "## Referenced Attachments" in out
    assert "## Session Attachments" in out
    assert "## Links" in out
    assert "Bob" in out
    assert "Q3 hiring" in out
    assert "meeting-notes.pptx" in out
    assert "[auth](https://wiki/auth)" in out


def test_appendix_heading_is_h1_not_h2():
    """Pin the heading level explicitly so a future regression to
    '## Appendix' (which renders as a sub-section of # Open
    Questions under the bundled prompt) breaks loudly."""
    out = build_appendix_markdown(_DATA_FULL)
    # Each heading line that contains "Appendix (auto-extracted)"
    # must start with exactly one '#' followed by a space.
    appendix_heading_lines = [
        line for line in out.splitlines()
        if "Appendix (auto-extracted)" in line
    ]
    assert len(appendix_heading_lines) == 1, appendix_heading_lines
    line = appendix_heading_lines[0]
    assert line.startswith("# "), f"Expected H1 heading, got {line!r}"
    assert not line.startswith("## "), f"Heading regressed to H2: {line!r}"


def test_subsection_headings_are_h2_not_h3():
    """Sub-sections must be H2 so they're clearly subordinated to
    the H1 appendix heading + visually distinct from the H3
    ### Topic sub-headings the prompt uses inside # Notes."""
    out = build_appendix_markdown(_DATA_FULL)
    expected = {
        "Attendee Context",
        "Attendee Details",
        "Suggested Topics",
        "Referenced Attachments",
        "Session Attachments",
        "Links",
    }
    for line in out.splitlines():
        for name in expected:
            if line.endswith(name) and line.lstrip("#").strip() == name:
                # This is a heading line for one of the sub-sections.
                assert line.startswith("## "), (
                    f"Sub-section heading should be H2, got {line!r}"
                )
                assert not line.startswith("### "), (
                    f"Sub-section heading regressed to H3: {line!r}"
                )


def test_build_appendix_markdown_omits_empty_sections():
    data = AppendixData(
        attendee_context=[], attendee_details=[],
        topics=["Just one topic"], referenced_attachments=[],
        session_attachments=[], links=[],
    )
    out = build_appendix_markdown(data)
    assert "## Suggested Topics" in out
    assert "## Attendee Context" not in out
    assert "## Attendee Details" not in out
    assert "## Links" not in out


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
    # Rendered appendix appears (v0.7.5 heading levels: H1 + H2).
    assert "# Appendix (auto-extracted)" in out
    assert "## Attendee Details" in out
    assert "## Suggested Topics" in out


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
    rendered section uses '# Appendix (auto-extracted)' (a
    different heading level + the explicit suffix) so the two
    coexist."""
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
    assert "# Appendix (auto-extracted)" in out


def test_collect_from_markdown_picks_up_links_from_live_notes():
    out = collect_from_markdown(
        notes_text="## Appendix\nrefer to [doc](https://a.example)",
        live_notes_text="raw url: https://b.example",
    )
    urls = {link.url for link in out.links}
    assert "https://a.example" in urls
    assert "https://b.example" in urls


# ---- #93 strip_all_appendices + inject_appendix user-Appendix merge ----

def test_strip_all_appendices_removes_raw_h2_sections():
    """The strip helper drops every raw "(auto-extracted)" H2 block.

    Idempotent on a clean body."""
    raw = """# Notes

User content stays.

## Attendee Details (auto-extracted)
```json
[]
```

## Suggested Topics (auto-extracted)
```json
[]
```
"""
    out = strip_all_appendices(raw)
    assert "Attendee Details (auto-extracted)" not in out
    assert "Suggested Topics (auto-extracted)" not in out
    assert "User content stays." in out
    # Idempotent on already-clean input.
    assert strip_all_appendices(out) == out


def test_strip_all_appendices_no_op_when_clean():
    """A body with no raw appendix blocks passes through unchanged."""
    clean = "# Notes\n\nNothing to strip here.\n"
    assert strip_all_appendices(clean) == clean


def test_inject_appendix_creates_h1_when_no_user_appendix():
    """Baseline #93 behavior: no user `# Appendix` -> auto-extracted
    section appended as a fresh H1 at the end."""
    src = "# Notes\n\nbody text\n"
    out = inject_appendix(src, _DATA_FULL)
    assert "# Appendix (auto-extracted)" in out
    # Auto-extracted H1 sits after the user content.
    assert out.index("# Notes") < out.index("# Appendix (auto-extracted)")


def test_inject_appendix_merges_under_user_appendix_at_eof():
    """When the user wrote `# Appendix` at the end, the formatted
    H2 sub-sections are merged into its body. No separate
    "# Appendix (auto-extracted)" heading appears.

    Note: the rendered H2 sub-sections drop the "(auto-extracted)"
    suffix from their headings (the suffix is only on the raw H2
    blocks the LLM emits; the formatted versions read cleaner).
    The H1 wrapper carries the suffix when it's auto-injected; the
    user-written "# Appendix" stays as-is.
    """
    src = "# Notes\n\nbody\n\n# Appendix\n\nUser's own appendix content.\n"
    out = inject_appendix(src, _DATA_FULL)
    # User's heading preserved.
    assert "# Appendix\n" in out
    assert "User's own appendix content." in out
    # H2 sub-sections appear after the user's body.
    assert "## Attendee Details" in out
    # No separate auto-extracted H1 heading.
    assert "# Appendix (auto-extracted)" not in out


def test_inject_appendix_merges_under_user_appendix_mid_document():
    """User's `# Appendix` sits between other H1 sections -- the
    auto-extracted sub-sections merge before the next H1 boundary."""
    src = (
        "# Notes\n\nbody\n\n"
        "# Appendix\n\nUser's own appendix content.\n\n"
        "# Open Questions\n\nfollow-up items\n"
    )
    out = inject_appendix(src, _DATA_FULL)
    # All three user H1 headings preserved.
    assert "# Notes" in out
    assert "# Appendix\n" in out
    assert "# Open Questions" in out
    # Sub-sections appended INSIDE the user's Appendix section,
    # before the Open Questions H1 boundary.
    appendix_idx = out.index("# Appendix\n")
    attendee_idx = out.index("## Attendee Details")
    open_q_idx = out.index("# Open Questions")
    assert appendix_idx < attendee_idx < open_q_idx
    # No separate auto-extracted H1 heading.
    assert "# Appendix (auto-extracted)" not in out


def test_inject_appendix_case_insensitive_user_appendix_match():
    """User-written `# appendix` (lowercase) also triggers the merge."""
    src = "# Notes\n\n# appendix\n\nuser body\n"
    out = inject_appendix(src, _DATA_FULL)
    assert "# Appendix (auto-extracted)" not in out
    assert "## Attendee Details" in out


def test_inject_appendix_appendix_auto_extracted_not_user_marker():
    """A pre-existing `# Appendix (auto-extracted)` heading is NOT
    treated as a user marker; it gets stripped by the per-section
    strippers and the fresh auto-extracted H1 is appended at end."""
    src = (
        "# Notes\n\nbody\n\n"
        "# Appendix (auto-extracted)\n\n"
        "## Attendee Details (auto-extracted)\n```json\n[]\n```\n"
    )
    out = inject_appendix(src, _DATA_FULL)
    # The stale H1 may or may not survive depending on the
    # per-section strippers; the canonical heading appears as the
    # injected one at the end.
    assert "# Appendix (auto-extracted)" in out
    # And new content from _DATA_FULL is present.
    assert "Bob" in out


def test_inject_appendix_no_op_when_no_data_and_no_user_appendix():
    """Empty data + no user appendix = stripped body, nothing appended."""
    empty = AppendixData(
        attendee_context=[], attendee_details=[], topics=[],
        referenced_attachments=[], session_attachments=[], links=[],
    )
    src = "# Notes\n\nbody only\n"
    out = inject_appendix(src, empty)
    assert "# Appendix" not in out
    assert "body only" in out


def test_strip_all_appendices_idempotent_supports_cleanup_on_open():
    """The cleanup-on-open hook calls strip_all_appendices and
    rewrites only when the result differs. Pin idempotency so the
    second open doesn't rewrite an already-clean file."""
    legacy = """# Notes

User content stays.

## Attendee Details (auto-extracted)
```json
[]
```
"""
    once = strip_all_appendices(legacy)
    twice = strip_all_appendices(once)
    assert once == twice
    assert "Attendee Details (auto-extracted)" not in once


def test_inject_appendix_no_op_with_data_but_no_subsections_with_user_appendix():
    """Edge case: user wrote `# Appendix` but data is empty. The
    user's section stays untouched; nothing is injected."""
    empty = AppendixData(
        attendee_context=[], attendee_details=[], topics=[],
        referenced_attachments=[], session_attachments=[], links=[],
    )
    src = "# Notes\n\nbody\n\n# Appendix\n\nUser content.\n"
    out = inject_appendix(src, empty)
    assert "# Appendix\n" in out
    assert "User content." in out
    assert "(auto-extracted)" not in out
