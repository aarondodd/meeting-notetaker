"""Cross-session search dialog -- helper functions + result formatting.

The dialog itself is UI-heavy and exercised via a separate Qt fixture;
the helpers here are pure-Python and represent the load-bearing
sanitization + display logic (HTML escaping of snippets, source-badge
mapping, local-time date rendering, archive-name routing).
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtCore")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from meeting_notetaker.models.search_index import (  # noqa: E402
    SOURCE_LIVE_NOTES,
    SOURCE_NOTES,
    SOURCE_NOTES_ARCHIVE,
    SOURCE_TRANSCRIPT,
    SNIPPET_END_MARKER,
    SNIPPET_START_MARKER,
    SearchHit,
)
from meeting_notetaker.ui.search_dialog import (  # noqa: E402
    SessionSummary,
    _format_local_date,
    format_result_row,
    format_snippet_html,
)


# format_snippet_html


def test_snippet_html_replaces_markers_with_bold():
    raw = f"alpha {SNIPPET_START_MARKER}mdm{SNIPPET_END_MARKER} omega"
    out = format_snippet_html(raw)
    assert "<b>mdm</b>" in out
    assert "alpha" in out and "omega" in out


def test_snippet_html_escapes_html_in_user_content():
    """A transcript line like '<script>alert(1)</script>' must arrive
    at QLabel as escaped text, not executable markup. Marker
    replacement happens AFTER escape so the <b> tags survive."""
    raw = (
        f"<script>{SNIPPET_START_MARKER}foo{SNIPPET_END_MARKER}</script>"
    )
    out = format_snippet_html(raw)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "<b>foo</b>" in out


def test_snippet_html_empty_input_returns_empty():
    assert format_snippet_html("") == ""


# _format_local_date


def test_format_local_date_handles_zulu_iso(monkeypatch):
    """Round-trips the same way the session-list does; pinning a TZ
    so the assertion is deterministic across dev hosts."""
    import time
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset not available on this platform")
    monkeypatch.setenv("TZ", "Pacific/Honolulu")
    time.tzset()
    out = _format_local_date("2026-05-24T21:00:00Z")
    assert out == "2026-05-24 11:00"


def test_format_local_date_empty_returns_empty():
    assert _format_local_date("") == ""


def test_format_local_date_garbage_returns_empty():
    assert _format_local_date("not-a-date") == ""


# format_result_row


def _make_hit(source: str, *, archive_name=None, snippet="hello world",
              session_id="sess-1") -> SearchHit:
    return SearchHit(
        session_id=session_id, source=source,
        archive_name=archive_name, snippet=snippet, rank=1.0,
    )


def _make_summary(*, title="My Sync", created_at="2026-05-24T00:00:00Z") -> SessionSummary:
    return SessionSummary(
        session_id="sess-1", title=title, created_at=created_at,
    )


def test_format_result_row_has_title_date_source_when_summary_present():
    hit = _make_hit(SOURCE_TRANSCRIPT)
    summary = _make_summary(title="Platform Team Sync")
    row = format_result_row(hit, summary)
    assert "Platform Team Sync" in row
    assert "Transcript" in row  # source label


def test_format_result_row_falls_back_to_session_id_without_summary():
    hit = _make_hit(SOURCE_LIVE_NOTES, session_id="abc12345-deadbeef")
    row = format_result_row(hit, None)
    # Truncated to first 8 chars; surfaces enough to debug an orphan
    # without dumping the full UUID in the UI.
    assert "abc12345" in row


def test_format_result_row_shows_archive_name_for_notes_archive_hits():
    hit = _make_hit(
        SOURCE_NOTES_ARCHIVE,
        archive_name="notes-20260501-1430.md",
    )
    row = format_result_row(hit, _make_summary())
    assert "notes-20260501-1430.md" in row


def test_format_result_row_omits_archive_name_for_non_archive_sources():
    hit = _make_hit(SOURCE_NOTES, archive_name=None)
    row = format_result_row(hit, _make_summary())
    assert "notes-" not in row


def test_format_result_row_escapes_html_in_title():
    """Session titles are user-entered. A <script> tag in a title
    must NOT make it through to the dialog as raw markup."""
    hit = _make_hit(SOURCE_TRANSCRIPT, snippet="body")
    summary = _make_summary(title="<script>alert(1)</script>")
    row = format_result_row(hit, summary)
    assert "<script>" not in row
    assert "&lt;script&gt;" in row


def test_format_result_row_uses_friendly_source_label_for_each_kind():
    """Each source key has a human label -- regressing the mapping
    would surface 'notes_archive' to the user instead of 'Previous
    Notes', which would read weirdly."""
    expected = {
        SOURCE_TRANSCRIPT:    "Transcript",
        SOURCE_LIVE_NOTES:    "My Notes",
        SOURCE_NOTES:         "Synthesis",
        SOURCE_NOTES_ARCHIVE: "Previous Notes",
    }
    for source, label in expected.items():
        row = format_result_row(
            _make_hit(source, archive_name="x" if source == SOURCE_NOTES_ARCHIVE else None),
            _make_summary(),
        )
        assert label in row, f"missing friendly label for {source}: {row!r}"
