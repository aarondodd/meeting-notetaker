"""Dialog prompting for alt text + caption when an image is inserted.

Shown both for clipboard paste (image already in memory) and file insert
(image already copied to the session images dir). The dialog is purely
about metadata -- the actual save happens in the caller.

Markdown image syntax accepted by most renderers:

    ![alt text](images/foo.png "caption")

The `caption` becomes the HTML `title` attribute; in the Preview pane and
the Synthesis tab (QTextBrowser) it surfaces as the tooltip when the
mouse hovers the image. The `alt text` is what screen readers announce
and what shows when the image fails to load.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)


_PREVIEW_MAX = 320


class ImageMetadataDialog(QDialog):
    """Tiny modal: image preview + alt + caption fields."""

    def __init__(
        self,
        *,
        preview_image: Optional[QImage] = None,
        preview_path: Optional[Path] = None,
        default_alt: str = "",
        default_caption: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Image details")
        self.setModal(True)
        self.resize(420, 460)

        layout = QVBoxLayout(self)

        if preview_image is not None and not preview_image.isNull():
            pixmap = QPixmap.fromImage(preview_image)
        elif preview_path is not None and preview_path.exists():
            pixmap = QPixmap(str(preview_path))
        else:
            pixmap = QPixmap()

        if not pixmap.isNull():
            scaled = pixmap.scaled(
                _PREVIEW_MAX,
                _PREVIEW_MAX,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            preview_label = QLabel(self)
            preview_label.setPixmap(scaled)
            preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(preview_label)
        else:
            layout.addWidget(QLabel("(no preview available)", self))

        layout.addWidget(QLabel("Alt text (shown if the image can't load, read by screen readers):", self))
        self._alt_edit = QLineEdit(self)
        self._alt_edit.setText(default_alt)
        self._alt_edit.setPlaceholderText("e.g. Slide showing Q3 revenue breakdown")
        layout.addWidget(self._alt_edit)

        layout.addWidget(QLabel("Caption (optional -- shown as tooltip on hover):", self))
        self._caption_edit = QLineEdit(self)
        self._caption_edit.setText(default_caption)
        self._caption_edit.setPlaceholderText("e.g. From Bob's screenshare at 14:25")
        layout.addWidget(self._caption_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._alt_edit.setFocus()

    def alt_text(self) -> str:
        return self._alt_edit.text().strip()

    def caption(self) -> str:
        return self._caption_edit.text().strip()
