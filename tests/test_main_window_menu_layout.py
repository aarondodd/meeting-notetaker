"""Menu bar layout + per-session File actions (#79 followup,
2026-06-02 reorg).

Pins:
- File menu carries the session-action surface (rename, edit
  timestamp, export submenu, delete submenu, new + quit).
- Tools menu carries Settings, Manage Classification, Address
  Book, Backup Now, Restore from Backup. Settings + classification +
  address book moved here from File.
- Per-session File actions enable/disable based on the live
  session-list selection, mirroring the right-click menu's matrix.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMenuBar  # noqa: E402

from meeting_notetaker.models.session import Session, STATE_COMPLETE  # noqa: E402
from meeting_notetaker.ui.main_window import MainWindow, _COL_TITLE  # noqa: E402
from meeting_notetaker.utils.paths import session_audio_dir  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


def _menu_actions(menubar: QMenuBar, title: str) -> list[str]:
    """Return the action labels of the named top-level menu."""
    target = title.replace("&", "").lower()
    for top in menubar.actions():
        if top.text().replace("&", "").lower() == target:
            menu = top.menu()
            if menu is None:
                return []
            out: list[str] = []
            for a in menu.actions():
                if a.isSeparator():
                    out.append("---")
                else:
                    out.append(a.text().replace("&", ""))
            return out
    raise AssertionError(f"no top-level menu named {title!r}")


def _make_session(session_id: str = "s-1", *, has_audio: bool = True) -> Session:
    return Session(
        id=session_id,
        title="Test Session",
        state=STATE_COMPLETE,
        created_at=datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
        has_audio=has_audio,
    )


def _select(win: MainWindow, session_id: str) -> None:
    root = win._list.invisibleRootItem()  # noqa: SLF001
    for i in range(root.childCount()):
        item = root.child(i)
        if item.data(_COL_TITLE, Qt.ItemDataRole.UserRole) == session_id:
            win._list.setCurrentItem(item)  # noqa: SLF001
            return
    raise AssertionError(f"session {session_id} not in list")


# ---- File menu layout ----------------------------------------------------


def test_file_menu_has_session_actions_and_quit(qt_app):
    win = MainWindow()
    try:
        labels = _menu_actions(win.menuBar(), "File")
        assert "New Session..." in labels
        assert "Rename Session..." in labels
        assert "Edit Timestamp..." in labels
        assert "Export" in labels
        assert "Delete" in labels
        assert "Quit" in labels
    finally:
        win.deleteLater()


def test_file_menu_no_longer_carries_settings_or_catalog_actions(qt_app):
    """Reorg: Settings + Manage Classification + Address Book moved
    to Tools. Make sure they don't double-up under File too."""
    win = MainWindow()
    try:
        labels = _menu_actions(win.menuBar(), "File")
        assert "Settings..." not in labels
        assert "Manage Classification..." not in labels
        assert "Address Book..." not in labels
    finally:
        win.deleteLater()


def test_tools_menu_carries_moved_actions(qt_app):
    win = MainWindow()
    try:
        labels = _menu_actions(win.menuBar(), "Tools")
        assert "Settings..." in labels
        assert "Manage Classification..." in labels
        assert "Address Book..." in labels
        assert "Backup Now..." in labels
        assert "Restore from Backup..." in labels
    finally:
        win.deleteLater()


# ---- Enable/disable matrix ----------------------------------------------


def test_session_actions_disabled_when_no_selection(qt_app):
    win = MainWindow()
    try:
        assert not win._action_rename_session.isEnabled()  # noqa: SLF001
        assert not win._action_edit_timestamp.isEnabled()  # noqa: SLF001
        assert not win._action_export_recording.isEnabled()  # noqa: SLF001
        assert not win._action_export_video.isEnabled()  # noqa: SLF001
        assert not win._action_export_package.isEnabled()  # noqa: SLF001
        assert not win._action_delete_recording.isEnabled()  # noqa: SLF001
        assert not win._action_delete_session.isEnabled()  # noqa: SLF001
    finally:
        win.deleteLater()


def test_rename_and_full_session_export_enabled_on_single_selection(qt_app, tmp_path):
    win = MainWindow()
    try:
        win.set_sessions([_make_session("s-1", has_audio=False)])
        _select(win, "s-1")
        assert win._action_rename_session.isEnabled()  # noqa: SLF001
        assert win._action_edit_timestamp.isEnabled()  # noqa: SLF001
        assert win._action_export_package.isEnabled()  # noqa: SLF001
        # Recording / video exports require retained audio on disk.
        assert not win._action_export_recording.isEnabled()  # noqa: SLF001
        assert not win._action_export_video.isEnabled()  # noqa: SLF001
        # Delete-session works on any selection; delete-recording needs audio.
        assert win._action_delete_session.isEnabled()  # noqa: SLF001
        assert not win._action_delete_recording.isEnabled()  # noqa: SLF001
    finally:
        win.deleteLater()


def test_recording_actions_enable_when_audio_present_on_disk(
    qt_app, tmp_path, isolated_data_dir,
):
    win = MainWindow()
    try:
        win.set_sessions([_make_session("s-2", has_audio=True)])
        _select(win, "s-2")
        # Write a fake WAV so has_retained_audio returns True.
        audio_dir = session_audio_dir("s-2")
        audio_dir.mkdir(parents=True, exist_ok=True)
        (audio_dir / "mic.wav").write_bytes(b"RIFF....WAVEfmt ")
        win._refresh_session_actions()  # noqa: SLF001
        assert win._action_export_recording.isEnabled()  # noqa: SLF001
        assert win._action_export_video.isEnabled()  # noqa: SLF001
        assert win._action_delete_recording.isEnabled()  # noqa: SLF001
    finally:
        win.deleteLater()


# ---- Signal emit contract -----------------------------------------------


def test_rename_action_uses_same_path_as_right_click(qt_app, monkeypatch):
    """File > Rename triggers the same dialog + signal flow as the
    right-click Rename. We patch the input dialog so the test runs
    headlessly."""
    from PyQt6.QtWidgets import QInputDialog

    win = MainWindow()
    captured: list[tuple[str, str]] = []
    win.rename_session_requested.connect(
        lambda sid, title: captured.append((sid, title))
    )
    try:
        win.set_sessions([_make_session("s-3", has_audio=False)])
        _select(win, "s-3")
        monkeypatch.setattr(
            QInputDialog, "getText",
            lambda *args, **kwargs: ("Renamed Title", True),
        )
        win._action_rename_session.trigger()  # noqa: SLF001
        assert captured == [("s-3", "Renamed Title")]
    finally:
        win.deleteLater()


def test_export_full_session_action_emits_signal(qt_app):
    win = MainWindow()
    captured: list[str] = []
    win.export_package_requested.connect(captured.append)
    try:
        win.set_sessions([_make_session("s-4", has_audio=False)])
        _select(win, "s-4")
        win._action_export_package.trigger()  # noqa: SLF001
        assert captured == ["s-4"]
    finally:
        win.deleteLater()
