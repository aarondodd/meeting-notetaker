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
from PyQt6.QtWidgets import (
    QCheckBox, QLabel, QPushButton, QVBoxLayout, QWidget,
)


class ScreencapSidebar(QWidget):
    """Capture / Insert buttons + Auto-capture toggle + help label."""

    capture_clicked = pyqtSignal()
    insert_clicked = pyqtSignal()
    auto_capture_toggled = pyqtSignal(bool)

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

        # Auto-capture toggle. Cadence + dedup threshold live in
        # main Settings; this is the per-session on/off.
        self._auto_checkbox = QCheckBox("Auto-capture", self)
        self._auto_checkbox.setToolTip(
            "Snapshot the region every N seconds (set in Settings). "
            "Each new capture is compared against the most-recently-"
            "kept image; near-duplicates are discarded so only "
            "meaningfully-different content sticks around. Manual "
            "Capture / Insert clicks always keep their image."
        )
        self._auto_checkbox.toggled.connect(self.auto_capture_toggled.emit)
        layout.addWidget(self._auto_checkbox)
        self._auto_interval_label = QLabel("", self)
        self._auto_interval_label.setStyleSheet("color: gray; font-size: 10px;")
        layout.addWidget(self._auto_interval_label)

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
        self.setFixedWidth(160)
        self.set_armed(False)

    def set_armed(self, armed: bool) -> None:
        """Enable/disable the buttons. Help label toggles too.

        Disarm also forces the auto-capture checkbox off so the
        timer in MainApp tears down cleanly.
        """
        self._capture_btn.setEnabled(armed)
        self._insert_btn.setEnabled(armed)
        self._auto_checkbox.setEnabled(armed)
        self._help.setVisible(not armed)
        if not armed and self._auto_checkbox.isChecked():
            # Suppress the toggled signal on programmatic uncheck;
            # the parent already handled disarm.
            self._auto_checkbox.blockSignals(True)
            self._auto_checkbox.setChecked(False)
            self._auto_checkbox.blockSignals(False)
        self._auto_interval_label.setVisible(armed)

    def set_auto_interval_seconds(self, seconds: int) -> None:
        """Update the helper text next to the auto-capture checkbox."""
        self._auto_interval_label.setText(
            f"every {seconds} s (Settings to change)"
        )

    def is_auto_capture_checked(self) -> bool:
        return self._auto_checkbox.isChecked()
