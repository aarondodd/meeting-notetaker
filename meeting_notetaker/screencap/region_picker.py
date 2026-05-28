"""Fullscreen translucent overlay that lets the user drag-select a region.

Launched once when the user clicks Start Screen Capture. Covers the
whole virtual desktop (every monitor stitched into one logical
rectangle) so a region can span across monitors if the user wants.
Mouse press starts the rectangle, drag resizes it, release commits
it, Esc cancels. The exec() result returns the captured QRect in
absolute screen coordinates, or None on cancel.

The overlay is semi-transparent black with a "cut-out" of the drawn
rect rendered at full transparency so the user sees what they're
selecting underneath. The Qt FramelessWindowHint + transparent
background trick is standard for screen-selection tools.
"""
from __future__ import annotations

import logging
from typing import Optional

from PyQt6.QtCore import QPoint, QRect, Qt, pyqtSignal
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QGuiApplication,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
)
from PyQt6.QtWidgets import QApplication, QLabel, QWidget

from .coord_translation import get_cursor_physical_pos


log = logging.getLogger(__name__)


def _describe_screens() -> str:
    """One-line summary of every connected screen (issue #49 diagnostics).

    Includes Qt's reported geometry, its device pixel ratio (the
    Windows DPI scaling factor at this screen), and the physical-
    pixel equivalent so the next mis-aligned-capture report shows
    the mismatch directly in the log.
    """
    parts = []
    for i, s in enumerate(QGuiApplication.screens()):
        geo = s.geometry()
        dpr = s.devicePixelRatio()
        phys_w = int(geo.width() * dpr)
        phys_h = int(geo.height() * dpr)
        parts.append(
            f"screen[{i}]={s.name()!r} "
            f"qt_geo=({geo.x()},{geo.y()},{geo.width()}x{geo.height()}) "
            f"dpr={dpr:.3f} "
            f"physical={phys_w}x{phys_h}"
        )
    return " | ".join(parts) if parts else "no screens"


