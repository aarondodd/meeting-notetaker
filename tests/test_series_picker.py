"""Series picker tests for the New Session dialog (v0.7.7).

Three layers:
  (a) ClassificationStore.suggest_series_from_history -- the new
      prior-title-aware fuzzy match
  (b) NewSessionDialog -- editable picker with auto-suggest +
      user-touch override
  (c) Result round-trip through NewSessionResult
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.models.classification import ClassificationStore  # noqa: E402
from meeting_notetaker.ui.new_session_dialog import (  # noqa: E402
    NewSessionDialog,
    NewSessionResult,
)


@pytest.fixture
def store(tmp_path):
    s = ClassificationStore(tmp_path / "classification.db")
    yield s
    s.close()


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


# ----------------------------------------------------------------------
# (a) ClassificationStore.suggest_series_from_history


def test_suggest_returns_none_for_empty_input(store):
    assert store.suggest_series_from_history("") is None
    assert store.suggest_series_from_history("   ") is None


def test_suggest_falls_back_to_series_name_match_when_no_history(store):
    """No prior-title map provided -> only series names are scored.
    This is the "first session in a new series" path and must still
    catch the obvious cases."""
    series = store.get_or_create_series("Platform Team Sync")
    hit = store.suggest_series_from_history("Platform Team Sync 2026-06-05")
    assert hit is not None and hit.id == series.id


def test_suggest_picks_up_prior_session_title_match(store):
    """The real headline: a series name doesn't lexically appear in
    the new title, but a prior session under that series does. The
    suggest must score the prior session titles and return the
    series of the best-matching prior."""
    series = store.get_or_create_series("Weekly Jane 1:1")
    other = store.get_or_create_series("Q3 Planning")
    titles_by_series = {
        series.id: [
            "Aaron + Jane catch-up 2026-05-22",
            "Aaron + Jane catch-up 2026-05-29",
        ],
        other.id: ["Q3 kickoff", "Q3 retrospective"],
    }
    hit = store.suggest_series_from_history(
        "Aaron + Jane catch-up 2026-06-05",
        prior_session_titles_by_series=titles_by_series,
    )
    assert hit is not None and hit.id == series.id


def test_suggest_respects_threshold(store):
    """A completely unrelated title returns None even when a series
    name exists. The 0.7 default is lenient but not blanket."""
    store.get_or_create_series("Backend Migration")
    hit = store.suggest_series_from_history(
        "Customer Onboarding Review Q3",
    )
    assert hit is None


def test_suggest_session_title_wins_over_distant_series_name(store):
    """When the series name barely matches but a prior title is a
    near-exact match, the prior title's series wins."""
    near = store.get_or_create_series("Vendor Comparisons")
    far = store.get_or_create_series("Random Catchall")
    titles_by_series = {
        near.id: ["Acme Cloud vs Beta Systems"],
    }
    hit = store.suggest_series_from_history(
        "Acme Cloud vs Beta Systems -- follow-up",
        prior_session_titles_by_series=titles_by_series,
    )
    assert hit is not None and hit.id == near.id
    assert hit.id != far.id


def test_suggest_returns_none_with_no_series_at_all(store):
    """Empty store -> nothing to suggest."""
    assert store.suggest_series_from_history("Anything") is None


# ----------------------------------------------------------------------
# (b) NewSessionDialog -- picker shape + auto-suggest behavior


def test_dialog_picker_lists_none_then_existing_series(qt_app):
    dlg = NewSessionDialog(series_names=["Daily Standup", "Weekly 1:1"])
    try:
        picker = dlg._series_picker  # noqa: SLF001
        assert picker.itemText(0) == "(none)"
        assert picker.itemData(0) == ""
        assert picker.itemText(1) == "Daily Standup"
        assert picker.itemText(2) == "Weekly 1:1"
    finally:
        dlg.deleteLater()


def test_dialog_picker_is_editable_for_new_series_names(qt_app):
    """Editable mode is the entry point for creating a new series
    from the dialog -- the user types a name not in the list."""
    dlg = NewSessionDialog(series_names=["Standup"])
    try:
        assert dlg._series_picker.isEditable() is True  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_dialog_autofills_from_suggest_when_title_changes(qt_app):
    """The suggest callable is debounced; flush directly so the
    test doesn't wait."""
    calls: list[str] = []

    def suggest(title):
        calls.append(title)
        if "standup" in title.lower():
            return "Daily Standup"
        return None

    dlg = NewSessionDialog(
        series_names=["Daily Standup", "Weekly 1:1"],
        suggest_series=suggest,
    )
    try:
        dlg._title_edit.setText("Standup 2026-06-05")  # noqa: SLF001
        dlg._suggest_timer.stop()  # noqa: SLF001
        dlg._run_series_suggest()  # noqa: SLF001
        assert dlg._series_picker.currentText() == "Daily Standup"  # noqa: SLF001
        assert dlg._series_user_touched is False  # noqa: SLF001
        assert calls == ["Standup 2026-06-05"]
    finally:
        dlg.deleteLater()


