"""Fit-to-pane image label, shared by Slides full-view and Transcript playback.

Plain QLabel renders a pixmap at native size, which makes a full-
resolution screenshot either spill out of its parent or render
with a huge margin. This subclass remembers the source pixmap and
rescales it to the widget's current size on every resize, keeping
aspect ratio and using a smooth transformation.

Pulled out of slides_widget.py in v0.6.5 so the Transcript pane's
playback top pane can reuse the same renderer.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QLabel, QSizePolicy, QWidget


class ScaledImageLabel(QLabel):
    """QLabel that keeps its pixmap fit-to-pane on resize."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._source: Optional[QPixmap] = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumHeight(200)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding,
        )

    def set_image_path(self, path: Path) -> None:
        pix = QPixmap(str(path))
        self._source = None if pix.isNull() else pix
        self._refresh()

    def clear_image(self) -> None:
        self._source = None
        self.clear()

    def has_image(self) -> bool:
        return self._source is not None

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._refresh()

    def _refresh(self) -> None:
        if self._source is None:
            self.clear()
            return
        scaled = self._source.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.setPixmap(scaled)
