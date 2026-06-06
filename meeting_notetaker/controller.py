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
from .models.speaker_tags import SpeakerTag, SpeakerTagStore
from .models.transcript import MIC, SYS, TranscriptSegment, TranscriptStore
from .transcription import model_manager
from .transcription.worker import LiveTranscriptionWorker, batch_transcribe, interleave
from .utils.config import Config
from .utils.live_notes import extract_section, parse_attendees
from .utils.paths import app_data_dir, session_audio_dir, session_dir
from .utils.vocabulary import derive_session_hotwords, join_hotwords, load_vocabulary


log = logging.getLogger(__name__)


def _retire_thread(thread: Optional[QThread]) -> None:
    """Join an OS thread before destroying its QObject wrapper.

    QThread.finished fires inside the run-wrapper BEFORE the OS thread
    is joined. Calling deleteLater (or letting Python refcount drop)
    races against that join and trips "QThread: Destroyed while thread
    is still running" on Windows. Mirrors the helpers in
    audio/player.py and ui/attachment_preview.py -- inlined here to
    keep controller.py self-contained.

    Safe to call with None (e.g., when proc_state.batch_thread was
    never set because the session went straight to finalize).
    """
    if thread is None:
        return
    try:
        thread.wait()
    except Exception:
        log.exception("controller: thread.wait() failed during retirement")
    try:
        thread.deleteLater()
    except Exception:
        log.exception("controller: thread.deleteLater() failed during retirement")


