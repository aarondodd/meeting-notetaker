"""Helpers for exporting note tabs to standalone files.

The bulk of the work happens at the UI layer (it owns the QTextDocument
that carries the rendered Markdown). The pieces here are the pure-Python
bits worth unit-testing without Qt: filename generation, the dialog
filter strings, and the header that gets prepended to the printable
Markdown so the PDF identifies what it is.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Optional


_INVALID_CHARS_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')
_COLLAPSE_SPACES_RE = re.compile(r"\s+")


def sanitize_filename_stem(text: str, *, fallback: str = "export") -> str:
    """Return a filesystem-safe filename stem (no extension).

    Strips Windows-illegal characters, collapses whitespace to single
    spaces, trims, and falls back to `fallback` if the result would be
    empty. Trailing dots are stripped because Windows ignores them on
    create and the resulting path can collide.
    """
    cleaned = _INVALID_CHARS_RE.sub(" ", text or "")
    cleaned = _COLLAPSE_SPACES_RE.sub(" ", cleaned).strip()
    cleaned = cleaned.rstrip(".").strip()
    return cleaned or fallback


def default_export_filename(
    session_title: str,
    tab_label: str,
    extension: str,
    *,
    now: Optional[datetime] = None,
) -> str:
    """Build a default filename for an exported tab.

    Shape: "<sanitized title> -- <tab label> -- YYYY-MM-DD.<ext>".
    extension may be passed with or without a leading dot.
    """
    when = (now or datetime.now()).strftime("%Y-%m-%d")
    ext = extension if extension.startswith(".") else f".{extension}"
    stem = sanitize_filename_stem(f"{session_title} -- {tab_label} -- {when}")
    return f"{stem}{ext}"


def build_print_markdown(
    *,
    session_title: str,
    tab_label: str,
    session_date: Optional[datetime],
    body: str,
) -> str:
    """Prepend a title + metadata header to `body` for the printable doc.

    The header gives the printed page / PDF a clear identity (session
    title, which tab the content came from, the session's date). The
    horizontal rule separates the header from the note content; we
    insert a blank line before `body` so a leading `# Heading` in the
    note doesn't get glued to the rule by the Markdown converter.
    """
    title = (session_title or "Untitled session").strip() or "Untitled session"
    tab = (tab_label or "Notes").strip() or "Notes"
    if session_date is not None:
        try:
            date_str = session_date.strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError):
            date_str = ""
    else:
        date_str = ""

    lines = [f"# {title} -- {tab}"]
    if date_str:
        lines.append("")
        lines.append(f"*{date_str}*")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(body or "")
    return "\n".join(lines)


def unique_export_path(target: Path) -> Path:
    """Return `target` or, if it exists, a sibling with `-N` appended."""
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    parent = target.parent
    i = 2
    while True:
        candidate = parent / f"{stem}-{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1
