"""Frameless click-through overlay that outlines the armed capture region.

After RegionPicker accepts a rectangle, MainApp keeps the region in
memory but the user has no on-screen indication of *what* will be
captured. This widget draws a thin colored border around the region
that persists until screen capture is disarmed -- a glanceable
reminder of the active capture rectangle.

The overlay is frameless, always-on-top, transparent to mouse input
(WindowTransparentForInput on Windows / equivalent on other
platforms) so the user can keep working inside the rectangle while
it's armed. Hiding the widget tears the overlay down cleanly.
"""
from __future__ import annotations

import logging
from typing import Optional

from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QColor, QGuiApplication, QPainter, QPaintEvent, QPen
from PyQt6.QtWidgets import QWidget


log = logging.getLogger(__name__)


# Color + stroke width for the persistent outline. The same cyan the
# RegionPicker uses, but a touch thinner so it reads as "informational
# overlay" rather than "still in selection mode".
_OUTLINE_COLOR = QColor(0, 200, 255, 220)
_OUTLINE_WIDTH = 2


class ArmedRegionOverlay(QWidget):
    """Persistent outline around the currently-armed capture region.

    Construct with the QRect (in absolute screen coordinates) that the
    user selected via RegionPicker, then call show(). Call close() or
    deleteLater() to remove. The widget sets WA_TransparentForMouseEvents
    so clicks land through it as if it weren't there.
    """

    def __init__(self, region: QRect, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput,
        )
        # Click-through. WindowTransparentForInput is the X11 / window-
        # manager hint; the WidgetAttribute below covers the Qt event
        # layer too so the widget really doesn't intercept input on any
        # platform.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        # Geometry expands by the stroke width so the line itself isn't
        # clipped at the rectangle's edge.
        bleed = _OUTLINE_WIDTH + 1
        target = QRect(
            region.x() - bleed,
            region.y() - bleed,
            region.width() + 2 * bleed,
            region.height() + 2 * bleed,
        )
        # Diagnostic for the "overlay jumps to a different position"
        # report (issue #49). Logs the input region, the target
        # geometry we ask Qt for, and which screen the geometry's
        # top-left lands on (with that screen's DPR). After show(),
        # frameGeometry / geometry can drift from what we set,
        # particularly on mixed-DPI multi-monitor setups -- we log
        # again on show() below so any drift surfaces.
        tl_screen = QGuiApplication.screenAt(target.topLeft())
        tl_dpr = tl_screen.devicePixelRatio() if tl_screen else None
        log.info(
            "ArmedRegionOverlay init: input_region=(%d,%d,%dx%d) "
            "target=(%d,%d,%dx%d) screen=%s dpr=%s",
            region.x(), region.y(), region.width(), region.height(),
            target.x(), target.y(), target.width(), target.height(),
            tl_screen.name() if tl_screen else "n/a",
            f"{tl_dpr:.3f}" if tl_dpr is not None else "n/a",
        )
        self.setGeometry(target)
        self._inset_rect = QRect(
            bleed, bleed, region.width(), region.height(),
        )

    def showEvent(self, event) -> None:
        """Log the actual geometry Qt landed on after show.

        Issue #49: on a 100%-scale monitor with a 125%-scale sibling,
        the overlay's setGeometry input vs the painted position can
        diverge -- Qt re-interprets coordinates when the widget is
        first realized on a screen with a different DPR than the one
        it was conceptually positioned over. Capturing actual_geo
        here shows that drift directly in the log.
        """
        actual = self.geometry()
        frame = self.frameGeometry()
        log.info(
            "ArmedRegionOverlay shown: actual_geo=(%d,%d,%dx%d) "
            "frame_geo=(%d,%d,%dx%d) screen=%s",
            actual.x(), actual.y(), actual.width(), actual.height(),
            frame.x(), frame.y(), frame.width(), frame.height(),
            (self.screen().name() if self.screen() else "n/a"),
        )
        super().showEvent(event)

    # ------------------------------------------------------------------

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        # Frame-only: no fill so the user sees the region underneath
        # completely unmodified. Drawing slightly outside the rect's
        # inner edge keeps the line wholly inside the widget's clip.
        pen = QPen(_OUTLINE_COLOR, _OUTLINE_WIDTH)
        pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.GlobalColor.transparent)
        painter.drawRect(self._inset_rect)
