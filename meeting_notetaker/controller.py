"""Session lifecycle controller.

Owns:
  - SessionStore (persistent)
  - The currently-active session (record/transcribe target)
  - Recorder lifecycles (mic + loopback)
  - ChunkBuffer + live transcription workers
  - Batch transcription on stop (and the optional retain-audio cleanup)

This is the bridge between the UI signals and the audio/transcription
modules; the UI never touches recorders or model_manager directly.
"""
from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from .audio.chunk_buffer import ChunkBuffer
from .models.session import (
    STATE_COMPLETE,
    STATE_ERROR,
    STATE_PAUSED,
    STATE_PROCESSING,
    STATE_RECORDING,
    Session,
    SessionStore,
    utc_now_iso,
)
from .models.transcript import MIC, SYS, TranscriptSegment, TranscriptStore
from .transcription import model_manager
from .transcription.worker import LiveTranscriptionWorker, batch_transcribe, interleave
from .utils.config import Config
from .utils.paths import session_audio_dir


log = logging.getLogger(__name__)


class _BatchTranscribeThread(QThread):
    """Wraps batch_transcribe for the final-pass after stop()."""

    done = pyqtSignal(list)            # list[TranscriptSegment]
    progress = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(
        self,
        mic_wav: Optional[Path],
        sys_wav: Optional[Path],
        model_size: str,
        *,
        vad_filter: bool,
        vad_min_silence_ms: int,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.mic_wav = mic_wav
        self.sys_wav = sys_wav
        self.model_size = model_size
        self.vad_filter = vad_filter
        self.vad_min_silence_ms = vad_min_silence_ms

    def run(self) -> None:
        try:
            model = model_manager.get_model(self.model_size, progress=self.progress.emit)
            results: list[TranscriptSegment] = []
            if self.mic_wav and self.mic_wav.exists() and self.mic_wav.stat().st_size > 44:
                results += batch_transcribe(
                    self.mic_wav, model,
                    source=MIC,
                    vad_filter=self.vad_filter,
                    vad_min_silence_ms=self.vad_min_silence_ms,
                    progress=self.progress.emit,
                )
            if self.sys_wav and self.sys_wav.exists() and self.sys_wav.stat().st_size > 44:
                results += batch_transcribe(
                    self.sys_wav, model,
                    source=SYS,
                    vad_filter=self.vad_filter,
                    vad_min_silence_ms=self.vad_min_silence_ms,
                    progress=self.progress.emit,
                )
            self.done.emit(interleave(results, []))
        except Exception as exc:  # pragma: no cover - thread safety net
            log.exception("batch transcribe failed")
            self.failed.emit(str(exc))


class SessionController(QObject):
    state_changed = pyqtSignal(str, str)               # session_id, state
    segment_arrived = pyqtSignal(str, object)          # session_id, TranscriptSegment
    transcript_replaced = pyqtSignal(str, list)        # session_id, list[TranscriptSegment]
    error = pyqtSignal(str)
    status = pyqtSignal(str)

    def __init__(self, store: SessionStore, config: Config, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.store = store
        self.config = config
        self._active_session: Optional[Session] = None
        self._mic_recorder = None
        self._loopback_recorder = None
        self._chunk_buffer: Optional[ChunkBuffer] = None
        self._workers: list[LiveTranscriptionWorker] = []
        self._t_start_wall: Optional[float] = None
        self._live_segments: list[TranscriptSegment] = []
        self._mic_wav: Optional[Path] = None
        self._sys_wav: Optional[Path] = None
        self._batch_thread: Optional[_BatchTranscribeThread] = None

    @property
    def active_session(self) -> Optional[Session]:
        return self._active_session

    # ---- session lifecycle -------------------------------------------------

    def start_session(self, session: Session) -> None:
        if self._active_session is not None:
            self.error.emit("Another session is already active. Stop it before starting a new one.")
            return
        self._active_session = session
        self._live_segments = []
        try:
            audio_dir = session_audio_dir(session.id)
            self._mic_wav = audio_dir / "mic.wav"
            self._sys_wav = audio_dir / "sys.wav"
            self._chunk_buffer = ChunkBuffer(sources=[MIC, SYS])

            # Live transcription off if capture-only mode is on. The model is
            # expected to be already cached -- the app layer preloads it via a
            # progress dialog before calling us, so this get_model() is a
            # near-instant cache hit on the second-and-subsequent runs.
            if not self.config.transcription.capture_only_mode:
                model = model_manager.get_model(
                    self.config.transcription.model_size,
                    progress=self.status.emit,
                )
                for source in (MIC, SYS):
                    worker = LiveTranscriptionWorker(
                        self._chunk_buffer,
                        source=source,
                        model=model,
                        vad_filter=self.config.audio.vad_enabled,
                        vad_min_silence_ms=self.config.audio.vad_min_silence_ms,
                    )
                    worker.chunk_done.connect(self._on_live_segment)
                    worker.error.connect(self.error.emit)
                    worker.start()
                    self._workers.append(worker)

            # Mic recorder.
            from .audio.mic_recorder import MicRecorder
            self._mic_recorder = MicRecorder(self._chunk_buffer, self._mic_wav, source_name=MIC)
            self._mic_recorder.error.connect(self.error.emit)
            self._mic_recorder.start()

            # Loopback recorder (Windows only; fail soft if unavailable).
            from .audio.loopback_recorder import LoopbackRecorder, LoopbackUnavailable
            if LoopbackRecorder.is_available():
                self._loopback_recorder = LoopbackRecorder(
                    self._chunk_buffer, self._sys_wav, source_name=SYS
                )
                self._loopback_recorder.error.connect(self.error.emit)
                try:
                    self._loopback_recorder.start()
                except LoopbackUnavailable as exc:
                    log.warning("loopback unavailable: %s", exc)
                    self._loopback_recorder = None
                    self.status.emit("System-audio loopback unavailable; recording mic only.")
            else:
                self.status.emit("PyAudioWPatch not installed; recording mic only.")

            self._t_start_wall = time.monotonic()
            self.store.update_session(
                session.id,
                state=STATE_RECORDING,
                started_at=utc_now_iso(),
                model_used=self.config.transcription.model_size,
                has_audio=True,
            )
            session.state = STATE_RECORDING
            self.state_changed.emit(session.id, STATE_RECORDING)
        except Exception as exc:
            log.exception("start_session failed")
            # Stop anything that managed to start before we bailed.
            try:
                if self._mic_recorder is not None and self._mic_recorder.is_recording:
                    self._mic_recorder.stop()
            except Exception:
                log.exception("mic cleanup after partial-start failed")
            try:
                if self._loopback_recorder is not None and self._loopback_recorder.is_recording:
                    self._loopback_recorder.stop()
            except Exception:
                log.exception("loopback cleanup after partial-start failed")
            for worker in self._workers:
                try:
                    worker.stop(drain=False)
                except Exception:
                    log.exception("worker stop after partial-start failed")
            for worker in self._workers:
                try:
                    worker.wait(2000)
                except Exception:
                    pass
            self._workers = []
            self.store.update_session(session.id, state=STATE_ERROR)
            session.state = STATE_ERROR
            self.state_changed.emit(session.id, STATE_ERROR)
            self.error.emit(f"Failed to start recording: {exc}")
            self._teardown_recording(error=True)

    def pause_session(self) -> None:
        if self._active_session is None:
            return
        if self._mic_recorder is not None:
            self._mic_recorder.pause()
        if self._loopback_recorder is not None:
            self._loopback_recorder.pause()
        self.store.update_session(self._active_session.id, state=STATE_PAUSED)
        self._active_session.state = STATE_PAUSED
        self.state_changed.emit(self._active_session.id, STATE_PAUSED)

    def resume_session(self) -> None:
        if self._active_session is None:
            return
        if self._mic_recorder is not None:
            self._mic_recorder.resume()
        if self._loopback_recorder is not None:
            self._loopback_recorder.resume()
        self.store.update_session(self._active_session.id, state=STATE_RECORDING)
        self._active_session.state = STATE_RECORDING
        self.state_changed.emit(self._active_session.id, STATE_RECORDING)

    def stop_session(self) -> None:
        if self._active_session is None:
            return
        session = self._active_session
        duration = int((time.monotonic() - self._t_start_wall) if self._t_start_wall else 0)
        # Stop recorders first so WAV files close cleanly.
        try:
            if self._mic_recorder is not None:
                self._mic_recorder.stop()
        except Exception:
            log.exception("mic stop failed")
        try:
            if self._loopback_recorder is not None:
                self._loopback_recorder.stop()
        except Exception:
            log.exception("loopback stop failed")

        # Stop live workers (drains remaining ChunkBuffer).
        for worker in self._workers:
            worker.stop(drain=True)
        for worker in self._workers:
            worker.wait(5000)
        self._workers = []

        # Persist live transcript (if any) before kicking off batch pass.
        store = TranscriptStore(session.id)
        if self._live_segments:
            store.write_segments(self._live_segments)
            self.store.update_session(session.id, has_transcript=True)
            self.transcript_replaced.emit(session.id, list(self._live_segments))

        self.store.update_session(
            session.id,
            state=STATE_PROCESSING,
            ended_at=utc_now_iso(),
            duration_seconds=duration,
        )
        session.state = STATE_PROCESSING
        self.state_changed.emit(session.id, STATE_PROCESSING)

        # Kick off the batch / final pass in a background thread.
        self._batch_thread = _BatchTranscribeThread(
            self._mic_wav,
            self._sys_wav,
            self.config.transcription.model_size,
            vad_filter=self.config.audio.vad_enabled,
            vad_min_silence_ms=self.config.audio.vad_min_silence_ms,
        )
        self._batch_thread.progress.connect(self.status.emit)
        self._batch_thread.done.connect(lambda segs: self._on_batch_done(session, store, segs))
        self._batch_thread.failed.connect(lambda msg: self._on_batch_failed(session, msg))
        self._batch_thread.start()

    def _on_batch_done(self, session: Session, store: TranscriptStore, segments: list[TranscriptSegment]) -> None:
        if segments:
            store.write_segments(segments)
            self.store.update_session(session.id, has_transcript=True)
            self.transcript_replaced.emit(session.id, segments)
        # Audio retention: delete WAVs if not retained.
        if not session.retain_audio:
            audio_dir = session_audio_dir(session.id)
            try:
                shutil.rmtree(audio_dir, ignore_errors=True)
                self.store.update_session(session.id, has_audio=False)
            except OSError:
                log.exception("audio cleanup failed")
        self.store.update_session(session.id, state=STATE_COMPLETE)
        session.state = STATE_COMPLETE
        self.state_changed.emit(session.id, STATE_COMPLETE)
        self._teardown_recording()
        self.status.emit("Transcription complete.")

    def _on_batch_failed(self, session: Session, msg: str) -> None:
        self.store.update_session(session.id, state=STATE_ERROR)
        session.state = STATE_ERROR
        self.state_changed.emit(session.id, STATE_ERROR)
        self.error.emit(f"Final transcription failed: {msg}")
        self._teardown_recording(error=True)

    def _teardown_recording(self, *, error: bool = False) -> None:
        self._active_session = None
        self._mic_recorder = None
        self._loopback_recorder = None
        self._chunk_buffer = None
        self._t_start_wall = None
        self._mic_wav = None
        self._sys_wav = None
        if error:
            self._live_segments = []

    # ---- live segment routing ---------------------------------------------

    def _on_live_segment(self, segment: TranscriptSegment) -> None:
        if self._active_session is None:
            return
        self._live_segments.append(segment)
        self.segment_arrived.emit(self._active_session.id, segment)

    # ---- per-session field updates ----------------------------------------

    def set_retain_audio(self, session_id: str, retain: bool) -> None:
        self.store.update_session(session_id, retain_audio=retain)
        if self._active_session and self._active_session.id == session_id:
            self._active_session.retain_audio = retain

    # ---- crash recovery ---------------------------------------------------

    def recover_orphans(self) -> list[Session]:
        """Mark sessions stuck in transient states as 'error'. Returns the affected sessions."""
        orphans = self.store.find_orphans()
        for s in orphans:
            self.store.update_session(s.id, state=STATE_ERROR)
        return orphans
