"""Tray + app icon generation -- pure QPainter, no external image files.

Mirrors bookmarker.utils.icon.generate_tray_icon. Imports PyQt6 at call time
so test environments without Qt can still import the module.
"""
from __future__ import annotations


STATE_COLORS = {
    "idle":       (140, 140, 140),
    "ready":      (60, 130, 240),
    "recording":  (220, 60, 60),
    "paused":     (240, 195, 60),
    "processing": (130, 130, 220),
    "error":      (180, 60, 60),
}


def make_state_icon(state: str, size: int = 16):
    from PyQt6.QtCore import Qt, QSize
    from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap

    color = STATE_COLORS.get(state, STATE_COLORS["idle"])
    pixmap = QPixmap(QSize(size, size))
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(*color))
    pad = max(1, size // 8)
    painter.drawEllipse(pad, pad, size - 2 * pad, size - 2 * pad)
    if state == "recording":
        painter.setBrush(QColor(255, 255, 255))
        inner = max(1, size // 6)
        cx, cy = size // 2, size // 2
        painter.drawEllipse(cx - inner, cy - inner, 2 * inner, 2 * inner)
    painter.end()
    return QIcon(pixmap)


def app_icon(size: int = 64):
    from PyQt6.QtCore import Qt, QSize
    from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap

    pixmap = QPixmap(QSize(size, size))
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setBrush(QColor(60, 130, 240))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(2, 2, size - 4, size - 4, 8, 8)
    painter.setBrush(QColor(255, 255, 255))
    cx, cy = size // 2, size // 2
    r = size // 4
    painter.drawEllipse(cx - r, cy - r, 2 * r, 2 * r)
    painter.setBrush(QColor(220, 60, 60))
    inner = max(1, size // 12)
    painter.drawEllipse(cx - inner, cy - inner, 2 * inner, 2 * inner)
    painter.end()
    return QIcon(pixmap)
