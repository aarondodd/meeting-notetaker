"""Helpers for exporting note tabs to standalone files.

The bulk of the work happens at the UI layer (it owns the QTextDocument
that carries the rendered Markdown). The pieces here are the pure-Python
bits worth unit-testing without Qt: filename generation and the dialog
filter strings.
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
