"""AddressBookDialog smoke tests.

Heavy on dialog assembly + table population; the underlying
ClassificationStore mutations are tested separately. These tests
just verify the dialog wires up cleanly + reads the right shape
out of the store.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.models.classification import ClassificationStore  # noqa: E402
from meeting_notetaker.ui.address_book_dialog import AddressBookDialog  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture
def store(tmp_path):
    s = ClassificationStore(tmp_path / "classification.db")
    yield s
    s.close()


def test_dialog_lists_contacts(qt_app, store):
    store.create_contact("Alice")
    store.create_contact("Bob")
    dlg = AddressBookDialog(store)
    try:
        # Two contacts, two rows in the list.
        assert dlg._list.count() == 2  # noqa: SLF001
        labels = [dlg._list.item(i).text() for i in range(dlg._list.count())]  # noqa: SLF001
        # Alphabetical -- Alice before Bob.
        assert "Alice" in labels[0]
        assert "Bob" in labels[1]
    finally:
        dlg.deleteLater()


def test_dialog_filter_narrows_list(qt_app, store):
    store.create_contact("Alice Anderson")
    store.create_contact("Bob Smith")
    store.create_contact("Carol Reyes")
    dlg = AddressBookDialog(store)
    try:
        dlg._filter_input.setText("smith")  # noqa: SLF001
        assert dlg._list.count() == 1  # noqa: SLF001
        assert "Bob Smith" in dlg._list.item(0).text()  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_dialog_filter_matches_aliases(qt_app, store):
    bob = store.create_contact("Bob Smith")
    store.add_alias(bob.id, "BS", kind="short")
    store.create_contact("Alice")
    dlg = AddressBookDialog(store)
    try:
        dlg._filter_input.setText("BS")  # noqa: SLF001
        assert dlg._list.count() == 1  # noqa: SLF001
        assert "Bob Smith" in dlg._list.item(0).text()  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_dialog_details_pane_disabled_with_no_selection(qt_app, store):
    store.create_contact("Alice")
    dlg = AddressBookDialog(store)
    try:
        # No selection until the user picks; the action buttons
        # should reflect that.
        dlg._list.clearSelection()  # noqa: SLF001
        assert not dlg._rename_btn.isEnabled()  # noqa: SLF001
        assert not dlg._merge_btn.isEnabled()  # noqa: SLF001
        assert not dlg._delete_btn.isEnabled()  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_dialog_details_pane_populates_on_selection(qt_app, store):
    bob = store.create_contact("Bob Smith")
    store.add_alias(bob.id, "BS", kind="short")
    store.add_alias(bob.id, "bsmith@corp.com", kind="email")
    dlg = AddressBookDialog(store)
    try:
        dlg._list.setCurrentRow(0)  # noqa: SLF001
        # Details name displays the canonical form.
        assert dlg._detail_name.text() == "Bob Smith"  # noqa: SLF001
        # Alias table includes the name + the two we added.
        rows = dlg._alias_table.rowCount()  # noqa: SLF001
        assert rows >= 3
        # All three buttons enabled now that something's selected.
        assert dlg._rename_btn.isEnabled()  # noqa: SLF001
        assert dlg._delete_btn.isEnabled()  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_dialog_orphan_label_shows_count(qt_app, store):
    # Two orphans (no session links) + one in-use contact.
    store.create_contact("Orphan A")
    store.create_contact("Orphan B")
    used = store.create_contact("Used")
    store.add_session_contact("s1", used.id)
    dlg = AddressBookDialog(store)
    try:
        assert "2 orphan" in dlg._orphan_label.text()  # noqa: SLF001
        assert dlg._delete_orphans_btn.isEnabled()  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_dialog_orphan_button_disabled_when_no_orphans(qt_app, store):
    used = store.create_contact("Used")
    store.add_session_contact("s1", used.id)
    dlg = AddressBookDialog(store)
    try:
        assert not dlg._delete_orphans_btn.isEnabled()  # noqa: SLF001
        assert "No orphans" in dlg._orphan_label.text()  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_dialog_suggestions_section_visible_when_pairs_exist(qt_app, store):
    a = store.create_contact("Bob Smith")
    b = store.create_contact("Bob Smitn")  # 1-edit typo -> suggested merge
    dlg = AddressBookDialog(store)
    try:
        # Suggested merges scan picks up the near-duplicate.
        assert not dlg._suggestions_group.isHidden()  # noqa: SLF001
        assert dlg._suggestions_list.count() >= 1  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_dialog_suggestions_hidden_with_clean_catalog(qt_app, store):
    store.create_contact("Alice")
    store.create_contact("Charlie")
    store.create_contact("Eve")
    dlg = AddressBookDialog(store)
    try:
        assert dlg._suggestions_group.isHidden()  # noqa: SLF001
    finally:
        dlg.deleteLater()
