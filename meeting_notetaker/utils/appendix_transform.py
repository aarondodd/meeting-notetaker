"""Render the unified Appendix section for preview / PDF / ZIP export.

Reads the raw JSON appendix blocks from a session's markdown buffer
(notes.md and / or live_notes.md), strips the raw blocks, and
appends a single ``## Appendix (auto-extracted)`` Markdown section
with sub-headings for each data type rendered as friendly Markdown
tables. Source buffer remains untouched -- this is a render-time
transform only.

Sub-sections (in order):
1. Attendee Context (#63)
2. Attendee Details (#51)
3. Suggested Topics (#57)
4. Referenced Attachments (#64, LLM-mentioned)
5. Session Attachments (#64, live mirror of AttachmentsStore)
6. Links (#64, scraped from both buffers)

Sections with zero entries are omitted so the appendix doesn't
balloon with empty headings.

Use ``inject_appendix`` to replace the raw blocks with the rendered
appendix. Use ``build_appendix_markdown`` to construct the appendix
on its own (useful for previewing what the export will look like).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import (
    attendee_appendix,
    attendee_context,
    invite_mentions,
    link_extractor,
    topic_appendix,
)


# H1 so the appendix sits as a top-level peer of the synthesis
# sections (# Attendees, # Decisions, # Notes, # Open Questions,
# etc.) that the bundled default.md prompt produces. Sub-sections
# below use H2 so they're clearly subordinated to the appendix and
# distinct from the H3 ### Topic headings used inside # Notes.
# Pre-v0.7.5 used ## here which rendered as a subsection of the
# preceding H1 (typically # Open Questions).
_APPENDIX_HEADING = "# Appendix (auto-extracted)"
_SUBSECTION_LEVEL = "## "


@dataclass
class AppendixData:
    """Pre-parsed payload that ``inject_appendix`` renders.

    Caller assembles this -- the helper functions here don't know
    about ``AttachmentsStore`` or session lookup. This keeps the
    transform a pure function over Python data.
    """
    attendee_context: list  # list[AttendeeContextEntry]
    attendee_details: list  # list[AttendeeAppendixEntry]
    topics: list[str]
    referenced_attachments: list  # list[InviteMentionEntry]
    session_attachments: list[str]  # display names
    links: list  # list[ExtractedLink]


def collect_from_markdown(
    *,
    notes_text: str = "",
    live_notes_text: str = "",
    session_attachments: Optional[list[str]] = None,
) -> AppendixData:
    """Parse all appendix sources out of the given buffers + the
    caller-supplied session attachment names.

    ``session_attachments`` is the display-name list from
    ``AttachmentsStore`` -- this module doesn't depend on the
    store directly so unit tests can pass an in-memory list.
    """
    return AppendixData(
        attendee_context=attendee_context.parse_attendee_context(notes_text),
        attendee_details=attendee_appendix.parse_appendix(notes_text),
        topics=topic_appendix.parse_topic_appendix(notes_text),
        referenced_attachments=invite_mentions.parse_invite_mentions(notes_text),
        session_attachments=list(session_attachments or []),
        links=link_extractor.extract_links(
            notes_text=notes_text or "",
            live_notes_text=live_notes_text or "",
        ),
    )


def build_appendix_markdown(data: AppendixData) -> str:
    """Return the rendered Markdown for the appendix, or "" when
    none of the sources contributed any entries.

    The rendering deliberately uses simple Markdown tables that
    Qt's setMarkdown renders cleanly + that survive the PDF print
    path without storage-XML gymnastics.
    """
    sections: list[str] = []
    if data.attendee_context:
        sections.append(_render_attendee_context(data.attendee_context))
    if data.attendee_details:
        sections.append(_render_attendee_details(data.attendee_details))
    if data.topics:
        sections.append(_render_topics(data.topics))
    if data.referenced_attachments:
        sections.append(_render_referenced_attachments(data.referenced_attachments))
    if data.session_attachments:
        sections.append(_render_session_attachments(data.session_attachments))
    if data.links:
        sections.append(_render_links(data.links))
    if not sections:
        return ""
    # Separator strategy: blank-line + horizontal rule + blank-line
    # between every sub-section. The HR forces Qt's GFM table parser
    # to see an unambiguous block boundary, which was needed on
    # Windows where the H2 sub-headings after each table were being
    # absorbed as table continuation (reported in #v0.7.5 testing).
    # As a bonus, the HR visually distinguishes the sub-sections so
    # they read as discrete items rather than one long appendix
    # blob.
    body = "\n\n---\n\n".join(sections)
    return f"{_APPENDIX_HEADING}\n\n{body}\n"


def inject_appendix(
    source: str,
    data: AppendixData,
) -> str:
    """Render the appendix transform over ``source`` markdown.

    1. Removes the raw JSON appendix sections (attendee details,
       attendee context, topics, referenced attachments).
    2. Appends the rendered ``## Appendix (auto-extracted)`` section
       built from ``data``.

    A user-written ``## Appendix`` section earlier in the document
    is preserved -- the rendered section uses a distinct
    ``## Appendix (auto-extracted)`` heading so the two don't
    collide.

    When ``data`` produces no rendered output (every source empty),
    the raw blocks are still stripped + nothing is appended.
    """
    stripped = source or ""
    stripped = attendee_context.strip_appendix(stripped)
    stripped = attendee_appendix.strip_appendix(stripped)
    stripped = topic_appendix.strip_appendix(stripped)
    stripped = invite_mentions.strip_appendix(stripped)
    rendered = build_appendix_markdown(data)
    if not rendered:
        return stripped
    if not stripped.endswith("\n"):
        stripped = stripped + "\n"
    return stripped.rstrip() + "\n\n" + rendered


# ---------------------------------------------------------------------
# Section renderers


def _md_escape(s: str) -> str:
    """Escape Markdown table-cell-hostile characters.

    Pipe + backslash + newline are the table-killers; everything else
    Qt renders fine. Newlines flatten to spaces so a multi-line
    observation doesn't break the row.
    """
    if not s:
        return ""
    return (
        s.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r\n", " ")
        .replace("\n", " ")
        .strip()
    )


def _render_attendee_context(entries) -> str:
    rows = [
        f"{_SUBSECTION_LEVEL}Attendee Context",
        "",
        "| Name | Observation |",
        "|------|-------------|",
    ]
    for e in entries:
        rows.append(f"| {_md_escape(e.name)} | {_md_escape(e.observation)} |")
    return "\n".join(rows)


def _render_attendee_details(entries) -> str:
    rows = [
        f"{_SUBSECTION_LEVEL}Attendee Details",
        "",
        "| Name | Title | Company | Email | Phone |",
        "|------|-------|---------|-------|-------|",
    ]
    for e in entries:
        rows.append(
            "| "
            + " | ".join([
                _md_escape(e.name),
                _md_escape(e.title),
                _md_escape(e.company),
                _md_escape(e.email),
                _md_escape(e.phone),
            ])
            + " |"
        )
    return "\n".join(rows)


def _render_topics(topics) -> str:
    lines = [f"{_SUBSECTION_LEVEL}Suggested Topics", ""]
    for t in topics:
        lines.append(f"- {_md_escape(t)}")
    return "\n".join(lines)


def _render_referenced_attachments(entries) -> str:
    rows = [
        f"{_SUBSECTION_LEVEL}Referenced Attachments",
        "",
        "| Name | Context |",
        "|------|---------|",
    ]
    for e in entries:
        rows.append(f"| {_md_escape(e.name)} | {_md_escape(e.context)} |")
    return "\n".join(rows)


def _render_session_attachments(names) -> str:
    lines = [f"{_SUBSECTION_LEVEL}Session Attachments", ""]
    for n in names:
        lines.append(f"- {_md_escape(n)}")
    return "\n".join(lines)


def _render_links(links) -> str:
    lines = [f"{_SUBSECTION_LEVEL}Links", ""]
    for link in links:
        if link.label and link.label != link.url:
            lines.append(f"- [{_md_escape(link.label)}]({link.url})")
        else:
            lines.append(f"- <{link.url}>")
    return "\n".join(lines)
