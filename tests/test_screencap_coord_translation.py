"""Coord-translation helpers for the screen-capture pipeline (#49).

The screen-capture path now keeps the rectangle in physical pixels
end-to-end -- mss reads physical, and the picker captures physical
via Win32 GetCursorPos. The ArmedRegionOverlay still needs Qt
logical for setGeometry, hence physical_rect_to_qt_logical_rect.

The lookup helper _qt_screen_at_physical iterates Qt screens
building each one's physical bounding box from QScreen.geometry()
+ devicePixelRatio(). Aaron's 2026-05-27 log proves the math: Qt
and mss agree on monitor top-left positions but disagree on monitor
sizes for DPR > 1 monitors, so a coordinate inside one monitor in
physical space may fall in a "gap" in Qt logical space.

These tests pin the translation contract using monkey-patched
QGuiApplication.screens() so we can simulate Aaron's actual
4-monitor mixed-DPI setup without needing real hardware.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtCore")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, QRect  # noqa: E402
from PyQt6.QtGui import QGuiApplication  # noqa: E402

from meeting_notetaker.screencap import coord_translation  # noqa: E402
from meeting_notetaker.screencap.coord_translation import (  # noqa: E402
    physical_rect_to_qt_logical_rect,
    _qt_screen_at_physical,
)


@pytest.fixture(scope="module")
def qt_app():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication(sys.argv)


class _FakeScreen:
    """Lightweight stand-in for QScreen used by the translation helpers.

    Only `geometry()`, `devicePixelRatio()`, and `name()` are touched
    by the code under test, so we don't need the full QScreen surface.
    """
    def __init__(self, name: str, geo: QRect, dpr: float) -> None:
        self._name = name
        self._geo = geo
        self._dpr = dpr

    def name(self) -> str:
        return self._name

    def geometry(self) -> QRect:
        return self._geo

    def devicePixelRatio(self) -> float:
        return self._dpr


def _aaron_screens() -> list[_FakeScreen]:
    """Aaron's 2026-05-27 log layout.

    Four monitors: a 1920x1200 laptop at 100%, two 4K Sceptre Z27s
    at 125%, and a 4K Smart M70F at 100%. The Sceptres' Qt logical
    sizes (3072x1728) are physically 3840x2160 each.
    """
    return [
        _FakeScreen("DISPLAY1", QRect(0, 0, 1920, 1200), 1.0),
        _FakeScreen("Smart M70F", QRect(993, -2160, 3840, 2160), 1.0),
        _FakeScreen("Sceptre Z27 (1)", QRect(-2847, -2160, 3072, 1728), 1.25),
        _FakeScreen("Sceptre Z27 (2)", QRect(-2847, -4320, 3072, 1728), 1.25),
    ]


@pytest.fixture
def aaron_layout(monkeypatch, qt_app):
    """Patch QGuiApplication.screens() to return Aaron's exact layout."""
    screens = _aaron_screens()
    monkeypatch.setattr(QGuiApplication, "screens", staticmethod(lambda: screens))
    return screens


# ---- _qt_screen_at_physical -----------------------------------------------


def test_screen_at_physical_finds_dpr_one_monitor(aaron_layout):
    """Smart M70F at 100% -- physical box equals logical box. Point
    at (1500, -1000) is inside Smart M70F (993..4833, -2160..0)."""
    screen = _qt_screen_at_physical(QPoint(1500, -1000))
    assert screen is not None
    assert screen.name() == "Smart M70F"


def test_screen_at_physical_finds_dpr_125_monitor_inside_physical_range(aaron_layout):
    """Sceptre Z27 (1) physical box is (-2847, -2160, 3840x2160).
    Point (500, -1000) is in physical x [-2847, 993] and y [-2160, 0]
    -- inside that monitor's PHYSICAL box. The Qt logical box for
    this monitor is only 3072 wide, so logical x ends at 225;
    x=500 would be 'in a gap' to QGuiApplication.screenAt, which
    is exactly the bug this helper sidesteps."""
    screen = _qt_screen_at_physical(QPoint(500, -1000))
    assert screen is not None
    assert screen.name() == "Sceptre Z27 (1)"


