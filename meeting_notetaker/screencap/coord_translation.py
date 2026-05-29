"""Translate between Qt logical pixels and physical (device) pixels.

Background (issue #49): Qt 6's per-monitor DPI awareness v2 on
Windows reports monitor **positions** in physical pixels but
monitor **sizes** in logical pixels (scaled by the per-monitor DPR).
For a widget that spans monitors of mixed DPI -- e.g., the screen-
capture region picker -- Qt picks a single DPR for the widget and
scales widget-local mouse events through that DPR even when the
cursor is physically on a monitor with a different DPR. The result
is that the widget-local coordinate Qt reports doesn't match the
physical pixel under the cursor.

mss reads the screen in **physical** pixels. If we pass Qt's
mis-scaled coordinates to mss, we capture from a different region
than the user drew. Aaron's 2026-05-27 test reproduced this:
drawing on a 100%-scale monitor produced a capture on a 125%-scale
*sibling* monitor; drawing on the 125% monitor produced a capture
shifted vertically by ~1 inch (147 physical pixels) from the
drawn rectangle.

This module sidesteps Qt's broken multi-DPI math by calling
Win32 ``GetCursorPos`` directly. In a DPI-aware-v2 process
(PyQt6's default), ``GetCursorPos`` returns the cursor's **true
physical-pixel coordinates** regardless of which monitor the
cursor is on. We use that as the source of truth for selection
endpoints and translate back to Qt logical coordinates only at
the boundary where Qt needs them (``ArmedRegionOverlay.setGeometry``).

On non-Windows platforms ``get_cursor_physical_pos`` returns None
and callers fall back to Qt event coordinates -- multi-DPI is
predominantly a Windows pain point and Linux/macOS dev paths run
on simpler configurations.
"""
from __future__ import annotations

import logging
import sys
from typing import Optional

from PyQt6.QtCore import QPoint, QRect
from PyQt6.QtGui import QGuiApplication


log = logging.getLogger(__name__)


def get_cursor_physical_pos() -> Optional[QPoint]:
    """Return the cursor's current physical-pixel position.

    Windows-only. Uses Win32 GetCursorPos via ctypes; returns None
    on every other platform + on any failure path so the caller's
    fallback to Qt event coordinates is the only thing tested on
    Linux / macOS.

    In a DPI-aware-v2 process the returned coordinates are physical
    pixels of the virtual desktop -- same coordinate system mss
    sees. Top-left of the leftmost physical pixel is the leftmost
    monitor's physical top-left.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        pt = wintypes.POINT()
        ok = ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        if not ok:
            log.warning("GetCursorPos returned 0 (last_error=%d)",
                        ctypes.windll.kernel32.GetLastError())
            return None
        return QPoint(pt.x, pt.y)
    except Exception:
        log.exception("get_cursor_physical_pos failed")
        return None


def physical_rect_to_qt_logical_rect(physical_rect: QRect) -> QRect:
    """Map a physical-pixel rect to its Qt logical-pixel equivalent.

    Used to position widgets (``ArmedRegionOverlay``) at a known
    physical screen location. Qt's ``setGeometry`` works in logical
    coordinates; we convert by finding the Qt screen whose physical
    bounding box contains the rect's top-left, then scaling the
    offset within that screen by 1/DPR.

    Within a single monitor:
        qt_logical_pt = qt_top_left + (physical_pt - qt_top_left) / DPR

    Qt and mss/Win32 agree on monitor TOP-LEFT positions (both
    report them in the same coordinate system), so the conversion
    only needs to scale the offset.

    Edge case -- a rect that spans monitors of different DPRs: we
    use the DPR of the screen at the rect's top-left for the whole
    conversion. The overlay may be slightly mispositioned on the
    cross-monitor portion, which is an acceptable limitation given
    that mixed-DPI cross-monitor captures are an uncommon case and
    mss's capture itself spans physical pixels correctly.
    """
    screen = _qt_screen_at_physical(physical_rect.topLeft())
    if screen is None:
        # Find nearest screen by Manhattan distance from the rect's
        # center to each screen's center. Better than returning the
        # input unchanged when the rect's top-left is in a physical
        # gap -- nearest-screen-edge is usually right.
        screens = QGuiApplication.screens()
        if not screens:
            return physical_rect
        center = physical_rect.center()
        screen = min(
            screens,
            key=lambda s: (
                abs(s.geometry().center().x() - center.x())
                + abs(s.geometry().center().y() - center.y())
            ),
        )
    qt_geo = screen.geometry()
    dpr = screen.devicePixelRatio()
    if dpr == 1.0:
        return physical_rect
    qt_left = qt_geo.x() + int((physical_rect.x() - qt_geo.x()) / dpr)
    qt_top = qt_geo.y() + int((physical_rect.y() - qt_geo.y()) / dpr)
    qt_w = max(1, int(physical_rect.width() / dpr))
    qt_h = max(1, int(physical_rect.height() / dpr))
    return QRect(qt_left, qt_top, qt_w, qt_h)


def _qt_screen_at_physical(physical_pt: QPoint):
    """Find the Qt screen whose physical-pixel box contains the point.

    Qt's ``QGuiApplication.screenAt`` does the lookup in Qt logical
    coordinates, which is wrong here because we're searching with
    a physical coord. We instead iterate all screens and build each
    one's physical bounding box from its Qt geo + DPR.
    """
    for s in QGuiApplication.screens():
        geo = s.geometry()
        dpr = s.devicePixelRatio()
        phys_box = QRect(
            geo.x(), geo.y(),
            max(1, int(geo.width() * dpr)),
            max(1, int(geo.height() * dpr)),
        )
        if phys_box.contains(physical_pt):
            return s
    return None
