"""Session lifecycle controller.

Owns two independent state surfaces:

  - The **live recording engine**: at most one session at a time, captured
    in `_active_recording_session` plus the recorder / chunk-buffer /
    worker fields. Cleared by `_teardown_recording` once the WAVs close
    and the live workers drain (i.e. at Stop).
  - **Per-session post-processing**: a dict keyed by session_id of
    `_ProcessingState` entries, each owning a batch transcription thread
    and (optionally) a speaker refinement thread for one specific
    session. Entries live from Stop until the diarization pass finishes
    and the session is finalized.

Splitting them lets the user start recording session B while session A
is still post-processing -- the live engine slot is free as soon as
session A's WAVs close, even though A's batch and refinement threads
keep running on disk-backed inputs.

This is the bridge between the UI signals and the audio/transcription
modules; the UI never touches recorders or model_manager directly.
"""
from __future__ import annotations

import logging
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Callable, Optional

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from .audio.chunk_buffer import ChunkBuffer
from .diarization import user_voiceprint as user_voiceprint_module
from .diarization.embeddings import EmbedderUnavailable, default_encoder
from .diarization.persistence import save_diarization
from .diarization.refiner import (
    RefinementResult,
    apply_labels_to_segments,
    refine_loopback,
)
from .diarization.store import open_speaker_store
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
from .utils.live_notes import extract_section, parse_attendees
from .utils.paths import app_data_dir, session_audio_dir
from .utils.vocabulary import derive_session_hotwords, join_hotwords, load_vocabulary


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
        hotwords: str = "",
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.mic_wav = mic_wav
        self.sys_wav = sys_wav
        self.model_size = model_size
        self.vad_filter = vad_filter
        self.vad_min_silence_ms = vad_min_silence_ms
        self.beam_size = beam_size
        self.hotwords = hotwords

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
                        hotwords=self.hotwords,
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


def _load_user_voiceprint_for_refinement():
    """Return the stored user-voiceprint embedding, or None.

    The refiner only consults it when present; absence means mic-source
    transcript segments stay attributed to the user by default (legacy
    behavior). Catches and logs file-level errors so a bad on-disk
    voiceprint never blocks the refinement pass.
    """
    try:
        vp = user_voiceprint_module.load()
    except Exception:
        log.exception("user voiceprint load failed; refinement falls back to default")
        return None
    return vp.embedding if vp is not None else None


