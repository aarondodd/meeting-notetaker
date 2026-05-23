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
        self.setGeometry(virtual_rect)
        self._start: Optional[QPoint] = None
        self._end: Optional[QPoint] = None
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
            self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._start is not None:
            self._end = event.pos()
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._start is None:
            return
        self._end = event.pos()
        rect_widget = QRect(self._start, self._end).normalized()
        # Need at least a few pixels in each dimension; sub-pixel clicks
        # are accidental and should cancel rather than commit.
        if rect_widget.width() < 8 or rect_widget.height() < 8:
            self._result = None
        else:
            # Translate widget-local coords back to absolute screen coords.
            origin = self._virtual_desktop_rect().topLeft()
            rect_screen = QRect(
                rect_widget.topLeft() + origin,
                rect_widget.size(),
            )
            self._result = rect_screen
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
