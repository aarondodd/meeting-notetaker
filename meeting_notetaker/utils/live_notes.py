"""Live notes template + attendee parsing.

The user takes notes during a meeting in parallel with the running transcript.
On a new session the live-notes file is seeded with a fixed template:

    # Attendees
    -

    # Agenda

    # Notes

    # Action Items

When the synthesis prompt is generated, the live notes are passed alongside
the transcript so the LLM can merge the user's framing/context with the
transcript-derived detail. Attendees are also parsed out of the bulleted
list under "# Attendees" and made available as a separate placeholder so
the LLM can assign action items to known people instead of "TBD".
"""
from __future__ import annotations

import re
from typing import Iterable


LIVE_NOTES_TEMPLATE = """# Attendees
-\x20

# Agenda

# Notes

# Action Items
"""

_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.*?)\s*$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.+?)\s*$")


def seed_body() -> str:
    """Initial content for a fresh live_notes.md file."""
    return LIVE_NOTES_TEMPLATE


def seed_body_with_calendar(
    *,
    attendees: list[str] | None = None,
    agenda: str = "",
) -> str:
    """Pre-fill the live-notes template with calendar-derived content.

    Used when a session is created via the Outlook calendar imminent-meeting
    flow. `attendees` populates the bulleted list under '# Attendees';
    `agenda` is dropped verbatim under '# Agenda' so the user keeps the
    Outlook invite text for reference.
    """
    names = [n for n in (attendees or []) if n]
    if names:
        attendee_block = "\n".join(f"- {n}" for n in names)
    else:
        attendee_block = "-\x20"
    agenda_block = (agenda or "").strip()
    return (
        f"# Attendees\n{attendee_block}\n\n"
        f"# Agenda\n{agenda_block}\n\n"
        f"# Notes\n\n"
        f"# Action Items\n"
    )


def parse_attendees(body: str) -> list[str]:
    """Extract attendee names from the bulleted list under '# Attendees'.

    Looks for a heading whose text equals 'Attendees' (case-insensitive),
    then collects subsequent bullet lines until the next heading or until
    a non-empty non-bullet line that breaks the list. Empty bullets and
    placeholder dashes are ignored. Trailing comments after '--' or '#'
    on a line are stripped.
    """
    if not body:
        return []
    lines = body.splitlines()
    in_section = False
    out: list[str] = []
    for raw in lines:
        heading = _HEADING_RE.match(raw)
        if heading:
            if in_section:
                break
            if heading.group(1).strip().lower() == "attendees":
                in_section = True
            continue
        if not in_section:
            continue
        bullet = _BULLET_RE.match(raw)
        if bullet:
            name = _strip_trailing_comment(bullet.group(1)).strip()
            if name:
                out.append(name)
            continue
        if raw.strip() == "":
            continue
        # A non-empty non-bullet line ends the attendees list.
        break
    # Dedupe while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for name in out:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(name)
    return unique


def _strip_trailing_comment(value: str) -> str:
    # Allow "- Name -- notes" style annotations; keep only the name.
    for sep in (" -- ", " #"):
        idx = value.find(sep)
        if idx >= 0:
            return value[:idx]
    return value


def format_attendee_list(attendees: Iterable[str]) -> str:
    """Render the attendee list for prompt substitution."""
    names = [n for n in attendees if n]
    if not names:
        return "(none specified)"
    return ", ".join(names)


def replace_attendees_section_with_table(
    body: str,
    contacts,
) -> str:
    """Swap the bullet list under `# Attendees` for a Markdown table.

    Issue #51 Phase 5. ``contacts`` is a sequence of Contact-shaped
    objects (must carry display_name + title/company/department/
    primary_email/phone). When any contact has any rich field set,
    the bullet list under the Attendees heading is replaced with a
    Markdown table; otherwise the body is returned unchanged. The
    caller (PDF export path) decides when to invoke this via the
    auto-detect rule documented in `should_render_attendees_as_table`.

    The table includes only columns where at least one contact has
    data (we don't pad an entire empty Phone column when no one has
    a phone number on file). Empty cells render as a single space
    so the Markdown converter doesn't collapse the row.
    """
    contacts = list(contacts or [])
    if not contacts:
        return body
    table_md = _format_attendee_table_markdown(contacts)
    if not table_md:
        return body
    lines = body.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        heading = _HEADING_RE.match(lines[i])
        if heading and heading.group(1).strip().lower() == "attendees":
            # Emit the heading line as-is.
            out.append(lines[i])
            i += 1
            # Skip any bullet / blank lines that constitute the
            # current section until we hit the next heading or
            # end-of-body.
            while i < len(lines):
                next_heading = _HEADING_RE.match(lines[i])
                if next_heading:
                    break
                if (
                    _BULLET_RE.match(lines[i])
                    or lines[i].strip() == ""
                ):
                    i += 1
                    continue
                # Non-bullet content under Attendees? Preserve it +
                # stop consuming.
                break
            # Insert the table, then a trailing blank line so the
            # next heading isn't glued to the table.
            out.append("")
            out.append(table_md)
            out.append("")
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def should_render_attendees_as_table(contacts) -> bool:
    """Auto-detect rule for the PDF rich-table option.

    Returns True when at least one contact has at least one rich
    field set (title / company / department / primary_email / phone).
    Bullet list is the right shape when there's nothing to show in
    the additional columns -- avoids a single-column table that's
    just the names with extra formatting.
    """
    for c in contacts or []:
        if (
            getattr(c, "title", None)
            or getattr(c, "company", None)
            or getattr(c, "department", None)
            or getattr(c, "primary_email", None)
            or getattr(c, "phone", None)
        ):
            return True
    return False


def _format_attendee_table_markdown(contacts) -> str:
    """Render contacts as a Markdown table.

    Only columns with at least one non-empty cell make it into the
    output. Name is always present. Empty cells are a single space
    so the GitHub-flavored Markdown converter doesn't collapse the
    row visually.
    """
    columns: list[tuple[str, str]] = [("Name", "display_name")]
    for header, attr in (
        ("Title", "title"),
        ("Company", "company"),
        ("Department", "department"),
        ("Email", "primary_email"),
        ("Phone", "phone"),
    ):
        if any(
            (getattr(c, attr, None) or "").strip()
            for c in contacts
        ):
            columns.append((header, attr))
    if len(columns) == 1:
        # Only names; not worth a table. Caller falls back to bullets.
        return ""
    header_row = "| " + " | ".join(h for h, _ in columns) + " |"
    sep_row = "|" + "|".join("---" for _ in columns) + "|"
    rows = [header_row, sep_row]
    for c in contacts:
        cells = []
        for _, attr in columns:
            v = getattr(c, attr, None)
            cells.append((v or " ").strip() or " ")
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join(rows)


def has_user_content(body: str) -> bool:
    """True if the user has written anything beyond the seeded template."""
    if not body:
        return False
    stripped = body.strip()
    if not stripped:
        return False
    return stripped != LIVE_NOTES_TEMPLATE.strip()


def extract_section(body: str, heading: str) -> str:
    """Return the text under the matching '# Heading' (case-insensitive).

    Stops at the next H1..H6. Returns "" if the heading is absent or empty.
    Bullets, prose, blank lines under the heading are all returned verbatim.
    """
    if not body:
        return ""
    target = heading.strip().lower()
    in_section = False
    out_lines: list[str] = []
    for raw in body.splitlines():
        h = _HEADING_RE.match(raw)
        if h:
            if in_section:
                break
            if h.group(1).strip().lower() == target:
                in_section = True
            continue
        if in_section:
            out_lines.append(raw)
    return "\n".join(out_lines).strip()
