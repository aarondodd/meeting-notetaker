"""Upgrade-progress dialog.

Runs `utils.updater.upgrade` on a worker thread so the UI stays
responsive while the build script (which calls pyinstaller) churns. The
build can take several minutes on a fresh install; this dialog streams
each stdout line into a read-only text view so the user can see what's
happening rather than staring at a frozen window.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..utils.updater import upgrade


class _UpgradeWorker(QThread):
    """Runs the upgrade() call off the main thread."""

    progress = pyqtSignal(str, str)   # stage, message
    finished_with_result = pyqtSignal(bool, str)  # success, message

    def __init__(self, *, owner: str, repo: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._owner = owner
        self._repo = repo

    def run(self) -> None:  # noqa: D401 -- Qt API
        success, message = upgrade(
            owner=self._owner,
            repo=self._repo,
            progress_callback=lambda stage, msg: self.progress.emit(stage, msg),
        )
        self.finished_with_result.emit(success, message)


class UpgradeProgressDialog(QDialog):
    """Modal dialog: status line + streaming build output + Close button."""

    def __init__(
        self,
        *,
        owner: str,
        repo: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Upgrading Meeting Notetaker")
        self.setMinimumSize(640, 420)
        self.setModal(True)

        self._success = False
        self._message = ""

        layout = QVBoxLayout(self)

        self._status_label = QLabel("Starting upgrade...", self)
        self._status_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self._status_label)

        self._output_view = QTextEdit(self)
        self._output_view.setReadOnly(True)
        mono = QFont("Consolas", 9)
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self._output_view.setFont(mono)
        layout.addWidget(self._output_view, 1)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self._close_btn = QPushButton("Close", self)
        self._close_btn.setEnabled(False)
        self._close_btn.clicked.connect(self.accept)
        button_row.addWidget(self._close_btn)
        layout.addLayout(button_row)

        self._worker = _UpgradeWorker(owner=owner, repo=repo, parent=self)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_with_result.connect(self._on_finished)
        self._worker.start()

    def result_summary(self) -> tuple[bool, str]:
        return self._success, self._message

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._worker.isRunning():
            event.ignore()
        else:
            super().closeEvent(event)

    # ---- worker callbacks --------------------------------------------------

    def _on_progress(self, stage: str, message: str) -> None:
        if stage == "build_output":
            self._output_view.append(message)
            scrollbar = self._output_view.verticalScrollBar()
            if scrollbar is not None:
                scrollbar.setValue(scrollbar.maximum())
        else:
            self._status_label.setText(message)
            self._output_view.append(f"\n=== {message} ===\n")
        QApplication.processEvents()

    def _on_finished(self, success: bool, message: str) -> None:
        self._success = success
        self._message = message
        if success:
            self._status_label.setText("Upgrade complete.")
            self._status_label.setStyleSheet("font-weight: bold; color: #4caf50;")
            self._output_view.append(f"\n=== SUCCESS ===\n{message}")
        else:
            self._status_label.setText("Upgrade failed.")
            self._status_label.setStyleSheet("font-weight: bold; color: #e53935;")
            self._output_view.append(f"\n=== FAILED ===\n{message}")
        self._close_btn.setEnabled(True)
