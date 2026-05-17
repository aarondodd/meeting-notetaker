"""Help > View Log dialog.

Non-modal QDialog that tails meeting_notetaker.log. A QFileSystemWatcher
fires on every write the OS reports; the dialog reads new bytes from the
last known offset and appends them to a monospace QPlainTextEdit. The
viewer is intentionally non-modal so the main window stays usable while
the log is open.

Auto-scroll stays glued to the bottom unless the user scrolls up; this
mirrors a typical tail-f experience and avoids fighting the user when
they want to read older entries.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QFileSystemWatcher, Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


# Reading the entire log on open would be expensive for a long-running
# install; this cap is a sane compromise for "show me what's happening".
_INITIAL_READ_BYTES = 256 * 1024


class LogViewerDialog(QDialog):
    """Tails a single log file. Safe to leave open during a recording."""

    def __init__(
        self, log_file: Path, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self._log_file = Path(log_file)
        self.setWindowTitle(f"Log -- {self._log_file.name}")
        # Treat this as a regular top-level window so it can be moved
        # independently of the main window.
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setModal(False)
        self.resize(900, 520)

        self._offset = 0
        self._auto_scroll = True

        layout = QVBoxLayout(self)

        path_row = QHBoxLayout()
        self._path_label = QLabel(str(self._log_file), self)
        self._path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        path_row.addWidget(self._path_label, 1)
        self._follow_check = QCheckBox("Follow", self)
        self._follow_check.setChecked(True)
        self._follow_check.setToolTip(
            "When on, the view scrolls to the latest line on every update. "
            "Turn off to read older entries without the view jumping."
        )
        self._follow_check.toggled.connect(self._on_follow_toggled)
        path_row.addWidget(self._follow_check)
        layout.addLayout(path_row)

        self._view = QPlainTextEdit(self)
        self._view.setReadOnly(True)
        self._view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPointSize(9)
        self._view.setFont(mono)
        layout.addWidget(self._view, 1)

        button_row = QHBoxLayout()
        self._clear_btn = QPushButton("Clear view", self)
        self._clear_btn.setToolTip(
            "Clear the on-screen buffer (the log file on disk is untouched). "
            "Future writes will still appear."
        )
        self._clear_btn.clicked.connect(self._clear_view)
        button_row.addWidget(self._clear_btn)
        self._reload_btn = QPushButton("Reload from disk", self)
        self._reload_btn.clicked.connect(self._reload)
        button_row.addWidget(self._reload_btn)
        button_row.addStretch(1)
        self._close_btn = QPushButton("Close", self)
        self._close_btn.clicked.connect(self.close)
        button_row.addWidget(self._close_btn)
        layout.addLayout(button_row)

        # File watcher; falls back to a 1s timer poll if the watcher's path
        # set is empty (e.g. log file doesn't exist yet on a fresh install).
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(lambda _p: self._read_new_bytes())
        self._poll = QTimer(self)
        self._poll.setInterval(1000)
        self._poll.timeout.connect(self._read_new_bytes)
        self._poll.start()

        if self._log_file.exists():
            self._watcher.addPath(str(self._log_file))
            self._initial_read()
        else:
            self._view.setPlainText(
                f"(log file does not exist yet at {self._log_file})\n"
                "It will be created the first time a log line is written."
            )

    # ---- internals ---------------------------------------------------------

    def _initial_read(self) -> None:
        """Seed the view with the last _INITIAL_READ_BYTES of the file."""
        try:
            size = self._log_file.stat().st_size
            start = max(0, size - _INITIAL_READ_BYTES)
            with self._log_file.open("rb") as f:
                f.seek(start)
                data = f.read()
            text = data.decode("utf-8", errors="replace")
            if start > 0:
                text = f"... (truncated {start} earlier bytes) ...\n" + text
            self._view.setPlainText(text)
            self._offset = size
            self._scroll_to_bottom_if_following()
        except OSError as exc:
            self._view.setPlainText(f"(failed to read log: {exc})")

    def _read_new_bytes(self) -> None:
        """Append anything written to the log file since the last read."""
        try:
            if not self._log_file.exists():
                return
            size = self._log_file.stat().st_size
            if size < self._offset:
                # File was rotated or truncated; restart from the beginning.
                self._offset = 0
                self._view.clear()
            if size == self._offset:
                return
            with self._log_file.open("rb") as f:
                f.seek(self._offset)
                data = f.read()
            self._offset = size
            text = data.decode("utf-8", errors="replace")
            self._view.moveCursor(self._view.textCursor().MoveOperation.End)
            self._view.insertPlainText(text)
            self._scroll_to_bottom_if_following()
            # Some editors/loggers replace the file in a way that breaks the
            # watcher; re-arm if necessary.
            if str(self._log_file) not in self._watcher.files():
                self._watcher.addPath(str(self._log_file))
        except OSError:
            # Transient read failures are fine; the timer will retry.
            pass

    def _reload(self) -> None:
        self._offset = 0
        self._view.clear()
        if self._log_file.exists():
            self._initial_read()

    def _clear_view(self) -> None:
        self._view.clear()

    def _on_follow_toggled(self, checked: bool) -> None:
        self._auto_scroll = checked
        if checked:
            self._scroll_to_bottom_if_following()

    def _scroll_to_bottom_if_following(self) -> None:
        if self._auto_scroll:
            sb = self._view.verticalScrollBar()
            if sb is not None:
                sb.setValue(sb.maximum())
