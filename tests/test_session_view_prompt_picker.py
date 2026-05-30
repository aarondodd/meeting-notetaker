"""Smoke test for the per-session prompt template picker.

The picker is a QComboBox in the SessionView synthesis row that lets
the user pick which prompt template applies to a given session.
Selection is persisted to the session's metadata.json by MainApp;
this test exercises only the widget's populate + select round-trip.
"""
from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def test_picker_populates_with_default_entry(qt_app):
    from meeting_notetaker.ui.session_view import SessionView

    sv = SessionView()
    sv.set_prompt_templates(["default", "standup", "one-on-one"])
    # The "default" template should appear as the "(default)" entry
    # (count includes it but the literal name "default" is not added
    # twice -- once as the friendly first entry, never as a separate
    # one).
    picker = sv._prompt_template_picker
    assert picker.count() == 3, [picker.itemText(i) for i in range(picker.count())]
    # First entry = "(default)" with empty data.
    assert picker.itemText(0) == "(default)"
    assert picker.itemData(0) == ""
    # Other entries.
    names = [picker.itemData(i) for i in range(picker.count())]
    assert "standup" in names
    assert "one-on-one" in names


def test_picker_restores_selection_from_saved_name(qt_app):
    from meeting_notetaker.ui.session_view import SessionView

    sv = SessionView()
    sv.set_prompt_templates(["default", "standup", "one-on-one"], selected="standup")
    assert sv.selected_prompt_template() == "standup"


def test_picker_default_when_saved_template_no_longer_exists(qt_app):
    """The user deleted the template they had selected; picker falls
    back to (default)."""
    from meeting_notetaker.ui.session_view import SessionView

    sv = SessionView()
    sv.set_prompt_templates(["default", "standup"], selected="deleted-template")
    # No exact match -> falls back to index 0 ((default)).
    assert sv.selected_prompt_template() == ""


def test_picker_set_during_population_does_not_emit_change_signal(qt_app):
    """The picker is repopulated whenever a session is selected;
    blockSignals during populate prevents a spurious save event from
    firing on every session-switch."""
    from meeting_notetaker.ui.session_view import SessionView

    sv = SessionView()
    captured: list[tuple[str, str]] = []
    sv.prompt_template_changed.connect(
        lambda sid, name: captured.append((sid, name))
    )
    sv.set_prompt_templates(["default", "standup"], selected="standup")
    assert captured == [], (
        "set_prompt_templates fired a change signal during populate; "
        "MainApp would persist on every session-switch, double-writing "
        "metadata.json."
    )


def test_picker_placeholder_reflects_settings_default(qt_app):
    """The first entry's label surfaces the Settings default name so
    the user sees which template will actually be used when no
    session-level override is set (#55). Data role stays empty so the
    resolution chain still runs at synthesis time."""
    from meeting_notetaker.ui.session_view import SessionView

    sv = SessionView()
    sv.set_prompt_templates(
        ["default", "standup", "one-on-one"],
        selected="",
        settings_default="one-on-one",
    )
    picker = sv._prompt_template_picker  # noqa: SLF001
    assert picker.itemText(0) == "(default: one-on-one)"
    assert picker.itemData(0) == ""
    # Selection should land on the placeholder when nothing's saved.
    assert picker.currentIndex() == 0
    assert sv.selected_prompt_template() == ""


def test_picker_placeholder_when_settings_default_empty(qt_app):
    """Empty Settings default -> the original "(default)" label is
    preserved (the placeholder still resolves to default.md via the
    runtime chain)."""
    from meeting_notetaker.ui.session_view import SessionView

    sv = SessionView()
    sv.set_prompt_templates(["default", "standup"], settings_default="")
    picker = sv._prompt_template_picker  # noqa: SLF001
    assert picker.itemText(0) == "(default)"


def test_picker_emits_on_user_change(qt_app):
    """When the user picks a different template, the change signal
    fires with the new template name."""
    from meeting_notetaker.ui.session_view import SessionView
    from meeting_notetaker.models.session import Session, STATE_COMPLETE

    sv = SessionView()
    sv.set_prompt_templates(["default", "standup", "one-on-one"])
    # Need a session set for the signal to fire (the handler guards
    # on self._session is not None).
    fake_session = Session(
        id="test-id", title="Test", state=STATE_COMPLETE,
        created_at="2026-05-22T12:00:00+00:00",
    )
    sv.set_session(
        fake_session, transcript="t", notes="", previous_notes_paths=[], live_notes=""
    )
    sv.set_prompt_templates(["default", "standup", "one-on-one"])

    captured: list[tuple[str, str]] = []
    sv.prompt_template_changed.connect(
        lambda sid, name: captured.append((sid, name))
    )
    # Find the index of "standup" and select it.
    picker = sv._prompt_template_picker
    standup_idx = next(
        i for i in range(picker.count()) if picker.itemData(i) == "standup"
    )
    picker.setCurrentIndex(standup_idx)

    assert captured == [("test-id", "standup")]
