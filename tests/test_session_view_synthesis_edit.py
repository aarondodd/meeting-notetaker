"""Editable Synthesis tab: signal emission + debounced save behavior.

Exercises SessionView at the Qt level using the offscreen platform.
Pattern matches tests/test_print_document.py.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.models.session import (
    STATE_COMPLETE,
    Session,
)
from meeting_notetaker.ui.session_view import SessionView  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture
def fake_session(tmp_path):
    return Session(
        id="test-session",
        title="Editable Synthesis Test",
        created_at="2026-05-17T10:00:00",
        state=STATE_COMPLETE,
        has_notes=True,
        retain_audio=False,
    )


def test_synthesis_tab_starts_in_preview_when_notes_have_content(qt_app, fake_session):
    view = SessionView()
    view.set_session(fake_session, transcript="", notes="# A note", previous_notes_paths=[])
    assert view._notes_view.is_in_preview() is True


def test_synthesis_tab_starts_in_edit_when_notes_empty(qt_app, fake_session):
    view = SessionView()
    view.set_session(fake_session, transcript="", notes="", previous_notes_paths=[])
    assert view._notes_view.is_in_preview() is False


def test_set_notes_text_does_not_emit_change_signal(qt_app, fake_session):
    """Programmatic load must not look like a user edit (would loop)."""
    view = SessionView()
    view.set_session(fake_session, transcript="", notes="initial", previous_notes_paths=[])
    received = []
    view.synthesis_notes_changed.connect(lambda sid, body: received.append((sid, body)))
    view.set_notes_text("updated by app")
    # Pump events long enough for any debounce timer to fire.
    QCoreApplication.processEvents()
    view._notes_save_timer.start(1)
    QCoreApplication.processEvents()
    assert received == []


def test_user_edit_emits_synthesis_notes_changed(qt_app, fake_session):
    """A user-typed change should propagate through the debounce timer."""
    view = SessionView()
    view.set_session(fake_session, transcript="", notes="initial", previous_notes_paths=[])
    # Move to Edit mode so we can mutate the buffer.
    view._notes_view.set_preview_mode(False)
    received = []
    view.synthesis_notes_changed.connect(lambda sid, body: received.append((sid, body)))
    # Simulate the user typing via the public API (this triggers textChanged
    # which starts the debounce timer).
    view._notes_view.setPlainText("initial edited")
    # Force the debounce to fire immediately.
    view.flush_pending_notes()
    assert received == [("test-session", "initial edited")]


def test_flush_pending_notes_is_safe_when_no_session(qt_app):
    view = SessionView()
    # No session set; flushing must not raise.
    view.flush_pending_notes()


def test_active_tab_text_returns_notes_source_for_synthesis(qt_app, fake_session):
    view = SessionView()
    view.set_session(fake_session, transcript="", notes="# Hello", previous_notes_paths=[])
    # Tabs: Transcript=0, My Notes=1, Synthesis=2, Previous=3
    view._tabs.setCurrentIndex(2)
    assert view.active_tab_text() == "# Hello"
    assert view.active_tab_label() == "Synthesis"
