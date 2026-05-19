"""Voice enrollment dialog.

Captures a fixed-length sample of the user's voice, computes an
ECAPA-TDNN embedding, and persists it via
`diarization.user_voiceprint.save`. The embedding is the centerpiece
of speaker attribution: at session refinement time the refiner
matches each mic-channel voice turn against this embedding -- a match
attributes the segment to the user; a miss falls back to
cross-checking against system-audio clusters (catches microphone
bleed when the room hears the meeting through speakers).

Two worker threads keep the Qt event loop responsive:
- `_EnrollmentWorker`: runs the PyAudio synchronous capture loop.
- `_EmbedWorker`: runs `default_encoder.embed_pcm`, which on first
  call after a fresh install triggers a ~22 MB SpeechBrain model
  download. The encoder is usually pre-warmed by MainApp on boot, but
  if a user opens enrollment before the pre-warm finishes the dialog
  still handles it gracefully -- the worker drives the download and
  the dialog shows pulsing progress until the embedding lands.
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..audio.mic_capture import MicCaptureError, record_mic_pcm
from ..diarization import user_voiceprint
from ..diarization.embeddings import default_encoder


log = logging.getLogger(__name__)


DEFAULT_DURATION_SEC = 10.0

# A short, neutral public-domain passage. Long enough to fill ~10 seconds
# at a comfortable pace, neutral enough that it doesn't anchor to any one
# topic or set of acronyms.
READING_PASSAGE = (
    "The clock on the wall ticked steadily as the rain tapped against the "
    "window. She set her cup down, glanced at the notes spread across the "
    "table, and began to read them aloud, one line at a time, in a calm "
    "and even voice. Outside, the street was quiet."
)


class _EnrollmentWorker(QThread):
    progress = pyqtSignal(float)
    finished_ok = pyqtSignal(object, int)   # (pcm np.ndarray, sample_rate)
    failed = pyqtSignal(str)

    def __init__(
        self,
        *,
        duration_sec: float,
        device_name: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.duration_sec = duration_sec
        self.device_name = device_name
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            pcm, sr = record_mic_pcm(
                self.duration_sec,
                device_name=self.device_name,
                cancel_check=lambda: self._cancel,
                progress_cb=lambda f: self.progress.emit(f),
            )
        except MicCaptureError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # pragma: no cover - thread safety net
            log.exception("voice enrollment capture failed")
            self.failed.emit(str(exc))
            return
        self.finished_ok.emit(pcm, sr)


class _EmbedWorker(QThread):
    """Computes an embedding off the main thread.

    The first call after a fresh install triggers a ~22 MB SpeechBrain
    model download inside `default_encoder.embed_pcm`; running it on a
    worker thread keeps the dialog responsive while the network fetch
    completes.
    """

    finished_ok = pyqtSignal(object)  # np.ndarray embedding
    failed = pyqtSignal(str)

    def __init__(
        self,
        pcm: np.ndarray,
        sample_rate: int,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._pcm = pcm
        self._sample_rate = int(sample_rate)

    def run(self) -> None:
        try:
            embedding = default_encoder.embed_pcm(self._pcm, self._sample_rate)
        except Exception as exc:  # pragma: no cover - thread safety net
            log.exception("voice enrollment embedding failed")
            self.failed.emit(str(exc))
            return
        self.finished_ok.emit(embedding)


class VoiceEnrollmentDialog(QDialog):
    """Records ~10 seconds of the user's voice and stores the embedding.

    On Accept: the captured PCM has been embedded and saved. On Reject /
    Cancel: nothing persists. The dialog is its own state machine -- the
    Record button changes role (Record -> Recording... -> Done/Retry) as
    capture progresses.
    """

    def __init__(
        self,
        *,
        device_name: str = "",
        duration_sec: float = DEFAULT_DURATION_SEC,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Record Voice Sample")
        self.setModal(True)
        self.resize(540, 420)
        self._device_name = device_name
        self._duration_sec = duration_sec
        self._worker: Optional[_EnrollmentWorker] = None
        self._embed_worker: Optional[_EmbedWorker] = None
        self._captured_pcm: Optional[np.ndarray] = None
        self._captured_sr: Optional[int] = None
        self._saved = False

        layout = QVBoxLayout(self)

        intro = QLabel(
            f"This records a {int(duration_sec)}-second sample of your voice "
            "and stores it locally as a SpeechBrain ECAPA-TDNN embedding "
            "(no audio leaves this machine). The embedding is used to "
            "attribute microphone-channel speech to you when system-audio "
            "bleed makes another speaker sound like the microphone. You "
            "can re-record or clear the sample any time in Settings.",
            self,
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        prompt_label = QLabel("Please read aloud at your normal volume:", self)
        layout.addWidget(prompt_label)

        passage = QLabel(READING_PASSAGE, self)
        passage.setWordWrap(True)
        passage.setStyleSheet(
            "QLabel { background-color: palette(base); padding: 10px; "
            "border: 1px solid palette(mid); }"
        )
        layout.addWidget(passage)

        self._status_label = QLabel("Ready. Click Record to begin.", self)
        self._status_label.setStyleSheet("color: #888;")
        layout.addWidget(self._status_label)

        self._progress = QProgressBar(self)
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        layout.addWidget(self._progress)

        self._record_btn = QPushButton("Record", self)
        self._record_btn.clicked.connect(self._on_record_clicked)
        layout.addWidget(self._record_btn)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Save")
        self._buttons.accepted.connect(self._on_accept)
        self._buttons.rejected.connect(self._on_reject)
        layout.addWidget(self._buttons)

    # ---- button handlers ----------------------------------------------------

    def _on_record_clicked(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            # Cancel an in-progress recording.
            self._worker.cancel()
            return
        self._captured_pcm = None
        self._captured_sr = None
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self._record_btn.setText("Cancel recording")
        self._status_label.setText("Recording... read the passage above.")
        self._progress.setValue(0)
        self._worker = _EnrollmentWorker(
            duration_sec=self._duration_sec,
            device_name=self._device_name,
            parent=self,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_capture_ok)
        self._worker.failed.connect(self._on_capture_failed)
        self._worker.start()

    def _on_progress(self, fraction: float) -> None:
        self._progress.setValue(int(max(0.0, min(1.0, fraction)) * 100))

    def _on_capture_ok(self, pcm: np.ndarray, sample_rate: int) -> None:
        self._captured_pcm = pcm
        self._captured_sr = int(sample_rate)
        self._progress.setValue(100)
        self._record_btn.setText("Re-record")
        self._status_label.setText(
            f"Captured {pcm.size / sample_rate:.1f}s of audio. "
            "Click Save to compute and store the voiceprint."
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)

    def _on_capture_failed(self, message: str) -> None:
        self._captured_pcm = None
        self._captured_sr = None
        self._progress.setValue(0)
        self._record_btn.setText("Record")
        self._status_label.setText(f"Capture failed: {message}")
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)

    def _on_accept(self) -> None:
        if self._captured_pcm is None or self._captured_sr is None:
            return
        # Disable buttons + show indeterminate progress while the embed
        # worker runs. The encoder is usually pre-warmed by MainApp at
        # boot so this returns in well under a second; on a cold first
        # launch it includes the ~22 MB model download from HF Hub and
        # can take 5-30s on a corporate network.
        if default_encoder.is_loaded:
            self._status_label.setText("Computing voiceprint...")
        else:
            self._status_label.setText(
                "Loading speaker identification model (~22 MB, first run "
                "only), then computing voiceprint..."
            )
        # Indeterminate (-1 to -1 makes the bar animate as a busy spinner)
        # because SpeechBrain doesn't expose per-byte download progress.
        self._progress.setRange(0, 0)
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self._buttons.button(QDialogButtonBox.StandardButton.Cancel).setEnabled(False)
        self._record_btn.setEnabled(False)

        self._embed_worker = _EmbedWorker(
            self._captured_pcm, self._captured_sr, parent=self
        )
        self._embed_worker.finished_ok.connect(self._on_embed_ok)
        self._embed_worker.failed.connect(self._on_embed_failed)
        self._embed_worker.start()

    def _on_embed_ok(self, embedding: np.ndarray) -> None:
        self._progress.setRange(0, 100)
        self._progress.setValue(100)
        self._buttons.button(QDialogButtonBox.StandardButton.Cancel).setEnabled(True)
        self._record_btn.setEnabled(True)
        norm = float(np.linalg.norm(embedding))
        if norm < 1e-6:
            self._status_label.setText(
                "The clip was too short or too quiet to embed. Re-record."
            )
            self._captured_pcm = None
            self._captured_sr = None
            return
        try:
            user_voiceprint.save(embedding)
        except Exception as exc:
            log.exception("saving the voiceprint failed")
            self._status_label.setText(f"Failed to save: {exc}")
            self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)
            return
        self._saved = True
        self.accept()

    def _on_embed_failed(self, message: str) -> None:
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)
        self._buttons.button(QDialogButtonBox.StandardButton.Cancel).setEnabled(True)
        self._record_btn.setEnabled(True)
        self._status_label.setText(f"Failed to compute embedding: {message}")

    def _on_reject(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(2000)
        if self._embed_worker is not None and self._embed_worker.isRunning():
            # No clean cancel for an in-flight HF download / model load;
            # wait briefly for it to finish so we don't tear down the
            # parent widget while a worker is using it. Worst case the
            # user sees a small delay before the dialog closes.
            self._embed_worker.wait(2000)
        self.reject()

    # ---- public ------------------------------------------------------------

    @property
    def saved(self) -> bool:
        """True if the user completed enrollment and the voiceprint was written."""
        return self._saved
