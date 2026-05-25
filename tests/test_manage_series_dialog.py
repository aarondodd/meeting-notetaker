"""ManageSeriesDialog -- table population + action-button gating.

The dialog is a thin shell over ClassificationStore (which is heavily
tested separately). Tests here pin the contract the dialog adds on
top: table reflects current store state, action buttons disable
correctly based on selection + corpus size, and the reload after
each mutation paints fresh counts.

User interaction (QInputDialog / QMessageBox prompts) isn't tested
headlessly; those paths are covered manually + via the store's own
mutator tests.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.models.classification import ClassificationStore  # noqa: E402
from meeting_notetaker.ui.manage_series_dialog import (  # noqa: E402
    _NAME_COL,
    _COUNT_COL,
    ManageSeriesDialog,
    _format_created,
)


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture
def store(tmp_path):
    s = ClassificationStore(tmp_path / "classification.db")
    yield s
    s.close()


def test_table_populates_with_name_and_session_count(qt_app, store):
    a = store.get_or_create_series("Alpha")
    b = store.get_or_create_series("Beta")
    store.assign_series("s1", a.id)
    store.assign_series("s2", a.id)
    # b has zero sessions.
    dlg = ManageSeriesDialog(store)
    try:
        assert dlg._table.rowCount() == 2  # noqa: SLF001
        # Rows are name-sorted (Alpha before Beta).
        name_a = dlg._table.item(0, _NAME_COL).text()  # noqa: SLF001
        count_a = dlg._table.item(0, _COUNT_COL).text()  # noqa: SLF001
        name_b = dlg._table.item(1, _NAME_COL).text()  # noqa: SLF001
        count_b = dlg._table.item(1, _COUNT_COL).text()  # noqa: SLF001
        assert name_a == "Alpha" and count_a == "2"
        assert name_b == "Beta" and count_b == "0"
    finally:
        dlg.deleteLater()


def test_action_buttons_disabled_with_no_selection(qt_app, store):
    store.get_or_create_series("Solo")
    dlg = ManageSeriesDialog(store)
    try:
        # Nothing selected on open.
        assert not dlg._rename_btn.isEnabled()  # noqa: SLF001
        assert not dlg._merge_btn.isEnabled()  # noqa: SLF001
        assert not dlg._delete_btn.isEnabled()  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_merge_disabled_when_only_one_series_exists(qt_app, store):
    store.get_or_create_series("Solo")
    dlg = ManageSeriesDialog(store)
    try:
        dlg._table.selectRow(0)  # noqa: SLF001
        assert dlg._rename_btn.isEnabled()  # noqa: SLF001
        # Merge needs 2+ series; one alone has nowhere to merge into.
        assert not dlg._merge_btn.isEnabled()  # noqa: SLF001
        assert dlg._delete_btn.isEnabled()  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_merge_enabled_with_two_or_more_series(qt_app, store):
    store.get_or_create_series("Alpha")
    store.get_or_create_series("Beta")
    dlg = ManageSeriesDialog(store)
    try:
        dlg._table.selectRow(0)  # noqa: SLF001
        assert dlg._merge_btn.isEnabled()  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_selected_series_id_returns_id_from_first_column(qt_app, store):
    """ID is stashed on the name-column item's UserRole. Pinned so a
    future column reorder doesn't quietly break the action handlers
    that read this."""
    a = store.get_or_create_series("Alpha")
    dlg = ManageSeriesDialog(store)
    try:
        dlg._table.selectRow(0)  # noqa: SLF001
        assert dlg._selected_series_id() == a.id  # noqa: SLF001
        assert dlg._selected_series_name() == "Alpha"  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_reload_picks_up_external_mutations(qt_app, store):
    """If something else (e.g. another dialog or a save hook) adds
    a series while this dialog is open, _reload() catches up."""
    store.get_or_create_series("First")
    dlg = ManageSeriesDialog(store)
    try:
        assert dlg._table.rowCount() == 1  # noqa: SLF001
        store.get_or_create_series("Second")
        dlg._reload()  # noqa: SLF001
        assert dlg._table.rowCount() == 2  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_format_created_renders_iso_date_only():
    """Series creation timestamp shows as 'YYYY-MM-DD' (no time).
    Just enough metadata to disambiguate two series named the same
    way; minute-precision noise isn't useful here."""
    assert _format_created("2026-05-24T14:30:00Z").startswith("2026-")
    # 10 chars: YYYY-MM-DD.
    assert len(_format_created("2026-05-24T14:30:00Z")) == 10


def test_format_created_empty_and_garbage_safe():
    assert _format_created("") == ""
    # Garbage falls through to the raw string -- still better than
    # crashing on a hand-edited DB row.
    assert _format_created("not-a-date") == "not-a-date"
