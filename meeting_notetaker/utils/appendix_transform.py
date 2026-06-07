"""Render the unified Appendix section for preview / PDF / ZIP export.

Reads the raw JSON appendix blocks from a session's markdown buffer
(notes.md and / or live_notes.md), strips the raw blocks, and
appends a single ``# Appendix (auto-extracted)`` Markdown section
(H1 -- top-level peer of the bundled prompt's H1 sections) with
``## X`` sub-headings for each data type rendered as friendly
Markdown tables. Source buffer remains untouched -- this is a
render-time transform only.

# Module overview

Two related operations:

  * ``strip_all_appendices(text)`` -- removes the raw H2
    "(auto-extracted)" JSON blocks the LLM emits. Used at paste-back
    time and at session open as the cleanup-on-open pass (#93).

  * ``inject_appendix(source, data)`` -- strips the raw blocks (same
    helpers as above) and appends the formatted appendix. When the
    source has a user-written ``# Appendix`` heading, the formatted
    sub-sections are merged into that section's body rather than a
    new ``# Appendix (auto-extracted)`` heading appearing as a peer.

The H2 "(auto-extracted)" suffix on each sub-section heading is
preserved so the user always sees which content is machine-generated.

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
    sections = _build_appendix_subsections(data)
    if not sections:
        return ""
    body = "\n\n".join(sections)
    return f"{_APPENDIX_HEADING}\n\n{body}\n"


def strip_all_appendices(markdown: str) -> str:
    """Remove every raw LLM-appendix H2 section from `markdown`.

    Runs the four section-specific strippers in sequence:
      * attendee_context.strip_appendix
      * attendee_appendix.strip_appendix (attendee details)
      * topic_appendix.strip_appendix
      * invite_mentions.strip_appendix

    Free function so worker threads + the session-content loader can
    scrub notes.md without depending on MainApp. Idempotent: a body
    with no raw blocks passes through unchanged.
    """
    from .attendee_appendix import strip_appendix  # noqa: PLC0415
    from .attendee_context import (  # noqa: PLC0415
        strip_appendix as strip_attendee_context,
    )
    from .invite_mentions import (  # noqa: PLC0415
        strip_appendix as strip_invite_mentions,
    )
    from .topic_appendix import (  # noqa: PLC0415
        strip_appendix as strip_topic_appendix,
    )
    markdown = strip_appendix(markdown)
    markdown = strip_topic_appendix(markdown)
    markdown = strip_attendee_context(markdown)
    markdown = strip_invite_mentions(markdown)
    return markdown


def inject_appendix(
    source: str,
    data: AppendixData,
) -> str:
    """Render the appendix transform over ``source`` markdown.

    1. Removes the raw JSON appendix sections (attendee details,
       attendee context, topics, referenced attachments).
    2. Builds the formatted appendix sub-sections from ``data``.
    3. If the source contains a user-written ``# Appendix`` heading
       (without the ``(auto-extracted)`` suffix), the formatted
       sub-sections are merged into that section's body. The
       user's own appendix content is preserved above the merged
       auto-extracted sub-sections.
    4. Otherwise the formatted sub-sections are appended as a
       fresh ``# Appendix (auto-extracted)`` H1 at the end.

    When ``data`` produces no rendered output (every source empty),
    the raw blocks are still stripped + nothing is appended.
    """
    stripped = source or ""
    stripped = attendee_context.strip_appendix(stripped)
    stripped = attendee_appendix.strip_appendix(stripped)
    stripped = topic_appendix.strip_appendix(stripped)
    stripped = invite_mentions.strip_appendix(stripped)
    subsections = _build_appendix_subsections(data)
    if not subsections:
        return stripped
    user_section_end = _find_user_appendix_section_end(stripped)
    if user_section_end is not None:
        # Merge sub-sections into the user's # Appendix section
        # rather than create a separate H1. We splice at the end
        # of the user's section, preserving everything they wrote
        # above.
        before = stripped[:user_section_end].rstrip()
        after = stripped[user_section_end:]
        merged_body = "\n\n".join(subsections)
        return f"{before}\n\n{merged_body}\n{after}"
    # No user-written # Appendix -- append a fresh auto-extracted H1.
    body = "\n\n".join(subsections)
    rendered = f"{_APPENDIX_HEADING}\n\n{body}\n"
    if not stripped.endswith("\n"):
        stripped = stripped + "\n"
    return stripped.rstrip() + "\n\n" + rendered


def _build_appendix_subsections(data: AppendixData) -> list[str]:
    """Return the rendered H2 sub-section strings in canonical order.

    Empty data sources are omitted -- the appendix should never
    surface empty tables. Used by both build_appendix_markdown
    (which wraps with the H1 heading) and inject_appendix's
    user-Appendix merge path (which skips the H1 wrapper).
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
    return sections


# Regex matching `# Appendix` at line start. We accept any case +
# trailing whitespace, but require an exact match to "Appendix"
# (rejecting "Appendix (auto-extracted)" which has its own canonical
# heading and isn't a user-written marker).
_USER_APPENDIX_HEADING_RE = __import__("re").compile(
    r"^# Appendix\s*$", __import__("re").IGNORECASE | __import__("re").MULTILINE,
)


def _find_user_appendix_section_end(text: str) -> Optional[int]:
    """Return the character index where the user's # Appendix
    section ends (i.e. the start of the next # H1 boundary, or the
    end of the text). Returns None when no user-written # Appendix
    is present.

    "User-written # Appendix" means an H1 line whose text is exactly
    "Appendix" (case-insensitive). The auto-injected
    "# Appendix (auto-extracted)" heading is NOT treated as a user
    marker -- it gets stripped + re-injected fresh on the next pass.
    """
    import re
    match = _USER_APPENDIX_HEADING_RE.search(text)
    if match is None:
        return None
    # Find the next H1 boundary after the matched line.
    # Walk forward from after the matched heading; an H1 is any line
    # starting with "# " (single hash, space) that isn't itself an
    # auto-extracted variant of Appendix.
    next_h1 = re.search(r"^# (?!Appendix\b)", text[match.end():], re.MULTILINE)
    if next_h1 is None:
        # User's appendix runs to EOF; the section ends at the
        # very end of text.
        return len(text)
    return match.end() + next_h1.start()


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
