"""InsertScreenshotDialog + LiveNotesWidget._insert_screenshot_action (#111)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PyQt6.QtWidgets import QApplication, QDialog  # noqa: E402

from meeting_notetaker.ui.insert_screenshot_dialog import (  # noqa: E402
    InsertScreenshotDialog,
)
from meeting_notetaker.ui.live_notes_widget import LiveNotesWidget  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _make_png(path: Path, *, w: int = 80, h: int = 50, color=(120, 80, 200)) -> None:
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(*color))
    p = QPainter(img)
    p.setPen(QColor(0, 0, 0))
    p.drawRect(0, 0, w - 1, h - 1)
    p.end()
    img.save(str(path), "PNG")


# ---- InsertScreenshotDialog ------------------------------------------------


def test_dialog_lists_supplied_screenshots(qt_app, tmp_path):
    paths = []
    for i in range(3):
        p = tmp_path / f"{i+1:04d}-cap.png"
        _make_png(p)
        paths.append(p)
    dlg = InsertScreenshotDialog(paths)
    assert dlg._list.count() == 3  # noqa: SLF001
    # Each row carries its source path on the UserRole data slot.
    stored = [
        Path(dlg._list.item(i).data(0x0100))  # noqa: SLF001 -- UserRole=0x0100
        for i in range(dlg._list.count())  # noqa: SLF001
    ]
    assert sorted(stored) == sorted(paths)


def test_dialog_empty_state_hides_list(qt_app):
    dlg = InsertScreenshotDialog([])
    # The grid is hidden; the empty-state label is the visible content.
    assert dlg._list.isHidden()  # noqa: SLF001
    assert dlg._empty_label.isVisible() or not dlg._empty_label.isHidden()  # noqa: SLF001


def test_dialog_ok_disabled_until_selection(qt_app, tmp_path):
    p = tmp_path / "0001-cap.png"
    _make_png(p)
    dlg = InsertScreenshotDialog([p])
    assert dlg._ok_btn.isEnabled() is False  # noqa: SLF001
    dlg._list.item(0).setSelected(True)  # noqa: SLF001
    # itemSelectionChanged is a queued signal in test harness; force
    # the update.
    dlg._on_selection_changed()  # noqa: SLF001
    assert dlg._ok_btn.isEnabled() is True  # noqa: SLF001


def test_dialog_accept_stores_selected_path(qt_app, tmp_path):
    paths = []
    for i in range(2):
        p = tmp_path / f"{i+1:04d}-cap.png"
        _make_png(p)
        paths.append(p)
    dlg = InsertScreenshotDialog(paths)
    dlg._list.item(1).setSelected(True)  # noqa: SLF001
    dlg._accept_if_selected()  # noqa: SLF001
    assert dlg.result() == QDialog.DialogCode.Accepted
    assert dlg.selected_path() == paths[1]


def test_dialog_accept_no_op_without_selection(qt_app, tmp_path):
    p = tmp_path / "0001-cap.png"
    _make_png(p)
    dlg = InsertScreenshotDialog([p])
    # No selection -> OK click should not accept.
    dlg._accept_if_selected()  # noqa: SLF001
    assert dlg.result() != QDialog.DialogCode.Accepted
    assert dlg.selected_path() is None


# ---- LiveNotesWidget._insert_screenshot_action wire-up ---------------------


def test_insert_screenshot_action_no_session_bound(qt_app, monkeypatch):
    """Without set_session_dir, the toolbar action warns + bails."""
    w = LiveNotesWidget()
    warnings: list[tuple] = []

    def fake_info(_self, title, text):
        warnings.append((title, text))

    from PyQt6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "information", fake_info)
    # Should not raise.
    w._insert_screenshot_action()  # noqa: SLF001
    assert len(warnings) == 1
    assert warnings[0][0] == "Insert Screenshot"


def test_insert_screenshot_action_inserts_markdown_ref(qt_app, tmp_path, monkeypatch):
    """End-to-end: bind a session_dir, drop a screenshot under
    screenshots/, accept the dialog -> editor body carries a
    markdown ref pointing at ``screenshots/<filename>``."""
    sess_dir = tmp_path / "session"
    (sess_dir / "screenshots").mkdir(parents=True)
    cap = sess_dir / "screenshots" / "0001-img.png"
    _make_png(cap)
    w = LiveNotesWidget()
    w.set_session_dir(sess_dir)

    # Force the dialog's exec() to auto-accept with our capture
    # selected so the test runs without a real user click.
    def fake_exec(self):
        if self._screenshots:  # noqa: SLF001
            self._list.item(0).setSelected(True)  # noqa: SLF001
            self._accept_if_selected()  # noqa: SLF001
            return QDialog.DialogCode.Accepted
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(InsertScreenshotDialog, "exec", fake_exec)
    w._insert_screenshot_action()  # noqa: SLF001
    body = w._editor.toPlainText()  # noqa: SLF001
    assert "screenshots/0001-img.png" in body
    # The default alt is the file stem.
    assert "![0001-img]" in body


def test_insert_screenshot_action_cancel_leaves_body_unchanged(qt_app, tmp_path, monkeypatch):
    sess_dir = tmp_path / "session"
    (sess_dir / "screenshots").mkdir(parents=True)
    _make_png(sess_dir / "screenshots" / "0001-img.png")
    w = LiveNotesWidget()
    w.set_session_dir(sess_dir)
    w._editor.setPlainText("Pre-existing notes.\n")  # noqa: SLF001

    monkeypatch.setattr(
        InsertScreenshotDialog, "exec",
        lambda self: QDialog.DialogCode.Rejected,
    )
    w._insert_screenshot_action()  # noqa: SLF001
    assert w._editor.toPlainText() == "Pre-existing notes.\n"  # noqa: SLF001


def test_insert_screenshot_action_empty_dir_still_opens_dialog(
    qt_app, tmp_path, monkeypatch,
):
    """No captures yet: the dialog still opens (empty-state path) and
    the user can cancel out cleanly. Verifies the editor doesn't
    receive a stray insert when there's nothing to choose from."""
    sess_dir = tmp_path / "session"
    sess_dir.mkdir()
    # No screenshots/ dir, no captures.
    w = LiveNotesWidget()
    w.set_session_dir(sess_dir)
    w._editor.setPlainText("Existing text.\n")  # noqa: SLF001

    monkeypatch.setattr(
        InsertScreenshotDialog, "exec",
        lambda self: QDialog.DialogCode.Rejected,
    )
    w._insert_screenshot_action()  # noqa: SLF001
    assert w._editor.toPlainText() == "Existing text.\n"  # noqa: SLF001