class _RetainedAudioEncodeWorker(QThread):
    """Re-encode mic.wav + sys.wav to the configured retain format on
    a background thread (issue #45).

    The encode itself (PyAV via audio.encode.encode_pair) is CPU-bound
    and takes ~10 s for a 20-minute recording. Running it on the Qt
    main thread inside `_finalize_session` froze the UI for the full
    duration -- the watchdog caught it after the 750 ms threshold.

    Emits exactly one of:
    * `done()` -- encode completed (success OR caught failure).
    * `failed(message)` -- only on a programming/IO error outside the
      encode call itself; the path is kept narrow because the
      individual-format failure path is already caught + logged
      inside the worker.
    """

    done = pyqtSignal()
    # Free-text status message the controller forwards to the app
    # (e.g., "Audio retained as WAV (opus re-encode failed)").
    status_message = pyqtSignal(str)

    def __init__(
        self,
        *,
        mic_wav: Path,
        sys_wav: Path,
        fmt: str,
    ) -> None:
        super().__init__(None)
        self._mic_wav = mic_wav
        self._sys_wav = sys_wav
        self._fmt = fmt

    def run(self) -> None:
        try:
            from .audio.encode import encode_pair  # noqa: PLC0415
            encode_pair(self._mic_wav, self._sys_wav, self._fmt)
        except ImportError:
            log.warning("PyAV not available; keeping WAVs in place")
            self.status_message.emit(
                "Audio retained as WAV (compressed encoder unavailable)."
            )
        except Exception:
            log.exception("retained-audio re-encode failed (fmt=%s)", self._fmt)
            self.status_message.emit(
                f"Audio retained as WAV ({self._fmt} re-encode failed; see log)."
            )
        finally:
            self.done.emit()


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
        beam_size: int = 1,
        hotwords: str = "",
        cpu_threads: int = 0,
        num_workers: int = 1,
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
        self.cpu_threads = cpu_threads
        self.num_workers = num_workers

    def run(self) -> None:
        try:
            model = model_manager.get_model(
                self.model_size,
                cpu_threads=self.cpu_threads,
                num_workers=self.num_workers,
                progress=self.progress.emit,
            )
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
    # Refinement-phase percent (0..100). Bucketed inside refine_loopback;
    # see the docstring there for what each band means.
    progress_pct = pyqtSignal(int)

    def __init__(
        self,
        sys_wav: Path,
        transcript_segments: list,
        *,
        match_threshold: float,
        merge_threshold: float,
        mic_wav: Optional[Path] = None,
        user_voiceprint=None,
        speaker_tags: Optional[list] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.sys_wav = sys_wav
        self.mic_wav = mic_wav
        self.transcript_segments = transcript_segments
        self.match_threshold = match_threshold
        self.merge_threshold = merge_threshold
        self.user_voiceprint = user_voiceprint
        self.speaker_tags = speaker_tags or []

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
                    speaker_tags=self.speaker_tags,
                    progress_cb=self.progress_pct.emit,
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
    speaker_tags: list[SpeakerTag] = field(default_factory=list)
    batch_thread: Optional["_BatchTranscribeThread"] = None
    refinement_thread: Optional["_SpeakerRefinementThread"] = None
    # Issue #45: opus / flac re-encode runs on a worker thread so the
    # 10-second encode for a long recording doesn't freeze the UI at
    # _finalize_session time. Held here so the worker survives until
    # its finished signal fires.
    encode_worker: Optional["_RetainedAudioEncodeWorker"] = None


class SessionController(QObject):
    state_changed = pyqtSignal(str, str)               # session_id, state
    segment_arrived = pyqtSignal(str, object)          # session_id, TranscriptSegment
    transcript_replaced = pyqtSignal(str, list)        # session_id, list[TranscriptSegment]
    # Unified post-Stop processing percent (0..100). The percent budget
    # is split across the batch-transcription and speaker-refinement
    # phases based on which ones will run -- see `_phase_plan` and
    # `_emit_unified_progress`. The signal name preserves backward compat
    # with the pre-unification "batch only" semantics; the SessionView
    # label format ("Refining transcript -- X%") is unchanged.
    batch_progress = pyqtSignal(str, int)              # session_id, unified pct (0..100)
    # Speaker-refinement signals. Fired after the batch transcription pass
    # commits but before STATE_COMPLETE. The UI uses them to (a) update the
    # status label while refinement runs, (b) pop the Label Unknown Speakers
    # dialog when the result has unknown clusters.
    speaker_refinement_starting = pyqtSignal(str)              # session_id
    speaker_refinement_done = pyqtSignal(str, object)          # session_id, RefinementResult
    speaker_refinement_skipped = pyqtSignal(str, str)          # session_id, reason
    # Emitted after a speaker tag is captured or removed during recording.
    # Carries the up-to-date `{name: count}` snapshot so the right-sidebar
    # widget can refresh its badge counters without re-reading the file.
    speaker_tags_changed = pyqtSignal(str, dict)               # session_id, counts
    error = pyqtSignal(str)
    status = pyqtSignal(str)
    # Non-fatal capture warning forwarded from the recorder (issue #44).
    # The recording still succeeded; the trailing seconds of the WAV
    # are silence because the device callback stalled before Stop.
    # The app surfaces this as a status-bar message rather than an
    # error dialog so the user keeps the session.
    capture_warning = pyqtSignal(str, str)  # session_id, message

    def __init__(self, store: SessionStore, config: Config, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.store = store
        self.config = config
        # Live recording engine slot. Only one session may hold the mic +
        # loopback at a time; cleared on Stop once the WAVs close.
        self._active_recording_session: Optional[Session] = None
        self._mic_recorder = None
        self._loopback_recorder = None
        # Hot-plug watcher for multi-endpoint mode (#85.6). Owns a
        # QTimer-driven poll over the WASAPI output endpoint set;
        # debounces flapping connectors before extending capture.
        self._endpoint_watcher = None
        self._chunk_buffer: Optional[ChunkBuffer] = None
        self._workers: list[LiveTranscriptionWorker] = []
        self._t_start_wall: Optional[float] = None
        # Recording-active elapsed-time accounting. The live recorders drop
        # frames while paused, so the WAV time is shorter than wall-clock
        # by the cumulative pause duration. Speaker tags need WAV time so
        # they snap to the right turn at refinement; we track active time
        # via these two fields (sum of completed active spans + the
        # current span's monotonic start).
        self._active_elapsed_accumulated_sec: float = 0.0
        self._active_span_start_monotonic: Optional[float] = None
        self._live_segments: list[TranscriptSegment] = []
        self._mic_wav: Optional[Path] = None
        self._sys_wav: Optional[Path] = None
        self._session_hotwords: str = ""
        # Per-session speaker tag stores. Keyed by session_id; instantiated
        # at start_session and consulted by tag_speaker / refinement.
        self._tag_stores: dict[str, "SpeakerTagStore"] = {}
        # Per-session phase plan for the unified post-Stop progress bar.
        # Populated in stop_session when we know which phases will fire.
        # Shape: {session_id: {"batch": (lo, hi), "refinement": (lo, hi)}}
        # where each tuple is the slice of the 0..100 unified bar that
        # phase fills. A phase that won't run is absent from the dict.
        self._phase_plans: dict[str, dict[str, tuple[int, int]]] = {}
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

    def start_session(
        self,
        session: Session,
        *,
        capture_only_override: Optional[bool] = None,
    ) -> None:
        # Only the live-recording slot blocks a new session; sessions in
        # post-Stop batch / refinement processing run independently and
        # do not gate the next recording.
        if self._active_recording_session is not None:
            self.error.emit("Another session is already recording. Stop it before starting a new one.")
            return
        self._active_recording_session = session
        self._live_segments = []
        hotwords = self._collect_hotwords(session)
        capture_only_effective = (
            capture_only_override
            if capture_only_override is not None
            else self.config.transcription.capture_only_mode
        )
        try:
            audio_dir = session_audio_dir(session.id)
            self._mic_wav = audio_dir / "mic.wav"
            self._sys_wav = audio_dir / "sys.wav"
            # Issue #47: the chunk_buffer is purely a scratchpad for
            # LiveTranscriptionWorker.pop_window. In capture-only mode
            # no worker is created so nothing drains it -- and the
            # recorders' callbacks would still push into it, growing
            # the underlying numpy array O(N) per write and starving
            # the PortAudio capture callback at ~10-15 min in. Don't
            # even allocate it when there's no consumer.
            self._chunk_buffer = (
                ChunkBuffer(sources=[MIC, SYS])
                if not capture_only_effective
                else None
            )

            # Live transcription off if capture-only mode is on. The model is
            # expected to be already cached -- the app layer preloads it via a
            # progress dialog before calling us, so this get_model() is a
            # near-instant cache hit on the second-and-subsequent runs.
            if not capture_only_effective:
                model = model_manager.get_model(
                    self.config.transcription.model_size,
                    cpu_threads=self.config.transcription.resolved_cpu_threads(),
                    num_workers=self.config.transcription.num_workers,
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
            # Non-fatal capture-stall warning (issue #44). Routed to a
            # local handler that tags the message with the session id
            # before emitting capture_warning.
            mic_session_id = session.id
            self._mic_recorder.capture_warning.connect(
                lambda msg, sid=mic_session_id: self.capture_warning.emit(sid, msg)
            )
            self._mic_recorder.start()

            # Loopback recorder (Windows only; fail soft if unavailable).
            # When multi_endpoint_capture is on (default), the
            # MultiEndpointLoopbackRecorder orchestrator captures every
            # WASAPI output endpoint to a sidecar WAV and mixes at
            # Stop. Issue #84: this is the primary defense against the
            # "Teams routed to a different endpoint than the one we
            # bound to" failure mode. Single-endpoint mode stays
            # available as a fallback for users hitting WASAPI quirks.
            from .audio.loopback_recorder import LoopbackRecorder, LoopbackUnavailable
            from .audio.multi_loopback import MultiEndpointLoopbackRecorder
            multi = self.config.audio.multi_endpoint_capture
            if MultiEndpointLoopbackRecorder.is_available() and multi:
                self._loopback_recorder = MultiEndpointLoopbackRecorder(
                    self._chunk_buffer,
                    self._sys_wav,
                    source_name=SYS,
                )
            elif LoopbackRecorder.is_available():
                self._loopback_recorder = LoopbackRecorder(
                    self._chunk_buffer,
                    self._sys_wav,
                    source_name=SYS,
                    device_name=self.config.audio.loopback_device_name,
                )
            else:
                self._loopback_recorder = None
                self.status.emit("PyAudioWPatch not installed; recording mic only.")

            # Endpoint hot-plug watcher (#85.6). Only meaningful in
            # multi-endpoint mode -- when a USB headset / monitor
            # appears mid-call we extend capture to include it. Single-
            # endpoint mode treats hot-plug as a no-op.
            self._endpoint_watcher = None
            if (
                isinstance(self._loopback_recorder, MultiEndpointLoopbackRecorder)
                and self._loopback_recorder is not None
            ):
                try:
                    from .audio.endpoint_watcher import EndpointWatcher
                except ImportError:
                    EndpointWatcher = None
                if EndpointWatcher is not None:
                    self._endpoint_watcher = EndpointWatcher(parent=self)
                    self._endpoint_watcher.endpoints_changed.connect(
                        self._on_endpoints_changed
                    )

            if self._loopback_recorder is not None:
                self._loopback_recorder.error.connect(self.error.emit)
                # Tag warnings with session id so the app correlates
                # the warning to a specific recording.
                lb_session_id = session.id
                self._loopback_recorder.capture_warning.connect(
                    lambda msg, sid=lb_session_id: self.capture_warning.emit(sid, msg)
                )
                # Mid-recording silence detector (#84). The handler
                # cross-references with audio_session_monitor so the
                # banner can name the meeting app rendering audio
                # elsewhere (the diagnostic the user needs to act on).
                self._loopback_recorder.silence_detected.connect(
                    lambda sid=lb_session_id: self._on_loopback_silence_detected(sid)
                )
                self._loopback_recorder.silence_cleared.connect(
                    lambda sid=lb_session_id: self._on_loopback_silence_cleared(sid)
                )
                try:
                    self._loopback_recorder.start()
                    if self._endpoint_watcher is not None:
                        self._endpoint_watcher.start()
                except LoopbackUnavailable as exc:
                    log.warning("loopback unavailable: %s", exc)
                    self._loopback_recorder = None
                    if self._endpoint_watcher is not None:
                        try:
                            self._endpoint_watcher.stop()
                        except Exception:
                            log.exception("endpoint watcher stop after loopback fail")
                        self._endpoint_watcher = None
                    self.status.emit("System-audio loopback unavailable; recording mic only.")

            self._t_start_wall = time.monotonic()
            self._active_elapsed_accumulated_sec = 0.0
            self._active_span_start_monotonic = self._t_start_wall
            self._session_hotwords = hotwords
            # Per-session speaker tag store, lazily initialized so a session
            # that never gets tagged doesn't write any file.
            self._tag_stores[session.id] = SpeakerTagStore(session_dir(session.id))
            self.store.update_session(
                session.id,
                state=STATE_RECORDING,
                started_at=utc_now_iso(),
                model_used=self.config.transcription.model_size,
                has_audio=True,
            )
            session.state = STATE_RECORDING
            self.state_changed.emit(session.id, STATE_RECORDING)
            # When a prior session is still post-processing, the live
            # workers will share CPU with that session's batch + refinement
            # threads. faster-whisper transcribe() is thread-safe but not
            # priority-managed, so live decode can fall behind real-time
            # under load. Surface the situation rather than letting the
            # user wonder why the transcript pane is lagging.
            if self._processing_sessions:
                self.status.emit(
                    f"Recording started. {len(self._processing_sessions)} prior "
                    "session(s) still processing; live transcription may be slow."
                )
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
        # Roll the current active span into the accumulator so future tag
        # timestamps stay aligned with WAV time across the pause.
        if self._active_span_start_monotonic is not None:
            self._active_elapsed_accumulated_sec += (
                time.monotonic() - self._active_span_start_monotonic
            )
            self._active_span_start_monotonic = None
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
        # Start a fresh active span. Accumulator already holds the
        # pre-pause time.
        self._active_span_start_monotonic = time.monotonic()
        self.store.update_session(self._active_recording_session.id, state=STATE_RECORDING)
        self._active_recording_session.state = STATE_RECORDING
        self.state_changed.emit(self._active_recording_session.id, STATE_RECORDING)

    def _recording_active_elapsed_sec(self) -> float:
        """Wall-clock seconds the session has actually been recording
        (i.e. WAV-aligned: pause time excluded). Returns 0.0 if no
        session is recording."""
        if self._active_recording_session is None:
            return 0.0
        total = self._active_elapsed_accumulated_sec
        if self._active_span_start_monotonic is not None:
            total += time.monotonic() - self._active_span_start_monotonic
        return max(0.0, total)

    # ---- speaker tags (Step 4 of issue #11 -- click-to-tag during recording)

    def tag_speaker(self, name: str) -> None:
        """Capture a click-to-tag anchor for the active recording session.

        No-op when no session is currently in STATE_RECORDING or
        STATE_PAUSED -- the user can still tag while paused, since the
        WAV-aligned time is what matters and pause doesn't advance it.
        """
        if self._active_recording_session is None:
            return
        session = self._active_recording_session
        if session.state not in (STATE_RECORDING, STATE_PAUSED):
            return
        clean = (name or "").strip()
        if not clean:
            return
        store = self._tag_stores.get(session.id)
        if store is None:
            log.warning("tag_speaker called without an open tag store; ignoring")
            return
        t_seconds = self._recording_active_elapsed_sec()
        try:
            store.append(SpeakerTag(name=clean, t_seconds=t_seconds))
        except (OSError, ValueError):
            log.exception("failed to persist speaker tag %r at %.2fs", clean, t_seconds)
            return
        self.speaker_tags_changed.emit(session.id, store.counts())

    def remove_last_speaker_tag(self, name: str) -> None:
        """Undo the most recent tag for a given name on the active session."""
        if self._active_recording_session is None:
            return
        session = self._active_recording_session
        store = self._tag_stores.get(session.id)
        if store is None:
            return
        if store.remove_last_for(name):
            self.speaker_tags_changed.emit(session.id, store.counts())

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
        try:
            if self._endpoint_watcher is not None:
                self._endpoint_watcher.stop()
                self._endpoint_watcher = None
        except Exception:
            log.exception("endpoint watcher stop failed")

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
        # Snapshot any speaker tags captured during recording. The tag
        # store stays alive in self._tag_stores until _finalize_session;
        # post-stop tag-removal isn't supported (recording is over) so
        # we copy the list once here.
        tag_store = self._tag_stores.get(session.id)
        tags_snapshot = tag_store.load() if tag_store is not None else []
        proc_state = _ProcessingState(
            session=session,
            mic_wav=self._mic_wav,
            sys_wav=self._sys_wav,
            live_segments=list(self._live_segments),
            speaker_tags=tags_snapshot,
        )
        self._processing_sessions[session.id] = proc_state
        hotwords = self._session_hotwords
        skip_batch = self.config.transcription.skip_batch_refinement
        # Plan the unified post-Stop progress bar before any signals
        # fire. Refinement will run if speaker-ID is enabled AND we
        # captured loopback audio (the encoder-availability check
        # happens inside _SpeakerRefinementThread; if it bails to
        # `skipped` the bar will jump to 100 via _finalize anyway).
        will_run_refinement = bool(
            self.config.speakers.enabled
            and proc_state.sys_wav
            and proc_state.sys_wav.exists()
        )
        self._phase_plans[session.id] = self._build_phase_plan(
            will_run_batch=not skip_batch,
            will_run_refinement=will_run_refinement,
        )
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
        self._emit_unified_progress(session.id, "batch", 0)

        beam_size = 1 if self.config.transcription.fast_batch else 5
        batch_thread = _BatchTranscribeThread(
            proc_state.mic_wav,
            proc_state.sys_wav,
            self.config.transcription.model_size,
            vad_filter=self.config.audio.vad_enabled,
            vad_min_silence_ms=self.config.audio.vad_min_silence_ms,
            beam_size=beam_size,
            hotwords=hotwords,
            cpu_threads=self.config.transcription.resolved_cpu_threads(),
            num_workers=self.config.transcription.num_workers,
        )
        batch_thread.progress.connect(self.status.emit)
        batch_thread.progress_pct.connect(
            lambda pct, sid=session.id: self._emit_unified_progress(sid, "batch", pct)
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
            self._complete_session_finalize(session_id, proc_state)
            return
        # retain_audio=True path. If the configured format is wav, no
        # encode is needed and we can finish synchronously. Otherwise
        # dispatch the encode worker (issue #45); the rest of finalize
        # runs in a continuation when the worker emits done.
        fmt = self.config.audio.retain_format
        if fmt == "wav":
            self._complete_session_finalize(session_id, proc_state)
            return
        audio_dir = session_audio_dir(session_id)
        mic_wav = audio_dir / "mic.wav"
        sys_wav = audio_dir / "sys.wav"
        self.status.emit(f"Encoding retained audio ({fmt})...")
        worker = _RetainedAudioEncodeWorker(
            mic_wav=mic_wav, sys_wav=sys_wav, fmt=fmt,
        )
        worker.setObjectName(f"RetainedAudioEncodeWorker[{session_id[:8]}]")
        worker.status_message.connect(self.status.emit)
        worker.done.connect(
            lambda sid=session_id: self._on_retained_encode_done(sid)
        )
        worker.finished.connect(lambda w=worker: _retire_thread(w))
        # Hold a reference so the worker isn't GC'd before it runs.
        proc_state.encode_worker = worker
        worker.start()

    def _on_retained_encode_done(self, session_id: str) -> None:
        """Continuation of _finalize_session after the encode worker."""
        proc_state = self._processing_sessions.get(session_id)
        if proc_state is None:
            # Session was deleted while encoding ran. Encode worker
            # already cleaned up via its finished -> _retire_thread
            # connection; nothing left to do.
            return
        # The encode worker is about to be retired via its finished
        # signal; clear our reference here so the cleanup below
        # doesn't try to retire it twice.
        proc_state.encode_worker = None
        self._complete_session_finalize(session_id, proc_state)

    def _complete_session_finalize(self, session_id: str, proc_state) -> None:
        """The state-flip + cleanup sequence at the end of _finalize_session.

        Lives in its own function because the retain_audio=True path
        defers everything past audio handling to after the encode
        worker finishes; no-retain + wav-retain paths run it inline.
        """
        session = proc_state.session
        self.store.update_session(session.id, state=STATE_COMPLETE)
        session.state = STATE_COMPLETE
        self.state_changed.emit(session.id, STATE_COMPLETE)
        # Belt-and-suspenders: most completion paths already retired
        # their thread, but if a session went straight to finalize
        # (skip-batch path) the threads were never set, and if a
        # refinement_done landed _before_ we got here, retirement
        # already ran -- _retire_thread(None) is safe either way.
        _retire_thread(proc_state.batch_thread)
        proc_state.batch_thread = None
        _retire_thread(proc_state.refinement_thread)
        proc_state.refinement_thread = None
        self._processing_sessions.pop(session_id, None)
        # The tag store stayed in self._tag_stores so the sidebar widget
        # could keep showing live counts during processing. Drop it now
        # that the session is done.
        self._tag_stores.pop(session_id, None)
        # Force the unified progress bar to a clean 100 in case the last
        # in-phase emit landed below (e.g. refinement got skipped at
        # runtime for a missing encoder, or the batch path bypassed
        # refinement entirely). Emit directly so the cleanup is
        # independent of the phase plan, then drop the plan -- the bar
        # isn't going to update again for this id.
        self.batch_progress.emit(session_id, 100)
        self._phase_plans.pop(session_id, None)
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
        # Retire the batch QThread now that its work is done. The done
        # signal fires from inside run()'s return; wait() blocks at most
        # microseconds for the OS thread to finish joining. Without
        # this, the QThread sits in memory until app exit and can land
        # "Destroyed while running" if Qt cleans it up earlier.
        _retire_thread(proc_state.batch_thread)
        proc_state.batch_thread = None
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
            speaker_tags=proc_state.speaker_tags,
        )
        refinement_thread.progress.connect(self.status.emit)
        refinement_thread.progress_pct.connect(
            lambda pct, sid=session_id: self._emit_unified_progress(sid, "refinement", pct)
        )
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
        proc_state = self._processing_sessions.get(session_id)
        if proc_state is not None:
            _retire_thread(proc_state.refinement_thread)
            proc_state.refinement_thread = None
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
            self._self_improve_store_from_tagged_clusters(result)
        except Exception:
            log.exception("post-refinement write failed; transcript unchanged")
        # Surface the result so the UI can pop the Label Unknown Speakers
        # dialog. Emit before finalize so the dialog can grab a fresh
        # sample clip from the loopback WAV (still on disk).
        self.speaker_refinement_done.emit(session_id, result)
        self._finalize_session(session_id, batch_segments=None)

    def _on_refinement_skipped(self, session_id: str, reason: str) -> None:
        proc_state = self._processing_sessions.get(session_id)
        if proc_state is not None:
            _retire_thread(proc_state.refinement_thread)
            proc_state.refinement_thread = None
        log.info("speaker refinement skipped for %s: %s", session_id, reason)
        self.speaker_refinement_skipped.emit(session_id, reason)
        self._finalize_session(session_id, batch_segments=None)

    # ---- unified post-Stop progress bar ------------------------------------

    def _build_phase_plan(
        self,
        *,
        will_run_batch: bool,
        will_run_refinement: bool,
    ) -> dict[str, tuple[int, int]]:
        """Allocate slices of 0..100 across whichever phases will run.

        Both phases: batch 0-70%, refinement 70-100% (roughly matches the
        observed wall-clock split on small.en for a typical meeting).
        Single-phase cases give that phase the whole bar so 100% always
        coincides with "actually done."
        """
        if will_run_batch and will_run_refinement:
            return {"batch": (0, 70), "refinement": (70, 100)}
        if will_run_batch:
            return {"batch": (0, 100)}
        if will_run_refinement:
            return {"refinement": (0, 100)}
        return {}

    def _emit_unified_progress(self, session_id: str, phase: str, raw_pct: int) -> None:
        """Map a phase-local 0..100 to the unified 0..100 bar and emit."""
        plan = self._phase_plans.get(session_id)
        if plan is None:
            # No plan recorded (e.g. session was never queued through stop).
            # Fall through with the raw value as a safety net.
            self.batch_progress.emit(session_id, max(0, min(100, int(raw_pct))))
            return
        slot = plan.get(phase)
        if slot is None:
            return
        lo, hi = slot
        raw_pct = max(0, min(100, int(raw_pct)))
        unified = lo + int(round((hi - lo) * raw_pct / 100))
        self.batch_progress.emit(session_id, unified)

    def _self_improve_store_from_tagged_clusters(self, result: RefinementResult) -> None:
        """Feed user-tagged cluster centroids into the cross-meeting
        SpeakerStore. The tag turns each cluster into a labelled
        sample that updates the speaker's running-average centroid
        (creating a new entry if the name was previously unknown).
        Untagged clusters are left alone -- they go through the walker
        and are persisted there via _apply_walker_decisions.
        """
        tagged = [
            c for c in result.clusters
            if c.was_user_tagged and c.name and c.centroid is not None
        ]
        if not tagged:
            return
        try:
            store = open_speaker_store()
        except Exception:
            log.exception("could not open speaker store for self-improvement")
            return
        try:
            for cluster in tagged:
                try:
                    store.add_sample(cluster.name, cluster.centroid)
                except Exception:
                    log.exception(
                        "failed to add tagged-cluster sample for %r",
                        cluster.name,
                    )
        finally:
            store.close()

    def _on_refinement_failed(self, session_id: str, msg: str) -> None:
        proc_state = self._processing_sessions.get(session_id)
        if proc_state is not None:
            _retire_thread(proc_state.refinement_thread)
            proc_state.refinement_thread = None
        log.warning("speaker refinement failed for %s: %s", session_id, msg)
        # Don't fail the whole session over a refinement bug; just skip
        # the labeling step and finalize normally.
        self.speaker_refinement_skipped.emit(session_id, f"failed: {msg}")
        self._finalize_session(session_id, batch_segments=None)

    def _on_batch_failed(self, session_id: str, msg: str) -> None:
        proc_state = self._processing_sessions.pop(session_id, None)
        self._phase_plans.pop(session_id, None)
        if proc_state is not None:
            _retire_thread(proc_state.batch_thread)
            proc_state.batch_thread = None
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

    # ---- mid-recording silence detection (#84) --------------------------

    def _on_loopback_silence_detected(self, session_id: str) -> None:
        """The loopback's rolling-window RMS dropped below floor for the
        full window. Compose a user-facing message that names a meeting
        app currently rendering audio elsewhere, if one is active --
        that is the diagnostic the user needs to know which knob to
        change (Teams output device, Windows default, recorder
        endpoint). Falls back to a generic copy when no cross-reference
        signal is available."""
        try:
            from .integrations import audio_session_monitor
            sessions = audio_session_monitor.enumerate_active_sessions()
        except Exception:
            log.exception("audio_session_monitor lookup failed during silence event")
            sessions = []
        # Filter to known meeting apps; the rest aren't load-bearing
        # for the user-facing diagnosis (a YouTube tab playing music
        # elsewhere is noise, not signal).
        try:
            from .integrations.audio_session_monitor import (
                DEFAULT_APP_ALLOWLIST,
                display_name_for,
            )
            allow = {a.lower() for a in DEFAULT_APP_ALLOWLIST}
            meeting_apps = sorted({
                display_name_for(s.process_name)
                for s in sessions
                if s.process_name.lower() in allow
            })
        except Exception:
            meeting_apps = []
        if meeting_apps:
            apps_label = ", ".join(meeting_apps)
            msg = (
                f"System audio appears silent for the past 30 seconds, but "
                f"{apps_label} is rendering audio. The recorder may be "
                f"bound to a different output device. Check your meeting "
                f"app's output device in its audio settings."
            )
        else:
            msg = (
                "System audio appears silent for the past 30 seconds. If "
                "you expect audio to be playing, check the output device "
                "in your meeting app's audio settings."
            )
        log.info("loopback silence_detected for %s: %s", session_id, msg)
        self.capture_warning.emit(session_id, msg)

    # ---- hot-plug endpoint changes (#85.6) ------------------------------

    def _on_endpoints_changed(self, change) -> None:
        """Forward an endpoint-set change to the multi-endpoint
        orchestrator. Only the `added` set drives action -- a removed
        endpoint stays in the capture set so its tail silence is
        captured + padded at finalize (the sub-recorder will detect
        the device gone via stalled callbacks and warn through the
        existing capture_warning path)."""
        from .audio.multi_loopback import MultiEndpointLoopbackRecorder
        if not isinstance(self._loopback_recorder, MultiEndpointLoopbackRecorder):
            return
        for name in change.added:
            ok = self._loopback_recorder.extend_to_endpoint(name)
            log.info(
                "controller: hot-plug extend_to_endpoint(%r) -> %s",
                name, ok,
            )

    def _on_loopback_silence_cleared(self, session_id: str) -> None:
        """The loopback callback started receiving above-floor audio
        again. Used today for log diagnostics; SessionView's banner
        is one-shot (the user can dismiss the warning manually) so we
        don't auto-clear from here."""
        log.info("loopback silence_cleared for %s", session_id)

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
