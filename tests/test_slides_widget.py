"""SlidesWidget: thumbnail grid + full-view nav + right-click menu.

Pins the navigation contract (click thumb -> full view, prev / next /
back work as expected, list refreshes preserve state where possible).
The right-click menu's actions hand off via signals; we verify
delete_requested fires after a "Yes" confirmation.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from meeting_notetaker.ui.slides_widget import SlidesWidget  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _make_png(path: Path, *, w: int = 80, h: int = 50, color=(100, 150, 200)) -> None:
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(*color))
    p = QPainter(img)
    p.setPen(QColor(0, 0, 0))
    p.drawRect(0, 0, w - 1, h - 1)
    p.end()
    img.save(str(path), "PNG")


def test_empty_state(qt_app):
    w = SlidesWidget()
    w.set_screenshots([])
    # Grid heading is visible when there's nothing to show.
    assert w._grid_heading.isVisible() or w._grid_heading.isHidden() is False  # noqa: SLF001
    assert w._list.count() == 0  # noqa: SLF001


def test_populated_grid(qt_app, tmp_path):
    paths = []
    for i in range(3):
        p = tmp_path / f"{i+1:04d}-20260523T1432{i:02d}Z.png"
        _make_png(p)
        paths.append(p)
    w = SlidesWidget()
    w.set_screenshots(paths)
    assert w._list.count() == 3  # noqa: SLF001


def test_click_thumb_switches_to_full_view(qt_app, tmp_path):
    """Clicking a thumbnail flips the QStackedWidget to the full-view page."""
    paths = []
    for i in range(3):
        p = tmp_path / f"{i+1:04d}-img.png"
        _make_png(p)
        paths.append(p)
    w = SlidesWidget()
    w.set_screenshots(paths)
    # Start on grid (index 0).
    assert w._stack.currentIndex() == 0  # noqa: SLF001
    w._show_full_for(1)  # noqa: SLF001
    assert w._stack.currentIndex() == 1  # noqa: SLF001
    # Caption renders position.
    assert "2 of 3" in w._caption.text()  # noqa: SLF001


def test_next_prev_navigation(qt_app, tmp_path):
    paths = []
    for i in range(3):
        p = tmp_path / f"{i+1:04d}-img.png"
        _make_png(p)
        paths.append(p)
    w = SlidesWidget()
    w.set_screenshots(paths)
    w._show_full_for(0)  # noqa: SLF001
    assert not w._prev_btn.isEnabled()  # noqa: SLF001 -- can't go before first
    assert w._next_btn.isEnabled()  # noqa: SLF001
    w._on_next_clicked()  # noqa: SLF001
    assert "2 of 3" in w._caption.text()  # noqa: SLF001
    w._on_next_clicked()  # noqa: SLF001
    assert "3 of 3" in w._caption.text()  # noqa: SLF001
    assert not w._next_btn.isEnabled()  # noqa: SLF001 -- last image; no further
    w._on_prev_clicked()  # noqa: SLF001
    assert "2 of 3" in w._caption.text()  # noqa: SLF001


def test_back_returns_to_grid(qt_app, tmp_path):
    paths = [tmp_path / "0001-img.png"]
    _make_png(paths[0])
    w = SlidesWidget()
    w.set_screenshots(paths)
    w._show_full_for(0)  # noqa: SLF001
    assert w._stack.currentIndex() == 1  # noqa: SLF001
    w._on_back_clicked()  # noqa: SLF001
    assert w._stack.currentIndex() == 0  # noqa: SLF001


def test_delete_confirm_yes_emits(qt_app, tmp_path, monkeypatch):
    p = tmp_path / "0001-img.png"
    _make_png(p)
    w = SlidesWidget()
    w.set_screenshots([p])
    fires: list[list[Path]] = []
    w.delete_requested.connect(fires.append)
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *_a, **_k: QMessageBox.StandardButton.Yes,
    )
    w._confirm_delete(p)  # noqa: SLF001
    # #110: signal now carries a list. Single-image callers wrap.
    assert fires == [[p]]


def test_delete_confirm_no_does_not_emit(qt_app, tmp_path, monkeypatch):
    p = tmp_path / "0001-img.png"
    _make_png(p)
    w = SlidesWidget()
    w.set_screenshots([p])
    fires: list[list[Path]] = []
    w.delete_requested.connect(fires.append)
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *_a, **_k: QMessageBox.StandardButton.No,
    )
    w._confirm_delete(p)  # noqa: SLF001
    assert fires == []


# ---- #110: multi-select delete --------------------------------------------


def test_delete_many_emits_list_on_yes(qt_app, tmp_path, monkeypatch):
    """Confirm + Yes on a multi-path delete emits one signal carrying
    every path."""
    paths = []
    for i in range(3):
        p = tmp_path / f"{i:04d}-img.png"
        _make_png(p)
        paths.append(p)
    w = SlidesWidget()
    w.set_screenshots(paths)
    fires: list[list[Path]] = []
    w.delete_requested.connect(fires.append)
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *_a, **_k: QMessageBox.StandardButton.Yes,
    )
    w._confirm_delete_many(paths)  # noqa: SLF001
    assert fires == [paths]


def test_delete_many_no_op_on_empty(qt_app, tmp_path):
    w = SlidesWidget()
    fires: list[list[Path]] = []
    w.delete_requested.connect(fires.append)
    w._confirm_delete_many([])  # noqa: SLF001
    assert fires == []


def test_delete_many_pluralized_prompt(qt_app, tmp_path, monkeypatch):
    """The confirm dialog text reflects the count + first few names."""
    paths = []
    for i in range(3):
        p = tmp_path / f"{i:04d}-img.png"
        _make_png(p)
        paths.append(p)
    w = SlidesWidget()
    w.set_screenshots(paths)
    captured: dict = {}

    def fake_question(_self, title, text, *_a, **_k):
        captured["title"] = title
        captured["text"] = text
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", fake_question)
    w._confirm_delete_many(paths)  # noqa: SLF001
    assert captured["title"] == "Delete screenshots"
    assert "3 screenshots" in captured["text"]
    for p in paths:
        assert p.name in captured["text"]


def test_delete_many_truncates_long_preview(qt_app, tmp_path, monkeypatch):
    paths = []
    for i in range(10):
        p = tmp_path / f"{i:04d}-img.png"
        _make_png(p)
        paths.append(p)
    w = SlidesWidget()
    w.set_screenshots(paths)
    captured: dict = {}

    def fake_question(_self, title, text, *_a, **_k):
        captured["text"] = text
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", fake_question)
    w._confirm_delete_many(paths)  # noqa: SLF001
    assert "and 4 more" in captured["text"]


def test_list_uses_extended_selection_mode(qt_app):
    """ExtendedSelection enables Ctrl/Shift-click multi-select."""
    from PyQt6.QtWidgets import QListWidget  # noqa: PLC0415
    w = SlidesWidget()
    assert w._list.selectionMode() == QListWidget.SelectionMode.ExtendedSelection  # noqa: SLF001


def test_delete_shortcut_no_op_on_empty_selection(qt_app, tmp_path):
    p = tmp_path / "0001-img.png"
    _make_png(p)
    w = SlidesWidget()
    w.set_screenshots([p])
    # Default state: nothing selected. Shortcut should not fire the
    # delete confirm at all (otherwise a stray Delete press would
    # show a dialog with an empty preview).
    fires: list[list[Path]] = []
    w.delete_requested.connect(fires.append)
    w._on_delete_shortcut()  # noqa: SLF001
    assert fires == []


def test_delete_shortcut_fires_for_selection(qt_app, tmp_path, monkeypatch):
    paths = []
    for i in range(2):
        p = tmp_path / f"{i:04d}-img.png"
        _make_png(p)
        paths.append(p)
    w = SlidesWidget()
    w.set_screenshots(paths)
    # Select both items.
    for i in range(w._list.count()):  # noqa: SLF001
        w._list.item(i).setSelected(True)  # noqa: SLF001
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *_a, **_k: QMessageBox.StandardButton.Yes,
    )
    fires: list[list[Path]] = []
    w.delete_requested.connect(fires.append)
    w._on_delete_shortcut()  # noqa: SLF001
    assert len(fires) == 1
    assert sorted(fires[0]) == sorted(paths)


def test_repopulate_after_delete_clamps_index_in_full_view(qt_app, tmp_path):
    """Deleting an image while the user is in full view nudges the
    current index into bounds without dropping them back to grid."""
    paths = []
    for i in range(3):
        p = tmp_path / f"{i+1:04d}-img.png"
        _make_png(p)
        paths.append(p)
    w = SlidesWidget()
    w.set_screenshots(paths)
    w._show_full_for(2)  # noqa: SLF001 -- last image
    assert w._current_index == 2  # noqa: SLF001
    # Now simulate deletion of the third image and a refresh with only
    # the first two paths.
    w.set_screenshots(paths[:2])
    # Still in full view; current_index pulled back to the new last.
    assert w._stack.currentIndex() == 1  # noqa: SLF001
    assert w._current_index == 1  # noqa: SLF001
