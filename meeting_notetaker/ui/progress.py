"""Background-task progress dialog.

Pattern:

    run_with_progress(
        parent,
        title="Loading model",
        message="Loading small.en...",
        fn=model_manager.get_model,
        size="small.en",
        on_success=lambda model: ...,
        on_failure=lambda msg:   ...,
    )

The callable runs in a QThread. If its signature accepts a `progress`
keyword argument, the dialog passes a callable that updates the status
label (so the worker can report "Downloading model.bin..." mid-task).

The dialog is modal but app-modal (not window-modal) so users can't kick
off another action while a model load is in flight.
"""
from __future__ import annotations

import inspect
import logging
from typing import Any, Callable, Optional

from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


log = logging.getLogger(__name__)


class BackgroundTask(QThread):
    """QThread that runs `fn(*args, **kwargs)` and emits the result."""

    progress = pyqtSignal(str)
    finished_ok = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn: Callable[..., Any], *args, parent: Optional[QObject] = None, **kwargs) -> None:
        super().__init__(parent)
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def run(self) -> None:
        try:
            sig = inspect.signature(self._fn)
            if "progress" in sig.parameters and "progress" not in self._kwargs:
                self._kwargs = dict(self._kwargs, progress=self.progress.emit)
            result = self._fn(*self._args, **self._kwargs)
        except Exception as exc:
            log.exception("background task failed: %s", self._fn)
            self.failed.emit(str(exc))
            return
        self.finished_ok.emit(result)


class ProgressDialog(QDialog):
    """Modal indeterminate-progress dialog with an optional Cancel button.

    Cancel does NOT terminate the thread -- callers must check
    `was_cancelled` and decide what to do. Most long-running ops here
    (model download, batch transcription) are not interruptible mid-flight
    by design.
    """

    def __init__(
        self,
        title: str,
        message: str,
        parent: Optional[QWidget] = None,
        *,
        allow_cancel: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.resize(440, 140)

        layout = QVBoxLayout(self)
        self._label = QLabel(message, self)
        self._label.setWordWrap(True)
        layout.addWidget(self._label)
        self._bar = QProgressBar(self)
        self._bar.setRange(0, 0)  # indeterminate
        layout.addWidget(self._bar)
        self._cancel_button = QPushButton("Cancel", self)
        self._cancel_button.clicked.connect(self._on_cancel)
        self._cancel_button.setVisible(allow_cancel)
        layout.addWidget(self._cancel_button)

        self._was_cancelled = False

    def set_message(self, text: str) -> None:
        self._label.setText(text)

    @property
    def was_cancelled(self) -> bool:
        return self._was_cancelled

    def _on_cancel(self) -> None:
        self._was_cancelled = True
        self._cancel_button.setEnabled(False)
        self._label.setText(self._label.text() + "\n(cancelling...)")


def run_with_progress(
    parent: QWidget,
    *,
    title: str,
    message: str,
    fn: Callable[..., Any],
    args: tuple = (),
    kwargs: Optional[dict] = None,
    on_success: Optional[Callable[[Any], None]] = None,
    on_failure: Optional[Callable[[str], None]] = None,
    allow_cancel: bool = False,
) -> None:
    """Run `fn(*args, **kwargs)` in a background thread under a modal progress dialog.

    on_success / on_failure are invoked on the GUI thread (Qt signal-slot
    machinery handles the marshaling). If the dialog was cancelled before
    the task finished, on_failure is invoked with the message
    'cancelled'.
    """
    kwargs = dict(kwargs or {})
    dialog = ProgressDialog(title, message, parent, allow_cancel=allow_cancel)
    task = BackgroundTask(fn, *args, parent=parent, **kwargs)

    task.progress.connect(dialog.set_message)

    def _handle_success(result):
        # Closing the dialog here unblocks dialog.exec() so the function returns.
        dialog.accept()
        if dialog.was_cancelled and on_failure is not None:
            on_failure("cancelled")
        elif on_success is not None:
            on_success(result)

    def _handle_failure(msg: str):
        dialog.reject()
        if on_failure is not None:
            on_failure(msg)

    task.finished_ok.connect(_handle_success)
    task.failed.connect(_handle_failure)
    task.start()
    dialog.exec()
    task.wait()
