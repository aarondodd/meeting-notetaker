"""Per-session screen-capture sidebar for the My Notes pane.

Two buttons stacked vertically:

* **Capture** -- grab the armed region, save as a PNG into the session's
  screenshots/ dir. The image shows up in the Slides tab.
* **Insert** -- the same capture, plus a markdown image-ref inserted
  at the current cursor position in the My Notes editor so the
  screenshot lands inline next to the note the user just wrote.

Both buttons are disabled unless screen capture is armed (a region
has been drawn and the user hasn't clicked Stop Screen Capture yet).
SessionView drives the enabled state via set_armed().
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget


class ScreencapSidebar(QWidget):
    """Stack of Capture / Insert buttons + a 'not armed' help label."""

    capture_clicked = pyqtSignal()
    insert_clicked = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self._title = QLabel("Screen Capture", self)
        self._title.setStyleSheet("font-weight: 600;")
        layout.addWidget(self._title)

        self._capture_btn = QPushButton("Capture", self)
        self._capture_btn.setToolTip(
            "Snapshot the selected region and save it to the session's "
            "screenshots/ folder. Shows up in the Slides tab."
        )
        self._capture_btn.clicked.connect(self.capture_clicked.emit)
        layout.addWidget(self._capture_btn)

        self._insert_btn = QPushButton("Insert", self)
        self._insert_btn.setToolTip(
            "Snapshot the selected region, save it, and insert a "
            "markdown image-reference at the cursor in My Notes."
        )
        self._insert_btn.clicked.connect(self.insert_clicked.emit)
        layout.addWidget(self._insert_btn)

        self._help = QLabel(
            "Click Start Screen Capture above, then draw a region "
            "to enable these buttons.",
            self,
        )
        self._help.setWordWrap(True)
        self._help.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(self._help)

        # Fixed width keeps the sidebar from sliding the editor pane
        # around when armed/disarmed.
        self.setFixedWidth(150)
        self.set_armed(False)

    def set_armed(self, armed: bool) -> None:
        """Enable/disable the buttons. Help label toggles too."""
        self._capture_btn.setEnabled(armed)
        self._insert_btn.setEnabled(armed)
        self._help.setVisible(not armed)
