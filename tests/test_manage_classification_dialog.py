"""ManageClassificationDialog + CatalogManagerWidget smoke tests.

The dialog wraps two CatalogManagerWidgets (Series + Topics) in a
QTabWidget. The widget itself is catalog-agnostic via the
CatalogAdapter protocol; these tests verify both tabs assemble
correctly + the action buttons gate on selection size as expected.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.models.classification import ClassificationStore  # noqa: E402
from meeting_notetaker.ui.manage_classification_dialog import (  # noqa: E402
    CatalogManagerWidget,
    ManageClassificationDialog,
    _SeriesAdapter,
    _TopicsAdapter,
)


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture
def store(tmp_path):
    s = ClassificationStore(tmp_path / "classification.db")
    yield s
    s.close()


def test_dialog_has_two_tabs(qt_app, store):
    dlg = ManageClassificationDialog(store)
    try:
        assert dlg._tabs.count() == 2  # noqa: SLF001
        assert dlg._tabs.tabText(0) == "Series"  # noqa: SLF001
        assert dlg._tabs.tabText(1) == "Topics"  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_series_tab_populates_from_store(qt_app, store):
    a = store.get_or_create_series("Alpha")
    b = store.get_or_create_series("Beta")
    store.assign_series("s1", a.id)
    store.assign_series("s2", a.id)
    dlg = ManageClassificationDialog(store)
    try:
        series_widget = dlg._series_widget  # noqa: SLF001
        assert series_widget._table.rowCount() == 2  # noqa: SLF001
        # Alpha (with 2 sessions) shows up first; Beta with 0.
        assert series_widget._table.item(0, 0).text() == "Alpha"  # noqa: SLF001
        assert series_widget._table.item(0, 1).text() == "2"  # noqa: SLF001
        assert series_widget._table.item(1, 0).text() == "Beta"  # noqa: SLF001
        assert series_widget._table.item(1, 1).text() == "0"  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_topics_tab_populates_from_store(qt_app, store):
    mdm = store.get_or_create_topic("MDM")
    store.add_session_topic("s1", mdm.id, accepted=True)
    store.add_session_topic("s2", mdm.id, accepted=True)
    store.get_or_create_topic("Orphan")
    dlg = ManageClassificationDialog(store)
    try:
        topics_widget = dlg._topics_widget  # noqa: SLF001
        assert topics_widget._table.rowCount() == 2  # noqa: SLF001
        names = {
            topics_widget._table.item(i, 0).text()  # noqa: SLF001
            for i in range(topics_widget._table.rowCount())  # noqa: SLF001
        }
        assert names == {"MDM", "Orphan"}
    finally:
        dlg.deleteLater()


def test_topics_tab_has_cleanup_orphans_button(qt_app, store):
    store.get_or_create_topic("Orphan A")
    store.get_or_create_topic("Orphan B")
    dlg = ManageClassificationDialog(store)
    try:
        topics_widget = dlg._topics_widget  # noqa: SLF001
        # Orphan cleanup is a topics-tab feature, not series-tab.
        assert topics_widget._cleanup_btn is not None  # noqa: SLF001
        assert topics_widget._cleanup_btn.isEnabled()  # noqa: SLF001
        assert "(2)" in topics_widget._cleanup_btn.text()  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_series_tab_has_no_cleanup_button(qt_app, store):
    """Cleanup-orphans is only on the Topics tab -- series rarely
    accumulate orphans (you delete them, you don't auto-extract)."""
    dlg = ManageClassificationDialog(store)
    try:
        series_widget = dlg._series_widget  # noqa: SLF001
        assert series_widget._cleanup_btn is None  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_action_buttons_gate_on_selection(qt_app, store):
    store.get_or_create_series("Solo")
    dlg = ManageClassificationDialog(store)
    try:
        widget = dlg._series_widget  # noqa: SLF001
        # No selection -> nothing enabled.
        widget._table.clearSelection()  # noqa: SLF001
        widget._refresh_buttons()  # noqa: SLF001
        assert not widget._rename_btn.isEnabled()  # noqa: SLF001
        assert not widget._merge_btn.isEnabled()  # noqa: SLF001
        assert not widget._delete_btn.isEnabled()  # noqa: SLF001
        # Select the row.
        widget._table.selectRow(0)  # noqa: SLF001
        assert widget._rename_btn.isEnabled()  # noqa: SLF001
        # Merge still disabled (only one series exists).
        assert not widget._merge_btn.isEnabled()  # noqa: SLF001
        assert widget._delete_btn.isEnabled()  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_merge_enabled_with_two_series(qt_app, store):
    store.get_or_create_series("Alpha")
    store.get_or_create_series("Beta")
    dlg = ManageClassificationDialog(store)
    try:
        widget = dlg._series_widget  # noqa: SLF001
        widget._table.selectRow(0)  # noqa: SLF001
        assert widget._merge_btn.isEnabled()  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_topics_adapter_lists_with_session_counts(qt_app, store):
    """Direct unit on the adapter -- avoids constructing a full
    dialog when all we want to verify is the (id, name, count,
    created) tuple shape."""
    mdm = store.get_or_create_topic("MDM")
    store.add_session_topic("s1", mdm.id, accepted=True)
    store.add_session_topic("s2", mdm.id, accepted=True)
    adapter = _TopicsAdapter(store)
    items = adapter.list_items()
    by_name = {name: (i, n, c) for (i, name, n, c) in items}
    assert by_name["MDM"][1] == 2  # session count
    # Adapter for topics supports orphan cleanup.
    assert adapter.supports_bulk_orphan_delete() is True


def test_series_adapter_does_not_support_orphan_cleanup(qt_app, store):
    adapter = _SeriesAdapter(store)
    assert adapter.supports_bulk_orphan_delete() is False
