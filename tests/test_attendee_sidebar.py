"""AttendeeSidebar -- right-side click-to-tag panel."""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.ui.attendee_sidebar import AttendeeSidebar  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def test_empty_list_shows_hint(qt_app):
    sidebar = AttendeeSidebar()
    sidebar.set_attendees([])
    assert sidebar.attendee_names() == []
    # `isHidden` is the offscreen-safe inverse of an explicit setVisible(False).
    # isVisible() additionally requires a shown parent and is False in CI.
    assert not sidebar._empty_label.isHidden()
    # And once we add an attendee, the empty hint hides.
    sidebar.set_attendees(["Pat"])
    assert sidebar._empty_label.isHidden()


def test_attendees_sorted_alphabetically(qt_app):
    sidebar = AttendeeSidebar()
    sidebar.set_attendees(["Sam Wong", "Aaron Dodd", "pat lee"])
    assert sidebar.attendee_names() == ["Aaron Dodd", "pat lee", "Sam Wong"]


def test_duplicate_names_collapsed(qt_app):
    sidebar = AttendeeSidebar()
    sidebar.set_attendees(["Pat", "  Pat  ", "Pat", "Sam"])
    assert sidebar.attendee_names() == ["Pat", "Sam"]


def test_blank_names_dropped(qt_app):
    sidebar = AttendeeSidebar()
    sidebar.set_attendees(["", "   ", "Pat"])
    assert sidebar.attendee_names() == ["Pat"]


def test_tag_click_emits_name(qt_app):
    sidebar = AttendeeSidebar()
    sidebar.set_attendees(["Pat", "Sam"])
    captured: list[str] = []
    sidebar.tag_clicked.connect(captured.append)
    # Click Pat's button directly.
    pat_row = sidebar._rows["Pat"]
    pat_row._button.click()
    assert captured == ["Pat"]


def test_set_counts_renders_badge(qt_app):
    sidebar = AttendeeSidebar()
    sidebar.set_attendees(["Pat", "Sam"])
    sidebar.set_counts({"Pat": 3, "Sam": 1})
    assert sidebar._rows["Pat"]._count_label.text() == "×3"
    assert sidebar._rows["Sam"]._count_label.text() == "×1"


def test_zero_count_hides_badge(qt_app):
    sidebar = AttendeeSidebar()
    sidebar.set_attendees(["Pat"])
    sidebar.set_counts({"Pat": 0})
    assert sidebar._rows["Pat"]._count_label.text() == ""


def test_rebuilding_preserves_counts_for_kept_names(qt_app):
    sidebar = AttendeeSidebar()
    sidebar.set_attendees(["Pat", "Sam"])
    sidebar.set_counts({"Pat": 2, "Sam": 5})
    sidebar.set_attendees(["Pat", "Sam", "Maya"])
    assert sidebar._rows["Pat"]._count_label.text() == "×2"
    assert sidebar._rows["Sam"]._count_label.text() == "×5"
    assert sidebar._rows["Maya"]._count_label.text() == ""


def test_rebuilding_drops_counts_for_removed_names(qt_app):
    sidebar = AttendeeSidebar()
    sidebar.set_attendees(["Pat", "Sam"])
    sidebar.set_counts({"Pat": 2, "Sam": 5})
    sidebar.set_attendees(["Pat"])  # Sam removed
    sidebar.set_attendees(["Pat", "Sam"])  # Sam back -- count should be gone
    assert sidebar._rows["Pat"]._count_label.text() == "×2"
    assert sidebar._rows["Sam"]._count_label.text() == ""


def test_remove_last_emits_on_right_click(qt_app):
    sidebar = AttendeeSidebar()
    sidebar.set_attendees(["Pat"])
    captured: list[str] = []
    sidebar.remove_last_requested.connect(captured.append)
    sidebar._rows["Pat"]._on_right_click(None)
    assert captured == ["Pat"]
