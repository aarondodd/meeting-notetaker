"""ArmedRegionOverlay: persistent outline shown while screen capture is armed.

The overlay covers the region the user drew, frameless and
click-through, so the capture rectangle is visible at a glance
without blocking interaction underneath.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QRect, Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.screencap.armed_overlay import ArmedRegionOverlay  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def test_overlay_geometry_wraps_the_region(qt_app):
    """The widget rect is slightly larger than the region to keep the
    stroke from being clipped at the edge."""
    region = QRect(100, 200, 640, 480)
    overlay = ArmedRegionOverlay(region)
    try:
        # The overlay extends a few pixels outside the region on each
        # side so the stroke isn't clipped.
        g = overlay.geometry()
        assert g.x() < region.x()
        assert g.y() < region.y()
        assert g.width() > region.width()
        assert g.height() > region.height()
        # But the inset_rect (where the stroke gets drawn) matches the
        # region's size.
        assert overlay._inset_rect.size() == region.size()  # noqa: SLF001
    finally:
        overlay.close()


def test_overlay_is_click_through(qt_app):
    """The transparent-for-mouse-events attribute is the load-bearing
    part of "click-through". Pin it so a future refactor doesn't
    accidentally make the overlay swallow clicks."""
    overlay = ArmedRegionOverlay(QRect(0, 0, 100, 100))
    try:
        assert overlay.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    finally:
        overlay.close()


def test_overlay_has_translucent_background(qt_app):
    """The frame outline is the only painted content; the inside of the
    rect must be transparent so the user can see what's being captured."""
    overlay = ArmedRegionOverlay(QRect(0, 0, 100, 100))
    try:
        assert overlay.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    finally:
        overlay.close()


def test_overlay_is_frameless_top_level_tool(qt_app):
    """Top-level Tool window with no title bar, stays on top."""
    overlay = ArmedRegionOverlay(QRect(0, 0, 100, 100))
    try:
        flags = overlay.windowFlags()
        assert flags & Qt.WindowType.FramelessWindowHint
        assert flags & Qt.WindowType.WindowStaysOnTopHint
        assert flags & Qt.WindowType.Tool
    finally:
        overlay.close()
