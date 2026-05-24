"""Status-bar pills: colored dot + short text + tooltip.

Replaces the older single-QLabel "Mic: foo | System audio: bar | ..."
string with one widget per segment. Each segment is a painted dot (so
the visual is identical across Windows builds and DPI settings -- no
emoji-font dependency) followed by a short label. State is encoded in
the dot color:

    green  -- active / OK
    yellow -- warning / idle / needs attention
    red    -- error / unavailable
    gray   -- informational / disabled-but-shown

The verbose "watching" / "running, connected" prose lives in the
tooltip; hovering a segment surfaces it on demand without consuming
chrome at rest.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPainter, QPixmap
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QWidget


DOT_COLORS = {
    "green": QColor(34, 197, 94),
    "yellow": QColor(234, 179, 8),
    "red": QColor(239, 68, 68),
    "gray": QColor(156, 163, 175),
}


def dot_pixmap(color_name: str, size: int = 10) -> QPixmap:
    """Render a filled antialiased circle of the named color.

    Returned QPixmap has a transparent background so it composites
    cleanly into a QLabel sitting on the status bar's native
    background. Size is in logical pixels; the dot is painted with an
    inset of half a pixel so the anti-aliased edge doesn't get cropped
    by the QPixmap's bounding rectangle.
    """
    color = DOT_COLORS.get(color_name, DOT_COLORS["gray"])
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(color)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(0, 0, size - 1, size - 1)
    painter.end()
    return pix


@dataclass(frozen=True)
class SegmentState:
    """Drives one StatusSegment widget."""

    color: str = "gray"
    # Short label that always shows -- e.g. "Mic", "Cal", "Syn".
    short_label: str = ""
    # Optional payload after the short label -- e.g. device name (Mic)
    # or count (Spk). Empty string means "label only, no payload".
    payload: str = ""
    tooltip: str = ""
    visible: bool = True


class StatusSegment(QWidget):
    """One pill in the status bar: dot icon + short label + payload."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        self._dot = QLabel(self)
        self._text = QLabel(self)
        layout.addWidget(self._dot)
        layout.addWidget(self._text)

    def apply(self, state: SegmentState) -> None:
        if not state.visible:
            self.hide()
            return
        self.show()
        self._dot.setPixmap(dot_pixmap(state.color))
        if state.payload:
            self._text.setText(f"{state.short_label} {state.payload}")
        else:
            self._text.setText(state.short_label)
        self.setToolTip(state.tooltip or self._text.text())