def test_dialog_skips_autofill_after_user_picks_a_row(qt_app):
    """activated fires only on real user picks. Once the user has
    chosen, later title edits don't override."""

    def suggest(title):
        return "Weekly 1:1" if "1:1" in title else "Daily Standup"

    dlg = NewSessionDialog(
        series_names=["Daily Standup", "Weekly 1:1"],
        suggest_series=suggest,
    )
    try:
        # Simulate the user picking a row from the dropdown.
        dlg._series_picker.activated.emit(1)  # noqa: SLF001
        dlg._series_picker.setCurrentIndex(1)  # noqa: SLF001
        assert dlg._series_user_touched is True  # noqa: SLF001
        # Title change -> suggest runs, but auto-fill must not
        # apply because user_touched is True.
        dlg._title_edit.setText("1:1 with Manager 2026-06-05")  # noqa: SLF001
        dlg._suggest_timer.stop()  # noqa: SLF001
        dlg._run_series_suggest()  # noqa: SLF001
        # The picker stays on the user's pick (Daily Standup).
        assert dlg._series_picker.currentText() == "Daily Standup"  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_dialog_skips_autofill_after_user_types_new_name(qt_app):
    """The editable line edit's editTextChanged fires when the user
    types. That trips the user-touched sentinel too -- otherwise the
    auto-fill would clobber the partial name as the user types."""

    def suggest(title):
        return "Suggested Series"

    dlg = NewSessionDialog(
        series_names=["Existing"],
        suggest_series=suggest,
    )
    try:
        # Simulate the user typing into the combobox line edit.
        dlg._series_picker.setEditText("MyNewSeri")  # noqa: SLF001
        assert dlg._series_user_touched is True  # noqa: SLF001
        # Title change -> suggest ignored.
        dlg._title_edit.setText("Standup")  # noqa: SLF001
        dlg._suggest_timer.stop()  # noqa: SLF001
        dlg._run_series_suggest()  # noqa: SLF001
        assert dlg._series_picker.currentText() == "MyNewSeri"  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_dialog_initial_series_name_preselects_and_freezes_autofill(qt_app):
    """When a session is being created with an explicit pre-chosen
    series (e.g. a future entry point that wants to inherit from a
    sibling), the picker shows it and the auto-suggest is locked
    out from the start."""
    dlg = NewSessionDialog(
        series_names=["Daily Standup", "Weekly 1:1"],
        suggest_series=lambda t: "Daily Standup",
        initial_series_name="Weekly 1:1",
    )
    try:
        assert dlg._series_picker.currentText() == "Weekly 1:1"  # noqa: SLF001
        assert dlg._series_user_touched is True  # noqa: SLF001
    finally:
        dlg.deleteLater()


# ----------------------------------------------------------------------
# (c) Result round-trip


def test_result_value_carries_none_when_picker_left_on_sentinel(qt_app):
    dlg = NewSessionDialog(series_names=["Daily Standup"])
    try:
        dlg._title_edit.setText("Test Session")  # noqa: SLF001
        result = dlg.result_value()
        assert isinstance(result, NewSessionResult)
        assert result.series_name == ""
    finally:
        dlg.deleteLater()


def test_result_value_carries_picked_series_name(qt_app):
    dlg = NewSessionDialog(
        series_names=["Daily Standup", "Weekly 1:1"],
        suggest_series=lambda t: "Daily Standup",
    )
    try:
        dlg._title_edit.setText("Standup")  # noqa: SLF001
        dlg._suggest_timer.stop()  # noqa: SLF001
        dlg._run_series_suggest()  # noqa: SLF001
        result = dlg.result_value()
        assert result.series_name == "Daily Standup"
    finally:
        dlg.deleteLater()


def test_result_value_carries_user_typed_new_series_name(qt_app):
    """Editable combobox lets the user create a series by typing.
    The result carries that text verbatim for MainApp to feed into
    get_or_create_series."""
    dlg = NewSessionDialog(series_names=["Existing Series"])
    try:
        dlg._title_edit.setText("Brand New Topic")  # noqa: SLF001
        dlg._series_picker.setEditText("My New Series")  # noqa: SLF001
        result = dlg.result_value()
        assert result.series_name == "My New Series"
    finally:
        dlg.deleteLater()