def test_screen_at_physical_finds_sceptre_2_above(aaron_layout):
    """Sceptre Z27 (2) physical (-2847, -4320, 3840x2160). Point
    inside this monitor's physical range."""
    screen = _qt_screen_at_physical(QPoint(0, -3500))
    assert screen is not None
    assert screen.name() == "Sceptre Z27 (2)"


def test_screen_at_physical_returns_none_for_off_screen_point(aaron_layout):
    """A point outside all monitors returns None. Caller decides
    whether to fall back to nearest screen."""
    screen = _qt_screen_at_physical(QPoint(10_000, 10_000))
    assert screen is None


# ---- physical_rect_to_qt_logical_rect --------------------------------------


def test_logical_conversion_is_identity_for_dpr_one(aaron_layout):
    """At 100% scaling Qt logical equals physical. The conversion
    is a no-op."""
    physical = QRect(1200, -1000, 800, 600)  # inside Smart M70F (100%)
    logical = physical_rect_to_qt_logical_rect(physical)
    assert logical == physical


def test_logical_conversion_scales_dpr_125_inside_screen(aaron_layout):
    """Aaron's capture-1 case in reverse. Physical rect on
    Sceptre Z27 (2) at 125% DPR -- a 1188x808 physical region maps
    to ~950x646 logical pixels. Sceptre Z27 (2)'s top-left is at
    (-2847, -4320) in both coord spaces; offsets within the
    monitor divide by DPR.

    The helper uses int() truncation (Python's floor toward zero
    for positives, away from zero for negatives) so a 1-pixel
    rounding error on the offsets is normal. Cosmetic on the
    overlay; the value is pinned here so future refactors don't
    silently introduce a multi-pixel drift."""
    physical = QRect(-2453, -3584, 1188, 808)
    logical = physical_rect_to_qt_logical_rect(physical)
    # logical_x = -2847 + int((-2453 - (-2847)) / 1.25)
    #           = -2847 + int(315.2)
    #           = -2847 + 315 = -2532
    # logical_y = -4320 + int((-3584 - (-4320)) / 1.25)
    #           = -4320 + int(588.8)
    #           = -4320 + 588 = -3732
    # logical_w = int(1188 / 1.25) = int(950.4) = 950
    # logical_h = int(808 / 1.25) = int(646.4) = 646
    assert logical.x() == -2532
    assert logical.y() == -3732
    assert logical.width() == 950
    assert logical.height() == 646


def test_logical_conversion_uses_nearest_screen_for_off_screen_point(aaron_layout):
    """When the physical top-left is outside all monitors, the
    helper falls back to the nearest screen's DPR so the overlay
    is at least roughly positioned rather than way off."""
    # A point far above + left of any monitor. Nearest center is
    # Sceptre Z27 (2)'s center (-2847+1920=-927, -4320+1080=-3240).
    physical = QRect(-5000, -5000, 100, 100)
    logical = physical_rect_to_qt_logical_rect(physical)
    # Conversion happens; result is in logical space using the
    # nearest screen's DPR (Sceptre Z27 (2) DPR=1.25).
    assert logical.width() == 80  # 100/1.25
    assert logical.height() == 80


def test_logical_conversion_with_no_screens_returns_input(monkeypatch, qt_app):
    """Pathological case: zero screens. Helper returns the rect
    unchanged rather than crashing -- saves the caller a try."""
    monkeypatch.setattr(QGuiApplication, "screens", staticmethod(lambda: []))
    physical = QRect(100, 200, 50, 50)
    logical = physical_rect_to_qt_logical_rect(physical)
    assert logical == physical


# ---- get_cursor_physical_pos ----------------------------------------------


def test_cursor_physical_pos_is_none_on_non_windows():
    """The Win32 GetCursorPos path is gated behind sys.platform.
    On Linux/macOS dev environments the function returns None and
    callers fall back to Qt event coords -- which are fine on
    single-monitor or uniform-DPI setups, which is what Linux/macOS
    dev typically runs."""
    if sys.platform == "win32":
        pytest.skip("Windows runs the real GetCursorPos path")
    assert coord_translation.get_cursor_physical_pos() is None
