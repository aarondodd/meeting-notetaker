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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
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
    """Wraps batch_transcribe for the final-pass after stop().

    Sources (mic + sys) run concurrently in a ThreadPoolExecutor since
    faster-whisper's `transcribe()` is thread-safe and we share one model
    instance. On modern multi-core CPUs this roughly halves wall-clock for
    two-source meetings; for single-source recordings it is a no-op.

    Progress is reported as a single 0-100% number, derived from each
    source's per-segment progress combined (mean across active sources).
    """

    done = pyqtSignal(list)            # list[TranscriptSegment]
    progress = pyqtSignal(str)         # human-readable status text
    progress_pct = pyqtSignal(int)     # 0..100 (combined across active sources)
    failed = pyqtSignal(str)

    def __init__(
        self,
        mic_wav: Optional[Path],
        sys_wav: Optional[Path],
        model_size: str,
        *,
        vad_filter: bool,
        vad_min_silence_ms: int,
        beam_size: int = 5,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.mic_wav = mic_wav
        self.sys_wav = sys_wav
        self.model_size = model_size
        self.vad_filter = vad_filter
        self.vad_min_silence_ms = vad_min_silence_ms
        self.beam_size = beam_size

    def run(self) -> None:
        try:
            model = model_manager.get_model(self.model_size, progress=self.progress.emit)
            tasks: list[tuple[str, Path]] = []
            if self.mic_wav and self.mic_wav.exists() and self.mic_wav.stat().st_size > 44:
                tasks.append((MIC, self.mic_wav))
            if self.sys_wav and self.sys_wav.exists() and self.sys_wav.stat().st_size > 44:
                tasks.append((SYS, self.sys_wav))

            per_source_pct: dict[str, float] = {src: 0.0 for src, _ in tasks}
            pct_lock = Lock()

            def on_pct(source: str, pct: float) -> None:
                with pct_lock:
                    per_source_pct[source] = pct
                    if per_source_pct:
                        combined = int(100 * sum(per_source_pct.values()) / len(per_source_pct))
                    else:
                        combined = 0
                self.progress_pct.emit(combined)

            results: list[TranscriptSegment] = []
            if not tasks:
                self.done.emit([])
                return
            with ThreadPoolExecutor(max_workers=max(1, len(tasks))) as pool:
                futures = {
                    pool.submit(
                        batch_transcribe,
                        wav, model,
                        source=src,
                        beam_size=self.beam_size,
                        vad_filter=self.vad_filter,
                        vad_min_silence_ms=self.vad_min_silence_ms,
                        progress=self.progress.emit,
                        on_progress_pct=on_pct,
                    ): src
                    for src, wav in tasks
                }
                for fut in futures:
                    results += fut.result()
            self.progress_pct.emit(100)
            self.done.emit(interleave(results, []))
        except Exception as exc:  # pragma: no cover - thread safety net
            log.exception("batch transcribe failed")
            self.failed.emit(str(exc))


class SessionController(QObject):
    state_changed = pyqtSignal(str, str)               # session_id, state
    segment_arrived = pyqtSignal(str, object)          # session_id, TranscriptSegment
    transcript_replaced = pyqtSignal(str, list)        # session_id, list[TranscriptSegment]
    batch_progress = pyqtSignal(str, int)              # session_id, pct (0..100)
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
            self._mic_recorder = MicRecorder(
                self._chunk_buffer,
                self._mic_wav,
                source_name=MIC,
                device_name=self.config.audio.mic_device_name,
            )
            self._mic_recorder.error.connect(self.error.emit)
            self._mic_recorder.start()

            # Loopback recorder (Windows only; fail soft if unavailable).
            from .audio.loopback_recorder import LoopbackRecorder, LoopbackUnavailable
            if LoopbackRecorder.is_available():
                self._loopback_recorder = LoopbackRecorder(
                    self._chunk_buffer,
                    self._sys_wav,
                    source_name=SYS,
                    device_name=self.config.audio.loopback_device_name,
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

        # Persist live transcript (if any). This is the "good enough" version
        # the user can synthesize from immediately while the batch pass refines
        # it in the background.
        store = TranscriptStore(session.id)
        if self._live_segments:
            store.write_segments(self._live_segments)
            self.store.update_session(session.id, has_transcript=True)
            self.transcript_replaced.emit(session.id, list(self._live_segments))

        self.store.update_session(
            session.id,
            ended_at=utc_now_iso(),
            duration_seconds=duration,
        )

        # Skip batch pass entirely if configured. The live transcript becomes
        # final and the user moves straight to STATE_COMPLETE (and audio
        # cleanup) -- no 30-min wait for a 30-min recording.
        if self.config.transcription.skip_batch_refinement:
            self._finalize_session(session, batch_segments=None)
            return

        self.store.update_session(session.id, state=STATE_PROCESSING)
        session.state = STATE_PROCESSING
        self.state_changed.emit(session.id, STATE_PROCESSING)
        self.batch_progress.emit(session.id, 0)

        beam_size = 1 if self.config.transcription.fast_batch else 5
        self._batch_thread = _BatchTranscribeThread(
            self._mic_wav,
            self._sys_wav,
            self.config.transcription.model_size,
            vad_filter=self.config.audio.vad_enabled,
            vad_min_silence_ms=self.config.audio.vad_min_silence_ms,
            beam_size=beam_size,
        )
        self._batch_thread.progress.connect(self.status.emit)
        self._batch_thread.progress_pct.connect(
            lambda pct, sid=session.id: self.batch_progress.emit(sid, pct)
        )
        self._batch_thread.done.connect(lambda segs: self._on_batch_done(session, store, segs))
        self._batch_thread.failed.connect(lambda msg: self._on_batch_failed(session, msg))
        self._batch_thread.start()

    def _finalize_session(
        self,
        session: Session,
        *,
        batch_segments: Optional[list[TranscriptSegment]],
    ) -> None:
        """Commit final state for a stopped session.

        If `batch_segments` is provided, the live transcript is replaced by
        the batch result on disk. Otherwise the on-disk live transcript is
        treated as final (the skip-batch-refinement path).
        """
        store = TranscriptStore(session.id)
        if batch_segments is not None and batch_segments:
            store.write_segments(batch_segments)
            self.store.update_session(session.id, has_transcript=True)
            self.transcript_replaced.emit(session.id, batch_segments)
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

    def _on_batch_done(
        self,
        session: Session,
        store: TranscriptStore,
        segments: list[TranscriptSegment],
    ) -> None:
        self._finalize_session(session, batch_segments=segments)

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
