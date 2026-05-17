"""Export filename helpers (pure-Python, Qt-free)."""
from __future__ import annotations

from datetime import datetime

from meeting_notetaker.utils.export import (
    build_print_markdown,
    default_export_filename,
    sanitize_filename_stem,
    unique_export_path,
)


def test_sanitize_filename_stem_strips_invalid():
    assert sanitize_filename_stem('a:b\\c/d*e?f"g<h>i|j') == "a b c d e f g h i j"


def test_sanitize_filename_stem_collapses_whitespace():
    assert sanitize_filename_stem("foo   bar\tbaz") == "foo bar baz"


def test_sanitize_filename_stem_falls_back_when_empty():
    assert sanitize_filename_stem("") == "export"
    assert sanitize_filename_stem("///") == "export"
    assert sanitize_filename_stem("custom", fallback="x") == "custom"


def test_sanitize_filename_stem_drops_trailing_dots():
    assert sanitize_filename_stem("Meeting....") == "Meeting"


def test_default_export_filename_uses_session_title_and_tab():
    out = default_export_filename(
        "EDAPA-737 Sync", "Synthesis", ".pdf",
        now=datetime(2026, 5, 17),
    )
    assert out == "EDAPA-737 Sync -- Synthesis -- 2026-05-17.pdf"


def test_default_export_filename_handles_extension_without_dot():
    out = default_export_filename(
        "x", "Notes", "pdf", now=datetime(2026, 1, 2)
    )
    assert out.endswith(".pdf")


def test_default_export_filename_sanitizes_title():
    out = default_export_filename(
        'a/b?c"d', "Synthesis", ".pdf", now=datetime(2026, 5, 17)
    )
    assert "/" not in out
    assert "?" not in out
    assert '"' not in out
    assert out.endswith(".pdf")


def test_unique_export_path_returns_target_when_free(tmp_path):
    target = tmp_path / "doc.pdf"
    assert unique_export_path(target) == target


def test_unique_export_path_appends_when_taken(tmp_path):
    (tmp_path / "doc.pdf").write_text("x")
    assert unique_export_path(tmp_path / "doc.pdf") == tmp_path / "doc-2.pdf"


def test_unique_export_path_walks_until_free(tmp_path):
    (tmp_path / "doc.pdf").write_text("x")
    (tmp_path / "doc-2.pdf").write_text("x")
    (tmp_path / "doc-3.pdf").write_text("x")
    assert unique_export_path(tmp_path / "doc.pdf") == tmp_path / "doc-4.pdf"


def test_build_print_markdown_includes_title_and_tab():
    out = build_print_markdown(
        session_title="EDAPA-737 Sync",
        tab_label="My Notes",
        session_date=datetime(2026, 5, 17, 14, 30),
        body="# Attendees\n- Aaron\n",
    )
    assert out.startswith("# EDAPA-737 Sync -- My Notes\n")
    assert "*2026-05-17 14:30*" in out
    assert "---" in out
    # Body content survives intact, with a blank line before the rule.
    assert out.rstrip().endswith("- Aaron")


def test_build_print_markdown_handles_missing_date():
    out = build_print_markdown(
        session_title="X",
        tab_label="Synthesis",
        session_date=None,
        body="content",
    )
    # No italic-wrapped date line when session_date is None.
    assert "*" not in out.split("---")[0]
    assert out.startswith("# X -- Synthesis")
    assert "content" in out


def test_build_print_markdown_falls_back_for_empty_title():
    out = build_print_markdown(
        session_title="   ",
        tab_label="",
        session_date=None,
        body="",
    )
    # Both title and tab fall back rather than rendering a bare "-- ".
    assert "Untitled session" in out
    assert "Notes" in out
