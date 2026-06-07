"""ImportAudioDialog -- bring an external audio recording into a session.

User flow (#88):

  1. Click "Choose file...". File picker filters to the formats
     advertised in audio.import_audio.SUPPORTED_EXTENSIONS.
  2. Dialog peeks the file via `describe_source` (no decode) and
     surfaces the format / duration / size in the metadata panel.
  3. User picks the speaker treatment (combined / my-voice / others)
     and confirms.
  4. OK runs `decode_to_canonical_wav` with a modal progress bar.
     On success, returns ImportAudioResult to the caller.

Three speaker-treatment modes drive which slot in the session folder
the decoded WAV lands in:

  - 'Single combined recording (run diarization)' -> sys.wav, diarize ON
  - 'My own voice only'                           -> mic.wav, diarize OFF
  - 'Other people\\'s voices'                      -> sys.wav, diarize ON

The dialog never touches the session or the controller directly --
the parent (MainApp) constructs it, drives the OK path, and routes
the decoded WAV through controller.start_processing_imported_session.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..audio.import_audio import (
    SUPPORTED_EXTENSIONS,
    AudioImportError,
    AudioImportResult,
    decode_to_canonical_wav,
    describe_source,
    is_supported_extension,
)


# ---- speaker treatment options ------------------------------------------

# Tuple shape: (display_label, slot, run_diarization)
# slot is "mic" or "sys" -- maps to the WAV filename the decode writes.
SPEAKER_TREATMENT_COMBINED = (
    "Single combined recording (run diarization)", "sys", True,
)
SPEAKER_TREATMENT_MY_VOICE = (
    "My own voice only", "mic", False,
)
SPEAKER_TREATMENT_OTHERS = (
    "Other people's voices", "sys", True,
)

ALL_SPEAKER_TREATMENTS = (
    SPEAKER_TREATMENT_COMBINED,
    SPEAKER_TREATMENT_MY_VOICE,
    SPEAKER_TREATMENT_OTHERS,
)


# ---- result payload -----------------------------------------------------


@dataclass
class ImportAudioResult:
    """What the dialog hands back to MainApp on accept."""

    source_path: Path
    decoded_wav_path: Path
    slot: str                 # "mic" or "sys"
    run_diarization: bool
    duration_seconds: float


# ---- file filter --------------------------------------------------------

def build_audio_file_filter() -> str:
    """Qt file-dialog filter string for the audio picker.

    Hand-rolled because the user wants a single line listing the
    advertised extensions; QFileDialog autogenerates a less useful
    grouping otherwise.
    """
    exts = " ".join(f"*{e}" for e in SUPPORTED_EXTENSIONS)
    return f"Audio / video files ({exts});;All files (*)"


# ---- decoder worker thread ----------------------------------------------


class _DecodeWorker(QThread):
    """Run decode_to_canonical_wav off the UI thread.

    Emits progress_pct (0..100) as the decoder makes progress, and
    one of finished_ok / finished_error at completion.
    """

    progress_pct = pyqtSignal(int)
    finished_ok = pyqtSignal(object)        # AudioImportResult
    finished_error = pyqtSignal(str)

    def __init__(
        self,
        src: Path,
        dst: Path,
        *,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._src = src
        self._dst = dst
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:  # noqa: D401 - Qt entry point
        try:
            result = decode_to_canonical_wav(
                self._src, self._dst,
                progress=lambda f: self.progress_pct.emit(int(f * 100)),
                should_cancel=lambda: self._cancel,
            )
        except AudioImportError as exc:
            self.finished_error.emit(exc.reason)
        except Exception as exc:  # pragma: no cover - thread safety net
            self.finished_error.emit(str(exc))
        else:
            self.finished_ok.emit(result)


# ---- dialog -------------------------------------------------------------


class ImportAudioDialog(QDialog):
    """Modal dialog for choosing + decoding an audio file into a session.

    Caller pattern:

        dlg = ImportAudioDialog(session_audio_dir, parent=main_window)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            result = dlg.result_payload
            controller.start_processing_imported_session(
                session,
                mic_wav=result.decoded_wav_path if result.slot == "mic" else None,
                sys_wav=result.decoded_wav_path if result.slot == "sys" else None,
                run_diarization=result.run_diarization,
            )
    """

    def __init__(
        self,
        audio_dir: Path,
        *,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import Audio Recording")
        self.setMinimumWidth(540)
        self._audio_dir = Path(audio_dir)
        self._source_path: Optional[Path] = None
        self._decode_worker: Optional[_DecodeWorker] = None
        self.result_payload: Optional[ImportAudioResult] = None

        layout = QVBoxLayout(self)

        intro = QLabel(
            "Import a recording from another device or app and run the "
            "standard transcription + speaker labelling pipeline on it. "
            "WAV, MP3, M4A, OGG/Opus, FLAC, and MP4/MOV/WebM audio "
            "tracks are supported.",
            self,
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # ---- Source file picker ----
        src_row = QHBoxLayout()
        self._source_label = QLabel("(no file chosen)", self)
        self._source_label.setWordWrap(True)
        self._source_label.setMinimumWidth(320)
        choose_btn = QPushButton("Choose file...", self)
        choose_btn.clicked.connect(self._on_choose_file)
        src_row.addWidget(self._source_label, stretch=1)
        src_row.addWidget(choose_btn, stretch=0)
        layout.addLayout(src_row)

        # ---- Metadata panel ----
        self._meta_frame = QFrame(self)
        self._meta_frame.setFrameShape(QFrame.Shape.StyledPanel)
        meta_layout = QVBoxLayout(self._meta_frame)
        self._meta_label = QLabel(
            "<i>Choose a file to see its details.</i>",
            self._meta_frame,
        )
        self._meta_label.setWordWrap(True)
        meta_layout.addWidget(self._meta_label)
        layout.addWidget(self._meta_frame)

        # ---- Speaker treatment dropdown ----
        treatment_row = QHBoxLayout()
        treatment_row.addWidget(QLabel("Speaker treatment:", self))
        self._treatment_picker = QComboBox(self)
        for label, _, _ in ALL_SPEAKER_TREATMENTS:
            self._treatment_picker.addItem(label)
        self._treatment_picker.setToolTip(
            "Single combined recording: diarization labels every speaker.\n"
            "My own voice only: skip diarization; all segments are tagged "
            "with your name.\n"
            "Other people's voices: diarization runs but the user-voiceprint "
            "match step is skipped."
        )
        treatment_row.addWidget(self._treatment_picker, stretch=1)
        layout.addLayout(treatment_row)

        # ---- Progress bar (hidden until import starts) ----
        self._progress = QProgressBar(self)
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.hide()
        layout.addWidget(self._progress)
        self._status_label = QLabel("", self)
        self._status_label.setWordWrap(True)
        self._status_label.hide()
        layout.addWidget(self._status_label)

        # ---- Button row ----
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Import")
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self._buttons.accepted.connect(self._on_accept)
        self._buttons.rejected.connect(self._on_cancel)
        layout.addWidget(self._buttons)

    # ---- file picking ---------------------------------------------------

    def _on_choose_file(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Choose an audio recording",
            "",
            build_audio_file_filter(),
        )
        if not path_str:
            return
        path = Path(path_str)
        if not is_supported_extension(path):
            QMessageBox.warning(
                self,
                "Unsupported file type",
                f"{path.suffix or path.name} is not a recognized audio "
                "or video format. Supported: WAV, MP3, M4A, OGG/Opus, "
                "FLAC, MP4/MOV/WebM, AMR.",
            )
            return
        self._source_path = path
        self._source_label.setText(str(path))
        self._refresh_meta_panel()
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)

    def _refresh_meta_panel(self) -> None:
        if self._source_path is None:
            self._meta_label.setText(
                "<i>Choose a file to see its details.</i>"
            )
            return
        info = describe_source(self._source_path)
        if info.get("error") or not info:
            self._meta_label.setText(
                f"<i>Could not read metadata for {self._source_path.name}. "
                "The decode pass will still try to proceed.</i>"
            )
            return
        ext = self._source_path.suffix.lstrip(".").upper()
        size_mb = info.get("file_size_bytes", 0) / (1024 * 1024)
        dur = info.get("duration_seconds", 0.0)
        m, s = divmod(int(dur), 60)
        h, m = divmod(m, 60)
        dur_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
        sample_rate = info.get("sample_rate", 0)
        channels = info.get("channels", 0)
        ch_str = {1: "mono", 2: "stereo"}.get(channels, f"{channels} ch")
        self._meta_label.setText(
            f"<b>{ext}</b> &middot; {size_mb:.1f} MB &middot; {dur_str} "
            f"&middot; {sample_rate:,} Hz {ch_str}<br>"
            f"<i>Will be downsampled to 16 kHz mono int16 WAV (~"
            f"{int(dur * 16000 * 2 / 1024)} KB).</i>"
        )

    # ---- accept / decode path -------------------------------------------

    def _selected_treatment(self) -> tuple[str, str, bool]:
        idx = self._treatment_picker.currentIndex()
        return ALL_SPEAKER_TREATMENTS[idx]

    def _on_accept(self) -> None:
        if self._source_path is None:
            return
        _, slot, run_diarization = self._selected_treatment()
        dst = self._audio_dir / f"{slot}.wav"
        # Lock the input controls so the user can't change the picker
        # mid-decode (which would put the result file in the wrong slot).
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self._treatment_picker.setEnabled(False)
        self._progress.show()
        self._progress.setValue(0)
        self._status_label.setText("Decoding...")
        self._status_label.show()

        worker = _DecodeWorker(self._source_path, dst, parent=self)
        worker.progress_pct.connect(self._progress.setValue)
        worker.finished_ok.connect(
            lambda result, s=slot, d=run_diarization: self._on_decode_ok(result, s, d)
        )
        worker.finished_error.connect(self._on_decode_error)
        self._decode_worker = worker
        worker.start()

    def _on_decode_ok(
        self,
        result: AudioImportResult,
        slot: str,
        run_diarization: bool,
    ) -> None:
        self.result_payload = ImportAudioResult(
            source_path=result.src_path,
            decoded_wav_path=result.dst_path,
            slot=slot,
            run_diarization=run_diarization,
            duration_seconds=result.duration_seconds,
        )
        self._status_label.setText("Import complete.")
        self.accept()

    def _on_decode_error(self, reason: str) -> None:
        QMessageBox.critical(
            self,
            "Import failed",
            reason or "The audio decode failed for an unknown reason.",
        )
        self._status_label.setText("Import failed; choose a different file.")
        self._progress.hide()
        self._treatment_picker.setEnabled(True)
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            self._source_path is not None
        )

    def _on_cancel(self) -> None:
        worker = self._decode_worker
        if worker is not None and worker.isRunning():
            worker.cancel()
            worker.wait(2000)
        self.reject()