class RegionPicker(QWidget):
    """Drag a rectangle to pick a screen region. exec() returns a QRect or None.

    Uses a frameless, always-on-top, translucent fullscreen overlay
    spanning the virtual desktop. The overlay swallows mouse events
    so the rectangle the user draws doesn't accidentally click
    through to the underlying app.
    """

    region_selected = pyqtSignal(object)  # QRect or None

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        # Cover the virtual desktop (all monitors). primaryScreen.geometry()
        # is just one monitor; we need the union via screens() bounding box.
        virtual_rect = self._virtual_desktop_rect()
        # Diagnostic snapshot (issue #49). The next mis-aligned-region
        # report includes this line in the log so the screen layout +
        # per-monitor DPI scaling are visible from the get-go.
        log.info(
            "RegionPicker init: virtual_rect=(%d,%d,%dx%d) | %s",
            virtual_rect.x(), virtual_rect.y(),
            virtual_rect.width(), virtual_rect.height(),
            _describe_screens(),
        )
        self.setGeometry(virtual_rect)
        self._start: Optional[QPoint] = None
        self._end: Optional[QPoint] = None
        # Physical-pixel positions captured at press + release via
        # Win32 GetCursorPos. The widget-local Qt coords (above)
        # drive the on-screen rectangle preview; the physical
        # coords drive the actual capture so mixed-DPI multi-monitor
        # picks land on the correct pixels (issue #49).
        self._start_physical: Optional[QPoint] = None
        self._end_physical: Optional[QPoint] = None
        self._result: Optional[QRect] = None
        # Help label pinned to the top so the user knows what to do.
        # Layered as a child QLabel so it paints over the dim overlay
        # without us hand-rolling a text path in paintEvent.
        self._help = QLabel(
            "Drag to select a region for screen capture. Esc to cancel.",
            self,
        )
        self._help.setStyleSheet(
            "background-color: rgba(20, 20, 20, 220); color: white; "
            "padding: 6px 12px; border-radius: 6px; font-size: 13px;"
        )
        self._help.adjustSize()
        # Center the label horizontally across the virtual desktop,
        # near the top. Coordinates are widget-local; the virtual rect
        # starts at (0, 0) in widget space even though it may start
        # negative in absolute screen space.
        self._help.move(
            (self.width() - self._help.width()) // 2, 24,
        )

    @staticmethod
    def _virtual_desktop_rect() -> QRect:
        """Bounding box of every connected screen, in absolute coords."""
        screens = QGuiApplication.screens()
        if not screens:
            return QRect(0, 0, 1920, 1080)
        rect = screens[0].geometry()
        for s in screens[1:]:
            rect = rect.united(s.geometry())
        return rect

    # ------------------------------------------------------------------
    # Public API

    def exec(self) -> Optional[QRect]:  # type: ignore[override]
        """Show the picker modally and return the selected QRect or None."""
        self.show()
        self.activateWindow()
        self.raise_()
        # The Qt event loop is reentrant via local QEventLoop.
        from PyQt6.QtCore import QEventLoop  # noqa: PLC0415
        loop = QEventLoop()
        self.destroyed.connect(loop.quit)
        loop.exec()
        return self._result

    # ------------------------------------------------------------------
    # Event handlers

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._start = event.pos()
            self._end = event.pos()
            # Pin the physical cursor position at the moment of
            # press so a mixed-DPI multi-monitor pick captures the
            # right pixels regardless of which DPR Qt picked for
            # the widget. Win32-only; None on other platforms +
            # the fallback math in mouseReleaseEvent handles that.
            self._start_physical = get_cursor_physical_pos()
            self._end_physical = self._start_physical
            self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._start is not None:
            self._end = event.pos()
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._start is None:
            return
        self._end = event.pos()
        self._end_physical = get_cursor_physical_pos()
        rect_widget = QRect(self._start, self._end).normalized()
        # Need at least a few pixels in each dimension; sub-pixel clicks
        # are accidental and should cancel rather than commit.
        if rect_widget.width() < 8 or rect_widget.height() < 8:
            self._result = None
            log.info(
                "RegionPicker release: degenerate rect_widget=(%d,%d,%dx%d); cancelled",
                rect_widget.x(), rect_widget.y(),
                rect_widget.width(), rect_widget.height(),
            )
        else:
            # Prefer the physical-pixel rect from Win32 GetCursorPos
            # (issue #49 fix). Falls back to the Qt widget-coord
            # translation when GetCursorPos returns None -- e.g.,
            # Linux/macOS dev environments where the fallback is
            # correct because there's no mixed-DPI multi-monitor
            # math going wrong in the first place.
            if (
                self._start_physical is not None
                and self._end_physical is not None
            ):
                rect_screen = QRect(
                    self._start_physical, self._end_physical,
                ).normalized()
                coord_source = "physical (GetCursorPos)"
            else:
                origin = self._virtual_desktop_rect().topLeft()
                rect_screen = QRect(
                    rect_widget.topLeft() + origin,
                    rect_widget.size(),
                )
                coord_source = "Qt logical (fallback)"
            self._result = rect_screen
            # Diagnostic (issue #49). Logs both coord systems we
            # observed -- the widget-local rect, the GetCursorPos
            # physical positions when available, the final rect we
            # return, and which Qt screen contains its top-left.
            tl_screen = QGuiApplication.screenAt(rect_screen.topLeft())
            tl_dpr = tl_screen.devicePixelRatio() if tl_screen else None
            log.info(
                "RegionPicker release: rect_widget=(%d,%d,%dx%d) "
                "physical_start=%s physical_end=%s "
                "rect_screen=(%d,%d,%dx%d) source=%s "
                "tl_screen=%s tl_dpr=%s",
                rect_widget.x(), rect_widget.y(),
                rect_widget.width(), rect_widget.height(),
                f"({self._start_physical.x()},{self._start_physical.y()})"
                if self._start_physical else "n/a",
                f"({self._end_physical.x()},{self._end_physical.y()})"
                if self._end_physical else "n/a",
                rect_screen.x(), rect_screen.y(),
                rect_screen.width(), rect_screen.height(),
                coord_source,
                tl_screen.name() if tl_screen else "n/a",
                f"{tl_dpr:.3f}" if tl_dpr is not None else "n/a",
            )
        self.region_selected.emit(self._result)
        self.close()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self._result = None
            self.region_selected.emit(None)
            self.close()
        else:
            super().keyPressEvent(event)

    # ------------------------------------------------------------------
    # Painting

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Whole-screen dim. RGBA(0,0,0,90) lets the user see what
        # they're aiming at while making the chrome clear.
        painter.fillRect(self.rect(), QColor(0, 0, 0, 90))
        if self._start is not None and self._end is not None:
            sel = QRect(self._start, self._end).normalized()
            # Cut out the selection by overpainting with a transparent
            # rectangle (composition mode = Clear). The user sees
            # underneath, framed by the dim outside.
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            painter.fillRect(sel, QBrush(Qt.GlobalColor.transparent))
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            # Stroke the selection edge.
            pen = QPen(QColor(0, 200, 255), 2)
            painter.setPen(pen)
            painter.drawRect(sel)
            # Dimension readout near the cursor.
            painter.setPen(QPen(QColor(255, 255, 255), 1))
            painter.drawText(
                sel.bottomRight() + QPoint(8, -4),
                f"{sel.width()} x {sel.height()}",
            )
