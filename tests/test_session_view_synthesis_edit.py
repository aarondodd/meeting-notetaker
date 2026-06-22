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


# ---- #116: refresh_button_state after synthesis lands ----------------------


def _empty_session():
    """Synthesis-just-finished state: has_notes is still False, notes
    buffer is what just landed via set_notes_text."""
    return Session(
        id="bug-116-session",
        title="Synthesis just finished",
        created_at="2026-06-18T13:00:00",
        state=STATE_COMPLETE,
        has_transcript=True,   # was transcribed
        has_notes=False,       # synthesis just landed but DB flip races refresh
        retain_audio=False,
    )


def _select_synthesis_tab(view: SessionView) -> None:
    view._tabs.setCurrentWidget(view._notes_page)  # noqa: SLF001


def test_refresh_button_state_unsticks_save_to_after_synthesis(qt_app):
    """Reproduces #116 exactly: session with has_notes=False but a
    populated notes buffer -- without refresh_button_state, the Save
    to... dropdown stays disabled until the user swaps sessions. After
    the refresh call, it's enabled."""
    view = SessionView()
    view.set_session(
        _empty_session(),
        transcript="some transcript content",
        notes="",
        previous_notes_paths=[],
    )
    _select_synthesis_tab(view)
    # Simulate the synthesis-result flow: set_synthesis_in_progress
    # toggled OFF earlier (buttons evaluated with empty buffer + has_
    # notes=False), then the notes buffer is populated AFTER. Force
    # the same shape here.
    view.set_notes_text("# Heading\n\nSynthesized content body.\n")
    # Without #116's refresh, the export dropdown stays greyed --
    # _update_print_button was last called with has_notes=False.
    # Now run refresh_button_state and verify the dropdown enables.
    view.refresh_button_state()
    assert view._export_pdf_btn.isEnabled() is True  # noqa: SLF001
    assert view._print_btn.isEnabled() is True  # noqa: SLF001


def test_refresh_button_state_picks_up_in_memory_has_notes_flip(qt_app):
    """The companion to the buffer-fallback path: when MainApp flips
    sv._session.has_notes = True directly, refresh_button_state must
    propagate that into the dropdown."""
    view = SessionView()
    view.set_session(
        _empty_session(),
        transcript="",
        notes="",
        previous_notes_paths=[],
    )
    _select_synthesis_tab(view)
    # Initial state: nothing in buffer, has_notes=False -> disabled.
    assert view._export_pdf_btn.isEnabled() is False  # noqa: SLF001
    # Caller flips the in-memory has_notes flag directly (matching
    # _apply_synthesis_result's sv_session.has_notes = True line).
    view._session.has_notes = True  # noqa: SLF001
    view.refresh_button_state()
    assert view._export_pdf_btn.isEnabled() is True  # noqa: SLF001


def test_refresh_button_state_no_op_with_no_session(qt_app):
    """Must not crash when called with no session bound (defensive)."""
    view = SessionView()
    # No session -- just verify the call returns cleanly.
    view.refresh_button_state()
    # Buttons remain disabled (no session is the right empty state).
    assert view._export_pdf_btn.isEnabled() is False  # noqa: SLF001


def test_refresh_button_state_my_notes_tab_unaffected_by_notes_buffer(qt_app):
    """My Notes tab's enabled state is independent of the Synthesis
    buffer -- the per-tab logic gates on tab membership, not on the
    contents of every tab. Sanity-check that refresh doesn't break
    that."""
    view = SessionView()
    view.set_session(
        _empty_session(),
        transcript="x",
        notes="",
        previous_notes_paths=[],
    )
    # Stay on My Notes tab.
    view._tabs.setCurrentWidget(view._live_notes_page)  # noqa: SLF001
    view.refresh_button_state()
    # My Notes path enables Save to / Print unconditionally (the
    # live-notes buffer is always editable, so there's always
    # something to export).
    assert view._export_pdf_btn.isEnabled() is True  # noqa: SLF001


# ---- #120: editor flush must NOT wipe the appendix sidecar ------------------


def test_flush_notes_does_not_clobber_appendix_sidecar(qt_app, fake_session):
    """Regression for #120 (appendix tray collapses to Links after save
    to Obsidian / export to Word).

    Setup: sidecar carries the four LLM sections (as it would after
    paste-back). The synthesis buffer is the *stripped* notes.md (no
    JSON blocks, matching the #93 always-on strip). A debounced editor
    flush MUST NOT call save_from_notes on the stripped buffer -- that
    parses zero entries and AppendixStore.save deletes the sidecar.
    """
    from meeting_notetaker.utils.appendix_store import AppendixStore
    from meeting_notetaker.utils.attendee_appendix import AttendeeAppendixEntry
    from meeting_notetaker.utils.attendee_context import AttendeeContextEntry
    from meeting_notetaker.utils.invite_mentions import InviteMentionEntry

    # Seed the sidecar as paste-back would have.
    store = AppendixStore(fake_session.id)
    store.save(
        attendee_context=[
            AttendeeContextEntry(name="Alice", observation="Lead PM"),
        ],
        attendee_details=[
            AttendeeAppendixEntry(
                name="Alice", title="PM", company="Acme",
                department="", email="alice@acme.test", phone="",
            ),
        ],
        topics=["Q3 roadmap", "Hiring"],
        referenced_attachments=[
            InviteMentionEntry(name="agenda.pdf", context="Reviewed"),
        ],
    )
    assert store.exists()

    view = SessionView()
    # Stripped notes body -- mirrors what set_notes_text receives from
    # _apply_synthesis_result after strip_all_appendices.
    view.set_session(
        fake_session,
        transcript="",
        notes="# Synthesis\n\nMeeting summary prose only.\n",
        previous_notes_paths=[],
    )
    # Simulate a user keystroke in the synthesis tab + the debounced
    # flush firing (the path that fires when the user has edited
    # the synthesis tab and then clicks Save to Obsidian / Export to
    # Word).
    view._notes_view.set_preview_mode(False)  # noqa: SLF001
    view._notes_view.setPlainText(  # noqa: SLF001
        "# Synthesis\n\nMeeting summary prose only. Edited.\n",
    )
    view.flush_pending_notes()

    # Sidecar must survive the flush.
    assert store.exists(), (
        "appendix sidecar was deleted by _flush_notes -- #120 regression"
    )
    ctx, details, topics, referenced = store.load_as_dataclasses()
    assert [e.name for e in ctx] == ["Alice"]
    assert [e.name for e in details] == ["Alice"]
    assert topics == ["Q3 roadmap", "Hiring"]
    assert [e.name for e in referenced] == ["agenda.pdf"]
