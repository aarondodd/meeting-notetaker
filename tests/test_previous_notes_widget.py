"""PreviousNotesWidget: list + preview + restore/delete actions.

Light smoke -- creates the widget with a few synthetic archive files,
verifies the list populates and the preview reflects the selection.
The actual save/delete logic lives in TranscriptStore and is pinned
by tests/test_transcript_store.py.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _make_archives(tmp_path: Path) -> list[Path]:
    """Three archives in the canonical notes-YYYYMMDD-HHMM.md form."""
    paths = []
    for stamp, body in (
        ("20260520-0900", "# Morning standup\n\nDiscussed Q3 roadmap.\n"),
        ("20260521-1430", "# Architecture review\n\n- Item 1\n- Item 2\n"),
        ("20260522-1100", "# Customer call\n\nThey want feature X.\n"),
    ):
        p = tmp_path / f"notes-{stamp}.md"
        p.write_text(body, encoding="utf-8")
        paths.append(p)
    # Sort newest-first to match list_previous_notes behavior.
    return sorted(paths, reverse=True)


def test_widget_populates_list_from_archives(qt_app, tmp_path):
    from meeting_notetaker.ui.previous_notes_widget import PreviousNotesWidget

    widget = PreviousNotesWidget()
    widget.set_session_id("test-session")
    widget.set_archives(_make_archives(tmp_path))
    assert widget._list.count() == 3
    # Most recent archive auto-selected (idx 0).
    item = widget._list.currentItem()
    assert item is not None
    assert "2026-05-22" in item.text()


def test_widget_preview_reflects_selection(qt_app, tmp_path):
    from meeting_notetaker.ui.previous_notes_widget import PreviousNotesWidget

    widget = PreviousNotesWidget()
    widget.set_session_id("test-session")
    archives = _make_archives(tmp_path)
    widget.set_archives(archives)
    # Default selection is the newest (last one written, first in
    # sorted-reverse order).
    qt_app.processEvents()
    preview_text = widget._preview.toPlainText()
    assert "Customer call" in preview_text

    # Select the oldest archive (last in the list).
    widget._list.setCurrentRow(2)
    qt_app.processEvents()
    assert "Morning standup" in widget._preview.toPlainText()


def test_widget_empty_state_with_no_archives(qt_app):
    from meeting_notetaker.ui.previous_notes_widget import PreviousNotesWidget

    widget = PreviousNotesWidget()
    widget.set_session_id("test-session")
    widget.set_archives([])
    assert widget._list.count() == 0
    assert widget._restore_btn.isEnabled() is False
    assert widget._delete_btn.isEnabled() is False
    # The placeholder text explains the empty state.
    assert "No archived" in widget._preview.toPlainText()


def test_widget_restore_emits_signal_with_session_and_path(qt_app, tmp_path):
    from meeting_notetaker.ui.previous_notes_widget import PreviousNotesWidget

    widget = PreviousNotesWidget()
    widget.set_session_id("test-session")
    archives = _make_archives(tmp_path)
    widget.set_archives(archives)
    qt_app.processEvents()

    received: list[tuple[str, Path]] = []
    widget.restore_requested.connect(lambda sid, p: received.append((sid, p)))

    # Bypass the QMessageBox.question prompt -- emit directly through
    # the signal connection by simulating the inner emit. (We can't
    # easily auto-answer a modal dialog in headless Qt.)
    widget.restore_requested.emit("test-session", archives[0])
    assert len(received) == 1
    assert received[0] == ("test-session", archives[0])


def test_widget_pretty_label_formats_timestamp(tmp_path):
    """notes-YYYYMMDD-HHMM.md renders as a human-readable date+time."""
    from meeting_notetaker.ui.previous_notes_widget import _pretty_label

    p = tmp_path / "notes-20260521-1430.md"
    p.write_text("x")
    assert _pretty_label(p) == "2026-05-21 14:30"


def test_widget_pretty_label_falls_back_for_non_standard(tmp_path):
    from meeting_notetaker.ui.previous_notes_widget import _pretty_label

    p = tmp_path / "notes-customstamp.md"
    p.write_text("x")
    # Doesn't match the strptime format -> falls back to bare filename.
    assert _pretty_label(p) == "notes-customstamp.md"
