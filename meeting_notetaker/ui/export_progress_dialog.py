"""Progress dialog for full-session export (issue #53, #60).

Pairs a determinate progress bar, a current-phase label, a Cancel
button, and a scrolling text log of every status message the
orchestrator emits.

Pattern:

    dialog = ExportProgressDialog(parent, "Building example.zip")
    worker.progress_changed.connect(dialog.set_progress)
    worker.status_changed.connect(dialog.append_status)
    worker.finished_with_result.connect(dialog.close_with_result)
    dialog.cancel_requested.connect(worker.cancel)
    dialog.show()

The Cancel button flips into a disabled "Cancelling..." state once
clicked; the dialog stays modal until the worker actually returns
and the orchestrator deletes any partial output.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class ExportProgressDialog(QDialog):
    cancel_requested = pyqtSignal()

    def __init__(
        self,
        parent: Optional[QWidget],
        header: str,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export full session")
        self.setModal(True)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.resize(520, 340)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self._header = QLabel(header, self)
        self._header.setWordWrap(True)
        font = self._header.font()
        font.setBold(True)
        self._header.setFont(font)
        layout.addWidget(self._header)

        self._status = QLabel("Starting...", self)
        self._status.setStyleSheet("color: #555;")
        layout.addWidget(self._status)

        self._bar = QProgressBar(self)
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        layout.addWidget(self._bar)

        self._log = QPlainTextEdit(self)
        self._log.setReadOnly(True)
        self._log.setPlaceholderText(
            "Export activity will appear here..."
        )
        # Monospace for stable alignment of status lines.
        font = self._log.font()
        font.setFamily("monospace")
        self._log.setFont(font)
        layout.addWidget(self._log, 1)

        # Cancel button row (#60). Distinct from the standard
        # QDialogButtonBox so we can intercept the click + flip the
        # button into a disabled "Cancelling..." state without
        # dismissing the dialog -- the worker may still be cleaning
        # up partial output for a few seconds after the request.
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self._cancel_btn = QPushButton("Cancel", self)
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)
        button_row.addWidget(self._cancel_btn)
        layout.addLayout(button_row)
        self._cancel_pending = False

    def set_progress(self, pct: int) -> None:
        # Clamp because some helpers emit -1 (no-op) or >100 if the
        # caller's math overshoots; the user shouldn't see that.
        pct = max(0, min(100, pct))
        self._bar.setValue(pct)

    def append_status(self, msg: str) -> None:
        """Append a status line to the log and update the current-
        phase label.

        Indented messages (leading whitespace) are taken as
        sub-phase details (e.g. "  Encoding audio") and only land in
        the log -- the top-line phase label keeps the orchestrator's
        announcement so the user sees the high-level step at a
        glance.
        """
        if not msg:
            return
        if not msg.lstrip().startswith("(") and not msg.startswith("  "):
            self._status.setText(msg)
        self._log.appendPlainText(msg)

    def close_with_result(self, err_msg: str) -> None:
        """Close the dialog. ``err_msg`` is appended to the log if
        present so the user has a record of what went wrong before
        the follow-up warning dialog appears."""
        if err_msg:
            self.append_status(f"Failed: {err_msg}")
        self.accept()

    def is_cancel_pending(self) -> bool:
        """True after the user clicked Cancel but before the worker
        has actually finished. MainApp consults this to decide
        whether to show the post-completion error dialog."""
        return self._cancel_pending

    def _on_cancel_clicked(self) -> None:
        if self._cancel_pending:
            return
        self._cancel_pending = True
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.setText("Cancelling...")
        self.append_status("Cancelling export...")
        self.cancel_requested.emit()

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        # Escape triggers the same cancel path as the button so the
        # keyboard shortcut works without an explicit close action.
        if event.key() == Qt.Key.Key_Escape:
            self._on_cancel_clicked()
            return
        super().keyPressEvent(event)
