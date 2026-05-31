"""Settings dialog nav + stacked pages (v0.7.5 redesign).

Pins:
  - Sections are sorted alphabetically.
  - Synthesis Automation + Synthesis Prompts collapsed to one
    "Synthesis" nav entry.
  - Backups nav entry exists (added in v0.7.5).
  - Selecting a nav row switches the QStackedWidget to that page.
  - Active section persists to config.ui.settings_active_section on
    accept, and is restored on next open.
  - There is exactly one entry per displayed page (no duplicates from
    the merge).
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.ui.settings_dialog import SettingsDialog  # noqa: E402
from meeting_notetaker.utils.config import Config  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


def _labels(dialog: SettingsDialog) -> list[str]:
    return [
        dialog._nav.item(i).text()  # noqa: SLF001
        for i in range(dialog._nav.count())  # noqa: SLF001
    ]


def test_sections_are_sorted_alphabetically(qt_app):
    cfg = Config()
    dlg = SettingsDialog(cfg)
    try:
        labels = _labels(dlg)
        assert labels == sorted(labels, key=str.lower)
    finally:
        dlg.deleteLater()


def test_synthesis_sections_are_merged(qt_app):
    """Pre-redesign the dialog had two separate Synthesis sections.
    The merge to one "Synthesis" page is the user-visible change."""
    cfg = Config()
    dlg = SettingsDialog(cfg)
    try:
        labels = _labels(dlg)
        assert labels.count("Synthesis") == 1
        assert "Synthesis Automation" not in labels
        assert "Synthesis Automation (optional)" not in labels
        assert "Synthesis Prompts" not in labels
    finally:
        dlg.deleteLater()


def test_expected_sections_present(qt_app):
    """All sections from the design land in the nav. Catches a typo'd
    label or a section accidentally dropped during refactor."""
    cfg = Config()
    dlg = SettingsDialog(cfg)
    try:
        labels = set(_labels(dlg))
    finally:
        dlg.deleteLater()
    expected = {
        "Audio", "Backups", "Calendar", "Export", "Interface",
        "Meeting Detection", "Screen Capture", "Speakers",
        "Synthesis", "Transcription",
    }
    assert expected == labels


def test_nav_row_switches_stack_page(qt_app):
    cfg = Config()
    dlg = SettingsDialog(cfg)
    try:
        # Pick the last row -- should switch the stack accordingly.
        last = dlg._nav.count() - 1  # noqa: SLF001
        dlg._nav.setCurrentRow(last)  # noqa: SLF001
        assert dlg._stack.currentIndex() == last  # noqa: SLF001
        dlg._nav.setCurrentRow(0)  # noqa: SLF001
        assert dlg._stack.currentIndex() == 0  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_active_section_restores_on_reopen(qt_app):
    """Selecting Backups and accepting writes the label into config;
    a second dialog instance starts on that page."""
    cfg = Config()
    dlg = SettingsDialog(cfg)
    try:
        labels = _labels(dlg)
        target = labels.index("Backups")
        dlg._nav.setCurrentRow(target)  # noqa: SLF001
        dlg._on_accept()  # noqa: SLF001
    finally:
        dlg.deleteLater()
    assert cfg.ui.settings_active_section == "Backups"

    # Reopen -- nav lands on Backups again.
    dlg2 = SettingsDialog(cfg)
    try:
        assert dlg2._nav.currentItem().text() == "Backups"  # noqa: SLF001
    finally:
        dlg2.deleteLater()


def test_first_launch_lands_on_alphabetical_first(qt_app):
    """Empty settings_active_section -> first nav entry. Audio is
    alphabetically first; pin so a future label rename surfaces here."""
    cfg = Config()
    cfg.ui.settings_active_section = ""
    dlg = SettingsDialog(cfg)
    try:
        assert dlg._nav.currentRow() == 0  # noqa: SLF001
        assert dlg._nav.currentItem().text() == "Audio"  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_unknown_active_section_falls_back_to_first(qt_app):
    """Config carrying a label that no longer exists (e.g. after a
    rename) should not error; the dialog falls back to the first
    entry."""
    cfg = Config()
    cfg.ui.settings_active_section = "Ghost Section"
    dlg = SettingsDialog(cfg)
    try:
        assert dlg._nav.currentRow() == 0  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_default_window_is_wide_enough_for_no_horizontal_scroll(qt_app):
    """Default sizing pins the no-horizontal-scroll contract."""
    cfg = Config()
    dlg = SettingsDialog(cfg)
    try:
        assert dlg.size().width() >= 800
    finally:
        dlg.deleteLater()


def test_export_default_folder_round_trips_through_dialog(qt_app, tmp_path):
    """Settings > Export > Default folder writes back to
    SynthesisConfig.export_default_folder on accept, and the field
    seeds the line edit with the saved value on next open."""
    target = tmp_path / "exports"
    target.mkdir()
    cfg = Config()
    cfg.synthesis.export_default_folder = str(target)
    dlg = SettingsDialog(cfg)
    try:
        # The text field is seeded from config.
        assert dlg._export_default_folder_edit.text() == str(target)  # noqa: SLF001
        # User edits + accepts -> config picks it up.
        new_target = tmp_path / "elsewhere"
        new_target.mkdir()
        dlg._export_default_folder_edit.setText(str(new_target))  # noqa: SLF001
        dlg._on_accept()  # noqa: SLF001
    finally:
        dlg.deleteLater()
    assert cfg.synthesis.export_default_folder == str(new_target)


def test_export_default_folder_blank_persists_as_empty(qt_app):
    """Clearing the field saves an empty string so save dialogs fall
    back to their per-call defaults."""
    cfg = Config()
    cfg.synthesis.export_default_folder = "/tmp/old"
    dlg = SettingsDialog(cfg)
    try:
        dlg._export_default_folder_edit.setText("")  # noqa: SLF001
        dlg._on_accept()  # noqa: SLF001
    finally:
        dlg.deleteLater()
    assert cfg.synthesis.export_default_folder == ""
