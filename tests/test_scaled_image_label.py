"""ScaledImageLabel: fit-to-pane resize behavior shared by Slides + playback."""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QColor, QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.ui.scaled_image_label import ScaledImageLabel  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _make_png(path, *, w=400, h=200):
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(40, 120, 200))
    img.save(str(path), "PNG")


def test_initial_state_is_empty(qt_app):
    label = ScaledImageLabel()
    assert not label.has_image()
    assert label.pixmap().isNull()


def test_set_image_path_loads_and_scales(qt_app, tmp_path):
    p = tmp_path / "img.png"
    _make_png(p, w=800, h=400)
    label = ScaledImageLabel()
    label.resize(200, 100)
    label.set_image_path(p)
    assert label.has_image()
    # Pixmap shouldn't exceed the widget bounds; it should be scaled
    # to fit the 200x100 box while preserving aspect ratio.
    pix = label.pixmap()
    assert not pix.isNull()
    assert pix.width() <= 200
    assert pix.height() <= 100


def test_clear_image_drops_source(qt_app, tmp_path):
    p = tmp_path / "img.png"
    _make_png(p)
    label = ScaledImageLabel()
    label.set_image_path(p)
    label.clear_image()
    assert not label.has_image()


def test_resize_rescales_pixmap(qt_app, tmp_path):
    """Growing the widget makes the rendered pixmap grow with it.

    Force-show + processEvents is needed before resize events
    propagate; offscreen Qt skips repaints on hidden widgets.
    """
    p = tmp_path / "img.png"
    _make_png(p, w=1200, h=600)
    label = ScaledImageLabel()
    label.show()
    label.resize(200, 100)
    qt_app.processEvents()
    label.set_image_path(p)
    qt_app.processEvents()
    small_w = label.pixmap().width()
    label.resize(800, 400)
    qt_app.processEvents()
    larger_w = label.pixmap().width()
    assert larger_w > small_w


def test_null_path_leaves_label_clean(qt_app, tmp_path):
    """An unreadable file path doesn't crash and the label reports
    no image."""
    label = ScaledImageLabel()
    label.set_image_path(tmp_path / "does-not-exist.png")
    assert not label.has_image()
