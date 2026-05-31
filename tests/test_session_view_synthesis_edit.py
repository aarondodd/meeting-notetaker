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


def test_my_notes_tab_starts_in_edit_when_body_is_seed_template(qt_app, fake_session):
    """A fresh session whose live_notes is exactly the boilerplate
    seed should land in Edit so the user can immediately start typing.
    (#67 followup -- My Notes default-to-preview is opt-in: it only
    triggers when the user has actually written something beyond
    the # Attendees / # Agenda / # Notes / # Action Items seed.)"""
    from meeting_notetaker.utils.live_notes import LIVE_NOTES_TEMPLATE
    view = SessionView()
    view.set_session(
        fake_session,
        transcript="",
        notes="",
        previous_notes_paths=[],
        live_notes=LIVE_NOTES_TEMPLATE,
    )
    assert view._live_notes_editor.is_in_preview() is False


def test_my_notes_tab_starts_in_preview_when_body_has_user_content(qt_app, fake_session):
    """Once the live_notes body has anything beyond the seed, a
    session-select lands in Preview so the user reads first and
    clicks Edit only when they want to mutate."""
    from meeting_notetaker.utils.live_notes import LIVE_NOTES_TEMPLATE
    body = LIVE_NOTES_TEMPLATE + "\nDiscussed the Q3 roadmap.\n"
    view = SessionView()
    view.set_session(
        fake_session,
        transcript="",
        notes="",
        previous_notes_paths=[],
        live_notes=body,
    )
    assert view._live_notes_editor.is_in_preview() is True


def test_my_notes_tab_starts_in_edit_when_live_notes_empty(qt_app, fake_session):
    """An empty live_notes body counts as no-user-content; Edit
    keeps the keystroke path open."""
    view = SessionView()
    view.set_session(
        fake_session,
        transcript="",
        notes="",
        previous_notes_paths=[],
        live_notes="",
    )
    assert view._live_notes_editor.is_in_preview() is False


def test_my_notes_async_load_flips_to_preview_when_body_has_content(qt_app, fake_session):
    """The MainApp post-load path is two-phase: set_session() runs
    with live_notes="" (snappy UI swap), then the content worker
    calls set_live_notes_text(<real body>). The preview-mode default
    must be reapplied at the second step or the user lands in Edit
    even when the loaded body has notes -- which is the bug reported
    on the v0.7.5 PR."""
    from meeting_notetaker.utils.live_notes import LIVE_NOTES_TEMPLATE
    view = SessionView()
    # Phase 1: empty prelude, just like _on_session_selected does.
    view.set_session(
        fake_session,
        transcript="",
        notes="",
        previous_notes_paths=[],
        live_notes="",
    )
    assert view._live_notes_editor.is_in_preview() is False
    # Phase 2: worker delivers the real body via the public setter.
    body = LIVE_NOTES_TEMPLATE + "\nDiscussed Q3 roadmap commitments.\n"
    view.set_live_notes_text(body)
    assert view._live_notes_editor.is_in_preview() is True


def test_my_notes_async_load_stays_in_edit_when_body_is_seed(qt_app, fake_session):
    """Fresh session: worker delivers the seed body (no user
    content yet) -- editor stays in Edit so the next keystroke
    lands without a Preview-to-Edit click."""
    from meeting_notetaker.utils.live_notes import LIVE_NOTES_TEMPLATE
    view = SessionView()
    view.set_session(
        fake_session,
        transcript="",
        notes="",
        previous_notes_paths=[],
        live_notes="",
    )
    view.set_live_notes_text(LIVE_NOTES_TEMPLATE)
    assert view._live_notes_editor.is_in_preview() is False


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
    # v0.6.5 tab order: My Notes=0, Synthesis=1, Previous=2, Transcript=3.
    view._tabs.setCurrentIndex(1)
    assert view.active_tab_text() == "# Hello"
    assert view.active_tab_label() == "Synthesis"


def test_set_title_updates_label_and_session(qt_app, fake_session):
    """Rename UX path: set_title updates the visible label AND the bound
    Session.title so downstream consumers (synthesis prompt render) see
    the new value without a full set_session round-trip."""
    view = SessionView()
    view.set_session(fake_session, transcript="", notes="", previous_notes_paths=[])
    assert view._title_label.text() == "Editable Synthesis Test"
    view.set_title("Renamed Session")
    assert view._title_label.text() == "Renamed Session"
    assert view._session.title == "Renamed Session"


def test_set_title_ignores_empty_and_whitespace(qt_app, fake_session):
    view = SessionView()
    view.set_session(fake_session, transcript="", notes="", previous_notes_paths=[])
    view.set_title("   ")
    assert view._title_label.text() == "Editable Synthesis Test"
    view.set_title("")
    assert view._title_label.text() == "Editable Synthesis Test"


def test_set_title_is_safe_with_no_session(qt_app):
    view = SessionView()
    # No session bound -- must not crash.
    view.set_title("anything")
