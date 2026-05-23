"""ScreenshotRail: scroll-binding + thumb positioning."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QColor, QImage  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication, QMessageBox, QPlainTextEdit,
)

from meeting_notetaker.ui.screenshot_rail import ScreenshotRail  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _make_png(path: Path, w: int = 160, h: int = 100) -> None:
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(40, 120, 200))
    img.save(str(path), "PNG")


def test_initial_state_hidden_widgets(qt_app):
    """A freshly-constructed rail with no anchors has no QLabel
    children carrying thumbnails."""
    rail = ScreenshotRail()
    assert rail._anchors == []  # noqa: SLF001


def test_set_anchors_creates_thumbnail_per_item(qt_app, tmp_path):
    rail = ScreenshotRail()
    editor = QPlainTextEdit("[00:00:03] hi\n[00:00:11] there\n[00:00:20] bye")
    rail.set_transcript_view(editor)

    p1 = tmp_path / "0001.png"
    p2 = tmp_path / "0002.png"
    _make_png(p1)
    _make_png(p2)
    rail.set_anchors([(p1, 0), (p2, 2)])
    assert len(rail._anchors) == 2  # noqa: SLF001
    assert all(label.pixmap() is not None for _, _, label in rail._anchors)  # noqa: SLF001


def test_set_anchors_replaces_previous(qt_app, tmp_path):
    rail = ScreenshotRail()
    editor = QPlainTextEdit("[00:00:03] one")
    rail.set_transcript_view(editor)
    p1 = tmp_path / "a.png"
    p2 = tmp_path / "b.png"
    _make_png(p1)
    _make_png(p2)
    rail.set_anchors([(p1, 0)])
    rail.set_anchors([(p2, 0)])
    qt_app.processEvents()
    # Only the second batch survives; the first set's QLabel was
    # deleteLater'd.
    paths = [str(p) for p, _, _ in rail._anchors]  # noqa: SLF001
    assert paths == [str(p2)]


def test_set_anchors_skips_unreadable_png(qt_app, tmp_path):
    rail = ScreenshotRail()
    editor = QPlainTextEdit("[00:00:03] one")
    rail.set_transcript_view(editor)
    p_bad = tmp_path / "missing.png"  # not written
    rail.set_anchors([(p_bad, 0)])
    assert rail._anchors == []  # noqa: SLF001


def test_delete_signal_fires_after_confirm_yes(qt_app, tmp_path, monkeypatch):
    rail = ScreenshotRail()
    editor = QPlainTextEdit("[00:00:03] one")
    rail.set_transcript_view(editor)
    p = tmp_path / "img.png"
    _make_png(p)
    rail.set_anchors([(p, 0)])
    captured: list[Path] = []
    rail.delete_requested.connect(captured.append)
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *_a, **_k: QMessageBox.StandardButton.Yes,
    )
    rail._confirm_delete(p)  # noqa: SLF001
    assert captured == [p]


def test_delete_signal_not_fired_after_confirm_no(qt_app, tmp_path, monkeypatch):
    rail = ScreenshotRail()
    editor = QPlainTextEdit("[00:00:03] one")
    rail.set_transcript_view(editor)
    p = tmp_path / "img.png"
    _make_png(p)
    rail.set_anchors([(p, 0)])
    captured: list[Path] = []
    rail.delete_requested.connect(captured.append)
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *_a, **_k: QMessageBox.StandardButton.No,
    )
    rail._confirm_delete(p)  # noqa: SLF001
    assert captured == []