class _SpeakerRefinementThread(QThread):
    """Runs the post-meeting speaker-identification pass off the UI thread.

    Loads the loopback WAV, segments it into turns, embeds each turn, and
    clusters them; matches each cluster against the speaker store. Emits
    the RefinementResult on success so the controller can rewrite the
    transcript with names and surface unknown clusters to the UI.

    Failure modes: SpeechBrain not importable (`EmbedderUnavailable`) or
    the loopback WAV doesn't exist / is empty. Both are reported via
    `skipped` rather than `failed` so the controller treats them as a
    soft no-op and proceeds straight to STATE_COMPLETE.
    """

    done = pyqtSignal(object)                # RefinementResult
    skipped = pyqtSignal(str)                # reason text
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(
        self,
        sys_wav: Path,
        transcript_segments: list,
        *,
        match_threshold: float,
        merge_threshold: float,
        mic_wav: Optional[Path] = None,
        user_voiceprint=None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.sys_wav = sys_wav
        self.mic_wav = mic_wav
        self.transcript_segments = transcript_segments
        self.match_threshold = match_threshold
        self.merge_threshold = merge_threshold
        self.user_voiceprint = user_voiceprint

    def run(self) -> None:
        try:
            if not self.sys_wav or not self.sys_wav.exists() or self.sys_wav.stat().st_size <= 44:
                self.skipped.emit("no loopback audio to analyze")
                return
            sys_segments = [s for s in self.transcript_segments if s.source == SYS]
            if not sys_segments:
                self.skipped.emit("no system-audio transcript segments to label")
                return
            if not default_encoder.is_available:
                self.skipped.emit(
                    "speaker-embedding model unavailable "
                    "(SpeechBrain not installed)"
                )
                return
            self.progress.emit("Identifying speakers...")
            speaker_store = open_speaker_store()
            try:
                result = refine_loopback(
                    self.sys_wav,
                    self.transcript_segments,
                    speaker_store=speaker_store,
                    encoder=default_encoder,
                    sys_source=SYS,
                    mic_source=MIC,
                    mic_wav=self.mic_wav,
                    user_voiceprint=self.user_voiceprint,
                    match_threshold=self.match_threshold,
                    merge_threshold=self.merge_threshold,
                )
            finally:
                speaker_store.close()
            self.done.emit(result)
        except EmbedderUnavailable as exc:
            self.skipped.emit(str(exc))
        except Exception as exc:  # pragma: no cover - thread safety net
            log.exception("speaker refinement failed")
            self.failed.emit(str(exc))


@dataclass
class _ProcessingState:
    """Per-session post-processing state.

    Captured at Stop time so the controller can release the live
    recording engine immediately while the batch + refinement passes
    keep running against the on-disk WAVs. Cleared from
    `SessionController._processing_sessions` once `_finalize_session`
    commits the final transcript and (optionally) cleans up audio.
    """

    session: Session
    mic_wav: Optional[Path]
    sys_wav: Optional[Path]
    live_segments: list[TranscriptSegment]
    batch_thread: Optional["_BatchTranscribeThread"] = None
    refinement_thread: Optional["_SpeakerRefinementThread"] = None


class SessionController(QObject):
    state_changed = pyqtSignal(str, str)               # session_id, state
    segment_arrived = pyqtSignal(str, object)          # session_id, TranscriptSegment
    transcript_replaced = pyqtSignal(str, list)        # session_id, list[TranscriptSegment]
    batch_progress = pyqtSignal(str, int)              # session_id, pct (0..100)
    # Speaker-refinement signals. Fired after the batch transcription pass
    # commits but before STATE_COMPLETE. The UI uses them to (a) update the
    # status label while refinement runs, (b) pop the Label Unknown Speakers
    # dialog when the result has unknown clusters.
    speaker_refinement_starting = pyqtSignal(str)              # session_id
    speaker_refinement_done = pyqtSignal(str, object)          # session_id, RefinementResult
    speaker_refinement_skipped = pyqtSignal(str, str)          # session_id, reason
    error = pyqtSignal(str)
    status = pyqtSignal(str)

    def __init__(self, store: SessionStore, config: Config, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.store = store
        self.config = config
        # Live recording engine slot. Only one session may hold the mic +
        # loopback at a time; cleared on Stop once the WAVs close.
        self._active_recording_session: Optional[Session] = None
        self._mic_recorder = None
        self._loopback_recorder = None
        self._chunk_buffer: Optional[ChunkBuffer] = None
        self._workers: list[LiveTranscriptionWorker] = []
        self._t_start_wall: Optional[float] = None
        self._live_segments: list[TranscriptSegment] = []
        self._mic_wav: Optional[Path] = None
        self._sys_wav: Optional[Path] = None
        self._session_hotwords: str = ""
        # Per-session post-processing entries, keyed by session_id. Survives
        # the live-engine teardown so a follow-on recording can start while
        # this session's batch + speaker passes run in the background.
        self._processing_sessions: dict[str, _ProcessingState] = {}

    def _collect_hotwords(self, session: Session) -> str:
        """Combine global vocabulary + this session's attendees + agenda proper nouns.

        Attendee names come from the live_notes '# Attendees' block (whether
        the user typed them or the calendar pre-fill seeded them). Proper
        nouns get pulled out of the '# Agenda' block via a coarse capitalized-
        phrase match -- false positives are cheap so we err toward inclusive.
        """
        live_notes = ""
        try:
            live_notes = TranscriptStore(session.id).read_live_notes()
        except OSError:
            log.exception("could not read live_notes for hotword derivation")
        attendees = parse_attendees(live_notes) if live_notes else []
        agenda = extract_section(live_notes, "Agenda") if live_notes else ""
        derived = derive_session_hotwords(
            load_vocabulary(),
            attendees=attendees,
            agenda=agenda,
        )
        return join_hotwords(derived)

    @property
    def active_session(self) -> Optional[Session]:
        """The session that currently owns the live recording engine.

        Returns None when the mic + loopback are idle, regardless of how
        many other sessions are in post-Stop processing.
        """
        return self._active_recording_session

    @property
    def processing_session_ids(self) -> list[str]:
        """IDs of sessions whose batch and/or refinement passes are still
        running in the background."""
        return list(self._processing_sessions.keys())

    # ---- session lifecycle -------------------------------------------------

    def start_session(self, session: Session) -> None:
        # Only the live-recording slot blocks a new session; sessions in
        # post-Stop batch / refinement processing run independently and
        # do not gate the next recording.
        if self._active_recording_session is not None:
            self.error.emit("Another session is already recording. Stop it before starting a new one.")
            return
        self._active_recording_session = session
        self._live_segments = []
        hotwords = self._collect_hotwords(session)
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
                        hotwords=hotwords,
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
            self._session_hotwords = hotwords
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
            return

    def pause_session(self) -> None:
        if self._active_recording_session is None:
            return
        if self._mic_recorder is not None:
            self._mic_recorder.pause()
        if self._loopback_recorder is not None:
            self._loopback_recorder.pause()
        self.store.update_session(self._active_recording_session.id, state=STATE_PAUSED)
        self._active_recording_session.state = STATE_PAUSED
        self.state_changed.emit(self._active_recording_session.id, STATE_PAUSED)

    def resume_session(self) -> None:
        if self._active_recording_session is None:
            return
        if self._mic_recorder is not None:
            self._mic_recorder.resume()
        if self._loopback_recorder is not None:
            self._loopback_recorder.resume()
        self.store.update_session(self._active_recording_session.id, state=STATE_RECORDING)
        self._active_recording_session.state = STATE_RECORDING
        self.state_changed.emit(self._active_recording_session.id, STATE_RECORDING)

    def stop_session(self) -> None:
        if self._active_recording_session is None:
            return
        session = self._active_recording_session
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

        # Capture per-session post-processing state now, before tearing
        # down the live engine. The batch + refinement threads will read
        # the WAV paths off this dataclass instead of self._mic_wav etc.,
        # so the live engine is free to start a new recording immediately.
        proc_state = _ProcessingState(
            session=session,
            mic_wav=self._mic_wav,
            sys_wav=self._sys_wav,
            live_segments=list(self._live_segments),
        )
        self._processing_sessions[session.id] = proc_state
        hotwords = self._session_hotwords
        skip_batch = self.config.transcription.skip_batch_refinement
        # Release the live recording engine immediately so the user can
        # start the next session while this one's batch pass runs.
        self._teardown_recording()

        # Skip batch pass entirely if configured. The live transcript becomes
        # final and the user moves straight to STATE_COMPLETE (and audio
        # cleanup) -- no 30-min wait for a 30-min recording.
        if skip_batch:
            self._finalize_session(session.id, batch_segments=None)
            return

        self.store.update_session(session.id, state=STATE_PROCESSING)
        session.state = STATE_PROCESSING
        self.state_changed.emit(session.id, STATE_PROCESSING)
        self.batch_progress.emit(session.id, 0)

        beam_size = 1 if self.config.transcription.fast_batch else 5
        batch_thread = _BatchTranscribeThread(
            proc_state.mic_wav,
            proc_state.sys_wav,
            self.config.transcription.model_size,
            vad_filter=self.config.audio.vad_enabled,
            vad_min_silence_ms=self.config.audio.vad_min_silence_ms,
            beam_size=beam_size,
            hotwords=hotwords,
        )
        batch_thread.progress.connect(self.status.emit)
        batch_thread.progress_pct.connect(
            lambda pct, sid=session.id: self.batch_progress.emit(sid, pct)
        )
        sid = session.id
        batch_thread.done.connect(lambda segs, _sid=sid: self._on_batch_done(_sid, segs))
        batch_thread.failed.connect(lambda msg, _sid=sid: self._on_batch_failed(_sid, msg))
        proc_state.batch_thread = batch_thread
        batch_thread.start()

    def _finalize_session(
        self,
        session_id: str,
        *,
        batch_segments: Optional[list[TranscriptSegment]],
    ) -> None:
        """Commit final state for a stopped session.

        If `batch_segments` is provided, the live transcript is replaced by
        the batch result on disk. Otherwise the on-disk live transcript is
        treated as final (the skip-batch-refinement path).

        Pulls per-session state from `_processing_sessions[session_id]` and
        removes the entry on completion. The live recording engine is
        already torn down at this point.
        """
        proc_state = self._processing_sessions.get(session_id)
        if proc_state is None:
            log.warning("finalize called for unknown processing session %s", session_id)
            return
        session = proc_state.session
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
        self._processing_sessions.pop(session_id, None)
        self.status.emit("Transcription complete.")

    def _on_batch_done(
        self,
        session_id: str,
        segments: list[TranscriptSegment],
    ) -> None:
        """Commit the batch transcript, then run speaker refinement.

        Refinement is skipped when (a) speakers.enabled is False, (b) the
        loopback WAV is missing or empty (mic-only session), or (c) the
        SpeechBrain embedder isn't importable. In any of those cases the
        session moves straight to STATE_COMPLETE via _finalize_session.

        Otherwise a worker thread runs the diarization pass on the
        loopback WAV. The audio dir is NOT cleaned up until after
        refinement finishes -- the WAV is needed for embedding extraction.
        """
        proc_state = self._processing_sessions.get(session_id)
        if proc_state is None:
            log.warning("batch done for unknown processing session %s", session_id)
            return
        session = proc_state.session
        store = TranscriptStore(session.id)
        if segments:
            store.write_segments(segments)
            self.store.update_session(session.id, has_transcript=True)
            self.transcript_replaced.emit(session.id, segments)
        # Capture the latest on-disk transcript for the refinement input.
        # Use the batch segments when present, else fall back to whatever
        # the live workers wrote at Stop.
        refinement_input = segments or list(proc_state.live_segments)

        if not self.config.speakers.enabled:
            self._finalize_session(session_id, batch_segments=None)
            return
        if not proc_state.sys_wav or not proc_state.sys_wav.exists():
            self._finalize_session(session_id, batch_segments=None)
            return

        self.speaker_refinement_starting.emit(session.id)
        self.status.emit("Identifying speakers...")
        voiceprint = _load_user_voiceprint_for_refinement()
        refinement_thread = _SpeakerRefinementThread(
            proc_state.sys_wav,
            refinement_input,
            match_threshold=self.config.speakers.match_threshold,
            merge_threshold=self.config.speakers.merge_threshold,
            mic_wav=proc_state.mic_wav,
            user_voiceprint=voiceprint,
        )
        refinement_thread.progress.connect(self.status.emit)
        refinement_thread.done.connect(
            lambda res, sid=session_id, segs=refinement_input:
            self._on_refinement_done(sid, segs, res)
        )
        refinement_thread.skipped.connect(
            lambda reason, sid=session_id: self._on_refinement_skipped(sid, reason)
        )
        refinement_thread.failed.connect(
            lambda msg, sid=session_id: self._on_refinement_failed(sid, msg)
        )
        proc_state.refinement_thread = refinement_thread
        refinement_thread.start()

    def _on_refinement_done(
        self,
        session_id: str,
        input_segments: list[TranscriptSegment],
        result: RefinementResult,
    ) -> None:
        """Apply labels, persist diarization metadata, and finalize."""
        try:
            labeled = apply_labels_to_segments(input_segments, result.segment_labels)
            store = TranscriptStore(session_id)
            store.write_segments(labeled)
            self.transcript_replaced.emit(session_id, labeled)
            save_diarization(
                store.session_dir,
                loopback_wav="audio/sys.wav",
                match_threshold=self.config.speakers.match_threshold,
                refinement_result=result,
                transcript_segments=labeled,
            )
        except Exception:
            log.exception("post-refinement write failed; transcript unchanged")
        # Surface the result so the UI can pop the Label Unknown Speakers
        # dialog. Emit before finalize so the dialog can grab a fresh
        # sample clip from the loopback WAV (still on disk).
        self.speaker_refinement_done.emit(session_id, result)
        self._finalize_session(session_id, batch_segments=None)

    def _on_refinement_skipped(self, session_id: str, reason: str) -> None:
        log.info("speaker refinement skipped for %s: %s", session_id, reason)
        self.speaker_refinement_skipped.emit(session_id, reason)
        self._finalize_session(session_id, batch_segments=None)

    def _on_refinement_failed(self, session_id: str, msg: str) -> None:
        log.warning("speaker refinement failed for %s: %s", session_id, msg)
        # Don't fail the whole session over a refinement bug; just skip
        # the labeling step and finalize normally.
        self.speaker_refinement_skipped.emit(session_id, f"failed: {msg}")
        self._finalize_session(session_id, batch_segments=None)

    def _on_batch_failed(self, session_id: str, msg: str) -> None:
        proc_state = self._processing_sessions.pop(session_id, None)
        self.store.update_session(session_id, state=STATE_ERROR)
        if proc_state is not None:
            proc_state.session.state = STATE_ERROR
        self.state_changed.emit(session_id, STATE_ERROR)
        self.error.emit(f"Final transcription failed: {msg}")

    def _teardown_recording(self, *, error: bool = False) -> None:
        """Release the live recording engine slot.

        Does NOT touch `_processing_sessions` -- per-session batch and
        refinement threads continue running on disk-backed inputs after
        this returns.
        """
        self._active_recording_session = None
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
        if self._active_recording_session is None:
            return
        self._live_segments.append(segment)
        self.segment_arrived.emit(self._active_recording_session.id, segment)

    # ---- per-session field updates ----------------------------------------

    def set_retain_audio(self, session_id: str, retain: bool) -> None:
        self.store.update_session(session_id, retain_audio=retain)
        if self._active_recording_session and self._active_recording_session.id == session_id:
            self._active_recording_session.retain_audio = retain
        proc_state = self._processing_sessions.get(session_id)
        if proc_state is not None:
            proc_state.session.retain_audio = retain

    # ---- crash recovery ---------------------------------------------------

    def recover_orphans(self) -> list[Session]:
        """Mark sessions stuck in transient states as 'error'. Returns the affected sessions."""
        orphans = self.store.find_orphans()
        for s in orphans:
            self.store.update_session(s.id, state=STATE_ERROR)
        return orphans
