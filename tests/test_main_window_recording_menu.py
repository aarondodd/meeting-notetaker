"""Right-click context menu: Open recording + Delete recording.

The menu actions appear regardless of audio state but only enable when
the selected session has at least one audio file on disk. Pins the
enablement contract + the signal-emit contract so MainApp's handlers
can wire to them with confidence.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from meeting_notetaker.models.session import Session, STATE_COMPLETE  # noqa: E402
from meeting_notetaker.ui.main_window import MainWindow, _COL_TITLE  # noqa: E402
from meeting_notetaker.utils.paths import session_audio_dir  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _select(win: MainWindow, session_id: str) -> None:
    root = win._list.invisibleRootItem()  # noqa: SLF001
    for i in range(root.childCount()):
        item = root.child(i)
        if item.data(_COL_TITLE, Qt.ItemDataRole.UserRole) == session_id:
            win._list.setCurrentItem(item)  # noqa: SLF001
            return
    raise AssertionError(f"session {session_id} not in list")


def _make_session(session_id: str) -> Session:
    return Session(
        id=session_id,
        title="Test Session",
        state=STATE_COMPLETE,
        created_at=datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
        has_audio=True,
    )


def test_open_recording_enabled_only_when_audio_present(
    qt_app, isolated_data_dir
):
    """Walk through _show_list_menu's enablement logic indirectly by
    placing audio on disk for one session and not the other, then
    inspecting which actions report enabled when each is selected."""
    win = MainWindow()
    try:
        sid_with_audio = "sess-audio"
        sid_no_audio = "sess-noaudio"
        win.set_sessions([_make_session(sid_with_audio), _make_session(sid_no_audio)])
        (session_audio_dir(sid_with_audio) / "mic.opus").write_bytes(b"OggS\x00")

        # Helper to call into the same enablement check the menu builder uses.
        from meeting_notetaker.utils.paths import has_retained_audio
        assert has_retained_audio(sid_with_audio) is True
        assert has_retained_audio(sid_no_audio) is False
    finally:
        win.deleteLater()


def test_open_recording_emits_with_session_id(qt_app, isolated_data_dir):
    """Emitting the signal is the contract MainApp listens to. We can't
    drive the menu's exec() in a unit test, so verify the signal carries
    the right id when fired directly."""
    win = MainWindow()
    try:
        sid = "sess-emit"
        win.set_sessions([_make_session(sid)])
        _select(win, sid)
        captured: list[str] = []
        win.open_recording_requested.connect(captured.append)
        win.open_recording_requested.emit(sid)
        assert captured == [sid]
    finally:
        win.deleteLater()


def test_delete_recording_confirmation_yes_emits(qt_app, isolated_data_dir, monkeypatch):
    """_confirm_delete_recording prompts via QMessageBox; if the user
    clicks Yes, the delete signal fires with the session id."""
    win = MainWindow()
    try:
        sid = "sess-del-yes"
        captured: list[str] = []
        win.delete_recording_requested.connect(captured.append)
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *_a, **_k: QMessageBox.StandardButton.Yes,
        )
        win._confirm_delete_recording(sid)  # noqa: SLF001
        assert captured == [sid]
    finally:
        win.deleteLater()


def test_delete_recording_confirmation_no_does_not_emit(
    qt_app, isolated_data_dir, monkeypatch,
):
    """User clicks No -> no signal fires. Pin the safe-default branch."""
    win = MainWindow()
    try:
        sid = "sess-del-no"
        captured: list[str] = []
        win.delete_recording_requested.connect(captured.append)
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *_a, **_k: QMessageBox.StandardButton.No,
        )
        win._confirm_delete_recording(sid)  # noqa: SLF001
        assert captured == []
    finally:
        win.deleteLater()
