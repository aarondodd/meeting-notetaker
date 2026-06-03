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
    """The placeholder entry's label surfaces the Settings default
    name (#55) so an expanded dropdown shows what the placeholder
    resolves to. The placeholder data stays empty so picking it
    explicitly clears any per-session override.

    Selection behavior is covered by
    test_picker_lands_on_settings_default_row_for_new_session (#76):
    when no per-session override exists, the dropdown points at the
    Settings default's row directly, not the placeholder, because
    the truncated "(default..stom)" form was unreadable inside the
    200px-wide combo and read as "Settings default is ignored."
    """
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


def test_picker_lands_on_settings_default_row_for_new_session(qt_app):
    """Regression for #76: when there is no per-session override but
    the user has picked a Settings default, the dropdown should show
    the chosen template's name directly so the user can see at a
    glance which template will run. The placeholder still exists in
    the list; the user can pick it explicitly to clear any
    per-session override later."""
    from meeting_notetaker.ui.session_view import SessionView

    sv = SessionView()
    sv.set_prompt_templates(
        ["default", "standup", "one-on-one"],
        selected="",
        settings_default="one-on-one",
    )
    picker = sv._prompt_template_picker  # noqa: SLF001
    # currentText shows the template name, not the placeholder.
    assert picker.currentData() == "one-on-one"
    assert picker.currentText() == "one-on-one"
    # selected_prompt_template returns the resolved name -- callers
    # like _on_send_to_llm see what the user sees.
    assert sv.selected_prompt_template() == "one-on-one"


def test_picker_falls_back_to_placeholder_when_settings_default_missing(qt_app):
    """If the Settings default points at a template that's no longer
    in the prompts folder (deleted, renamed), the dropdown falls
    back to the placeholder so something coherent is displayed.
    The runtime resolution chain handles the actual template lookup
    at Send time."""
    from meeting_notetaker.ui.session_view import SessionView

    sv = SessionView()
    sv.set_prompt_templates(
        ["default", "standup"],
        selected="",
        settings_default="ghost-template",
    )
    picker = sv._prompt_template_picker  # noqa: SLF001
    assert picker.currentIndex() == 0
    assert picker.currentData() == ""


def test_picker_per_session_override_wins_over_settings_default(qt_app):
    """Per-session override takes priority over the Settings default
    -- if the user explicitly picked 'standup' for this session, the
    dropdown shows 'standup' even if Settings default is something
    else."""
    from meeting_notetaker.ui.session_view import SessionView

    sv = SessionView()
    sv.set_prompt_templates(
        ["default", "standup", "one-on-one"],
        selected="standup",
        settings_default="one-on-one",
    )
    assert sv.selected_prompt_template() == "standup"


def test_picker_ignores_literal_default_as_settings_default(qt_app):
    """Settings default = 'default' is equivalent to no Settings
    default (the bundled template). The dropdown should show the
    placeholder, not try to find a 'default' row (which doesn't
    exist; that template is collapsed into the placeholder)."""
    from meeting_notetaker.ui.session_view import SessionView

    sv = SessionView()
    sv.set_prompt_templates(
        ["default", "standup"],
        selected="",
        settings_default="default",
    )
    picker = sv._prompt_template_picker  # noqa: SLF001
    assert picker.currentIndex() == 0
    assert picker.itemText(0) == "(default)"


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
