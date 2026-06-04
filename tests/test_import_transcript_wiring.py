"""End-to-end wiring tests for the Import Transcript action (#80).

Covers:
  - MainWindow's File menu carries the new "Import Transcript..." entry
  - The action enables only when a single session is selected
  - Triggering it emits import_transcript_requested
  - SessionView exposes the empty-state Import button when the loaded
    session has no transcript yet, and hides it once content arrives
  - TranscriptStore.set_imported_transcript writes raw.transcript.md
    atomically and is round-trippable via read_transcript

Offscreen Qt; no clipboard, no app context (MainApp's handler is
tested via the dialog + store helpers separately).
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

from meeting_notetaker.models.session import (  # noqa: E402
    STATE_COMPLETE,
    STATE_NEW,
    Session,
)
from meeting_notetaker.models.transcript import TranscriptStore  # noqa: E402
from meeting_notetaker.ui.main_window import MainWindow, _COL_TITLE  # noqa: E402
from meeting_notetaker.ui.session_view import SessionView  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


def _make_session(session_id: str = "s-1", *, state: str = STATE_NEW) -> Session:
    return Session(
        id=session_id,
        title="Test Session",
        state=state,
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


# ---- MainWindow: File menu entry -----------------------------------------


def test_file_menu_has_import_transcript_action(qt_app):
    win = MainWindow()
    try:
        labels: list[str] = []
        for top in win.menuBar().actions():
            if top.text().replace("&", "") == "File":
                for a in top.menu().actions():
                    if not a.isSeparator():
                        labels.append(a.text().replace("&", ""))
        assert "Import Transcript..." in labels
    finally:
        win.deleteLater()


def test_import_transcript_disabled_without_selection(qt_app):
    win = MainWindow()
    try:
        assert win._action_import_transcript.isEnabled() is False  # noqa: SLF001
    finally:
        win.deleteLater()


def test_import_transcript_enables_on_single_select(qt_app):
    """Should enable for any single selection, even brand-new sessions
    with no recording -- that's the headline scenario."""
    win = MainWindow()
    try:
        win.set_sessions([_make_session("s-import-1")])
        _select(win, "s-import-1")
        assert win._action_import_transcript.isEnabled() is True  # noqa: SLF001
    finally:
        win.deleteLater()


def test_import_transcript_action_emits_signal(qt_app):
    win = MainWindow()
    captured: list[str] = []
    win.import_transcript_requested.connect(lambda: captured.append("hit"))
    try:
        win.set_sessions([_make_session("s-import-2")])
        _select(win, "s-import-2")
        win._action_import_transcript.trigger()  # noqa: SLF001
        assert captured == ["hit"]
    finally:
        win.deleteLater()


# ---- SessionView: empty-state Import button ------------------------------


def test_empty_state_import_row_visible_when_no_transcript(qt_app):
    sv = SessionView()
    try:
        sv.set_session(
            _make_session("s-sv-1"),
            transcript="", notes="", previous_notes_paths=[], live_notes="",
        )
        # Hidden flag is False = the row is in its 'show me' state.
        # (isVisible() requires the widget tree to be shown; isHidden
        # tracks the explicit setVisible(False) call.)
        assert sv._transcript_empty_row.isHidden() is False  # noqa: SLF001
    finally:
        sv.deleteLater()


def test_empty_state_import_row_hidden_once_transcript_present(qt_app):
    sv = SessionView()
    try:
        sv.set_session(
            _make_session("s-sv-2"),
            transcript="Jane: hi there\n",
            notes="", previous_notes_paths=[], live_notes="",
        )
        assert sv._transcript_empty_row.isHidden() is True  # noqa: SLF001
    finally:
        sv.deleteLater()


def test_empty_state_import_button_emits_session_id(qt_app):
    sv = SessionView()
    captured: list[str] = []
    sv.import_transcript_clicked.connect(lambda sid: captured.append(sid))
    try:
        sv.set_session(
            _make_session("s-sv-3"),
            transcript="", notes="", previous_notes_paths=[], live_notes="",
        )
        sv._import_transcript_btn.click()  # noqa: SLF001
        assert captured == ["s-sv-3"]
    finally:
        sv.deleteLater()


def test_empty_state_import_row_hidden_when_no_session(qt_app):
    sv = SessionView()
    try:
        sv.set_session(None, transcript="", notes="", previous_notes_paths=[])
        assert sv._transcript_empty_row.isHidden() is True  # noqa: SLF001
    finally:
        sv.deleteLater()


# ---- TranscriptStore.set_imported_transcript -----------------------------


def test_set_imported_transcript_writes_file(qt_app, tmp_path, monkeypatch):
    """Pin the contract: writing an imported body lands at
    raw.transcript.md and round-trips through read_transcript with
    a trailing newline normalized in."""
    # Redirect %APPDATA% / XDG_CONFIG to tmp so we don't touch the
    # real user data dir.
    monkeypatch.setenv("MEETING_NOTETAKER_DATA_DIR", str(tmp_path))
    store = TranscriptStore("s-store-1")
    store.session_dir.mkdir(parents=True, exist_ok=True)
    body = "Jane: hello\nAaron: hi\n"
    path = store.set_imported_transcript(body)
    assert path == store.transcript_path
    assert path.exists()
    assert store.read_transcript() == body


def test_set_imported_transcript_adds_trailing_newline_if_missing(
    qt_app, tmp_path, monkeypatch,
):
    monkeypatch.setenv("MEETING_NOTETAKER_DATA_DIR", str(tmp_path))
    store = TranscriptStore("s-store-2")
    store.session_dir.mkdir(parents=True, exist_ok=True)
    body = "single line no newline"
    store.set_imported_transcript(body)
    assert store.read_transcript() == "single line no newline\n"


def test_set_imported_transcript_overwrites_existing(
    qt_app, tmp_path, monkeypatch,
):
    """Importing over an existing transcript replaces it. The MainApp
    handler asks for confirmation first; the store-level method is
    unconditional."""
    monkeypatch.setenv("MEETING_NOTETAKER_DATA_DIR", str(tmp_path))
    store = TranscriptStore("s-store-3")
    store.session_dir.mkdir(parents=True, exist_ok=True)
    store.transcript_path.write_text("old content\n", encoding="utf-8")
    store.set_imported_transcript("new content\n")
    assert store.read_transcript() == "new content\n"
