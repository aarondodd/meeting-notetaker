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


# ---- rich fields form (issue #51 Phase 1) ----------------------------------


def test_rich_field_inputs_populate_from_selected_contact(qt_app, store):
    """Selecting a contact whose rich fields are set must surface
    those values in the form inputs."""
    from meeting_notetaker.models.classification import ENRICH_SOURCE_OUTLOOK
    bob = store.create_contact("Bob Smith")
    store.update_contact_fields(
        bob.id,
        title="CEO",
        company="Bobco",
        department="Executive",
        primary_email="bob@bobco.com",
        phone="+1-555-0100",
        source=ENRICH_SOURCE_OUTLOOK,
    )
    dlg = AddressBookDialog(store)
    try:
        dlg._list.setCurrentRow(0)  # noqa: SLF001
        assert dlg._title_input.text() == "CEO"  # noqa: SLF001
        assert dlg._company_input.text() == "Bobco"  # noqa: SLF001
        assert dlg._department_input.text() == "Executive"  # noqa: SLF001
        assert dlg._email_input.text() == "bob@bobco.com"  # noqa: SLF001
        assert dlg._phone_input.text() == "+1-555-0100"  # noqa: SLF001
        # Source badge present in the header text (envelope emoji for
        # Outlook, per Aaron's spec).
        header = dlg._detail_name.text()  # noqa: SLF001
        assert "Bob Smith" in header
        assert "\U0001F4E7" in header, f"expected envelope emoji in header, got {header!r}"
    finally:
        dlg.deleteLater()


def test_rich_field_edit_persists_to_store(qt_app, store):
    """Typing in a form field + tabbing out should save the value
    via editingFinished. We simulate by setting text + calling the
    handler directly (Qt-event simulation is brittle in headless)."""
    from meeting_notetaker.models.classification import ENRICH_SOURCE_MANUAL
    bob = store.create_contact("Bob Smith")
    dlg = AddressBookDialog(store)
    try:
        dlg._list.setCurrentRow(0)  # noqa: SLF001
        # Simulate the user typing in the title field + tabbing out.
        dlg._title_input.setText("VP of Engineering")  # noqa: SLF001
        dlg._on_field_edited("title", "VP of Engineering")  # noqa: SLF001
        # Persisted to the store.
        updated = store.get_contact(bob.id)
        assert updated.title == "VP of Engineering"
        # Source is manual when the user edits via the form.
        assert updated.last_enriched_source == ENRICH_SOURCE_MANUAL
    finally:
        dlg.deleteLater()


def test_rich_field_empty_input_clears_value(qt_app, store):
    """Clearing a field (empty string) stores NULL, not empty string,
    so the enrichment paths' 'fill empty fields' logic treats it as
    available for re-filling."""
    bob = store.create_contact("Bob Smith")
    store.update_contact_fields(bob.id, title="VP")
    dlg = AddressBookDialog(store)
    try:
        dlg._list.setCurrentRow(0)  # noqa: SLF001
        dlg._title_input.setText("")  # noqa: SLF001
        dlg._on_field_edited("title", "")  # noqa: SLF001
        assert store.get_contact(bob.id).title is None
    finally:
        dlg.deleteLater()


def test_no_badge_for_unenriched_contact(qt_app, store):
    """A contact with last_enriched_source = NULL renders just the
    name; no trailing whitespace from a missing badge."""
    store.create_contact("Plain Jane")
    dlg = AddressBookDialog(store)
    try:
        dlg._list.setCurrentRow(0)  # noqa: SLF001
        assert dlg._detail_name.text() == "Plain Jane"  # noqa: SLF001
    finally:
        dlg.deleteLater()
