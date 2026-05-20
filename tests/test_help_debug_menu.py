"""Help > Debug submenu wiring + DependencyCheckDialog smoke tests."""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMenu  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _find_menu(parent_menu, name: str) -> QMenu | None:
    for action in parent_menu.actions():
        sub = action.menu()
        if sub is not None and action.text().replace("&", "") == name:
            return sub
    return None


def test_help_menu_has_debug_submenu_with_log_and_dep_check(qt_app):
    from meeting_notetaker.ui.main_window import MainWindow
    w = MainWindow()
    menubar = w.menuBar()
    help_menu = None
    for action in menubar.actions():
        if action.text().replace("&", "") == "Help":
            help_menu = action.menu()
            break
    assert help_menu is not None, "Help menu missing"

    debug_menu = _find_menu(help_menu, "Debug")
    assert debug_menu is not None, "Debug submenu missing"

    debug_items = [a.text().replace("&", "") for a in debug_menu.actions()]
    assert "View Log..." in debug_items
    assert "Check Dependencies..." in debug_items


def test_main_window_exposes_dependency_check_signal(qt_app):
    from meeting_notetaker.ui.main_window import MainWindow
    w = MainWindow()
    assert hasattr(w, "open_dependency_check_requested")
    # Signal emits as expected -- catch it via a probe
    captured = []
    w.open_dependency_check_requested.connect(lambda: captured.append(True))
    w.open_dependency_check_requested.emit()
    assert captured == [True]


def test_dependency_check_dialog_populates_without_crashing(qt_app):
    from meeting_notetaker.ui.dependency_check_dialog import DependencyCheckDialog
    dlg = DependencyCheckDialog()
    # Eight feature groups defined in dependency_check._GROUPS
    assert dlg._tree.topLevelItemCount() == 8
    # Summary label is populated with three counts
    text = dlg._summary_label.text()
    assert "OK" in text
    assert "MISSING" in text or "skipped" in text


def test_dependency_check_dialog_rerun_button_repopulates(qt_app):
    from meeting_notetaker.ui.dependency_check_dialog import DependencyCheckDialog
    dlg = DependencyCheckDialog()
    initial_top_count = dlg._tree.topLevelItemCount()
    # Clicking re-run should produce the same group count, not duplicate.
    dlg._rerun_btn.click()
    assert dlg._tree.topLevelItemCount() == initial_top_count


def test_dependency_check_dialog_copy_button_loads_report_into_clipboard(qt_app):
    from PyQt6.QtGui import QGuiApplication

    from meeting_notetaker.ui.dependency_check_dialog import DependencyCheckDialog
    dlg = DependencyCheckDialog()
    dlg._copy_btn.click()
    clipboard = QGuiApplication.clipboard()
    text = clipboard.text() if clipboard is not None else ""
    assert "Meeting Notetaker -- Dependency check" in text
    assert "Summary:" in text
