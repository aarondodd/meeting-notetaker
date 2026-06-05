"""View menu tests (#80 followup, v0.7.7).

Pins:
  - View menu is registered between Tools and Help in the menu bar.
  - View > Pop Out My Notes Preview is single-session-gated.
  - View > Editor & Preview Fonts... emits open_fonts_settings_requested.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.models.session import STATE_COMPLETE, Session  # noqa: E402
from meeting_notetaker.ui.main_window import MainWindow, _COL_TITLE  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


def _make_session(session_id: str = "s-1") -> Session:
    return Session(
        id=session_id, title="Test", state=STATE_COMPLETE,
        created_at=datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
    )


def _select(win: MainWindow, session_id: str) -> None:
    root = win._list.invisibleRootItem()  # noqa: SLF001
    for i in range(root.childCount()):
        item = root.child(i)
        if item.data(_COL_TITLE, Qt.ItemDataRole.UserRole) == session_id:
            win._list.setCurrentItem(item)  # noqa: SLF001
            return
    raise AssertionError(f"session {session_id} not in list")


def test_view_menu_between_tools_and_help(qt_app):
    win = MainWindow()
    try:
        labels = [a.text().replace("&", "") for a in win.menuBar().actions()]
        assert labels == ["File", "Tools", "View", "Help"]
    finally:
        win.deleteLater()


def test_view_menu_carries_pop_out_and_fonts_entries(qt_app):
    win = MainWindow()
    try:
        view_actions: list[str] = []
        for top in win.menuBar().actions():
            if top.text().replace("&", "") == "View":
                for a in top.menu().actions():
                    if not a.isSeparator():
                        view_actions.append(a.text().replace("&", "").replace("&&", "&"))
        assert "Pop Out My Notes Preview" in view_actions
        assert any("Fonts" in label for label in view_actions)
    finally:
        win.deleteLater()


def test_pop_out_action_disabled_without_session(qt_app):
    win = MainWindow()
    try:
        assert win._action_pop_out_notes.isEnabled() is False  # noqa: SLF001
    finally:
        win.deleteLater()


def test_pop_out_action_enables_on_single_select(qt_app):
    win = MainWindow()
    try:
        win.set_sessions([_make_session("s-pop-1")])
        _select(win, "s-pop-1")
        assert win._action_pop_out_notes.isEnabled() is True  # noqa: SLF001
    finally:
        win.deleteLater()


def test_pop_out_action_emits_signal(qt_app):
    win = MainWindow()
    captured: list[str] = []
    win.pop_out_notes_preview_requested.connect(lambda: captured.append("hit"))
    try:
        win.set_sessions([_make_session("s-pop-2")])
        _select(win, "s-pop-2")
        win._action_pop_out_notes.trigger()  # noqa: SLF001
        assert captured == ["hit"]
    finally:
        win.deleteLater()


def test_fonts_shortcut_emits_signal(qt_app):
    """View > Editor & Preview Fonts... fires
    open_fonts_settings_requested so MainApp can pre-set the active
    section before opening the Settings dialog."""
    win = MainWindow()
    captured: list[str] = []
    win.open_fonts_settings_requested.connect(lambda: captured.append("hit"))
    try:
        # Find the action under the View menu.
        for top in win.menuBar().actions():
            if top.text().replace("&", "") == "View":
                for a in top.menu().actions():
                    if "Fonts" in a.text().replace("&", "").replace("&&", "&"):
                        a.trigger()
                        break
        assert captured == ["hit"]
    finally:
        win.deleteLater()
