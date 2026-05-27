"""Mixed-stream audio player for the Transcript pane.

Loads the session's retained mic + sys files, decodes them into a
single mixed mono buffer at 16 kHz, and plays back via sounddevice.
Exposes Qt signals so the SessionView can highlight the active
transcript segment as playback progresses.

Why decode-into-memory instead of streaming-from-disk:

* The codebase already pulls PyAV (decode) + numpy + sounddevice
  (playback) into the bundle; the existing path matches the bundle
  shape.
* Seeking is the primary playback operation (click a line, jump 10s
  back). A pre-decoded array gives sample-accurate O(1) seeks; the
  streaming alternative has to manage decoder state across seeks,
  with codec-specific behavior on partial-block jumps.
* The on-disk audio for a typical meeting (1-2 hours) decodes to a
  ~150-300 MB int16 buffer at 16 kHz mono. The user's machine has
  the headroom; the entire faster-whisper model weight is the same
  order of magnitude.

The mix is a clip-safe average: at each sample, mixed = (mic + sys) / 2.
Each source is float32 in [-1, 1] coming out of PyAV's AudioResampler;
the average stays in [-1, 1] which converts to int16 without clipping.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

import numpy as np
from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

log = logging.getLogger(__name__)


# 16 kHz mono is enough resolution for spoken-word playback and keeps
# the in-memory buffer small. Stereo here would buy nothing -- both
# the mic source (mono) and the sys-downmix are mono in the final
# mix anyway.
_PLAYBACK_RATE = 16000


class AudioPlayer(QObject):
    """Wraps sounddevice + a pre-decoded mixed buffer.

    Signal contract:

    * `position_changed(int_ms)`: emitted ~10x/sec while playing or
      immediately after a seek. Carries the current playback position
      in milliseconds.
    * `playback_finished()`: emitted exactly once when the stream
      reaches the end of the buffer. The SessionView responds by
      flipping the play button back to "Play".
    * `loaded(int_total_ms)`: emitted after load() finishes; the
      transcript bar resizes its slider against this.
    """

    position_changed = pyqtSignal(int)
    playback_finished = pyqtSignal()
    loaded = pyqtSignal(int)
    load_failed = pyqtSignal(str)  # error message

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        # The mixed buffer as float32 PCM in [-1, 1], mono. None when
        # no session is loaded.
        self._buffer: Optional[np.ndarray] = None
        # Playback cursor in frames (int). Read/written by the
        # audio callback thread; main thread reads it on the
        # position-tick timer. Single-int reads/writes are atomic in
        # CPython, so no lock is required for the cursor itself.
        self._cursor_frames: int = 0
        self._total_frames: int = 0
        self._stream = None  # sounddevice OutputStream
        # Increments each time we build a stream. The finished_callback
        # closes over its generation id; the handler ignores callbacks
        # whose generation is stale (i.e. a newer stream has replaced
        # the one being reported about). This is the fix for the
        # "click in transcript pauses playback" regression: stop() on
        # the old stream queues a finished_callback that lands AFTER
        # seek_ms has already built and started a new stream, and the
        # handler would otherwise tear that new stream down.
        self._stream_generation: int = 0
        # Diagnostic-only: count callbacks since the last play() so we
        # can log just the first few invocations without flooding.
        self._callbacks_logged: int = 0
        self._lock = threading.Lock()  # protects _stream lifecycle
        # 100 ms tick for position updates. Runs while playing; one-
        # shot after a seek so the UI snaps to the new position even
        # if playback is paused.
        self._tick = QTimer(self)
        self._tick.setInterval(100)
        self._tick.timeout.connect(self._emit_position)
        # Asynchronous decode worker state. PyAV decode of a 1-2 hour
        # mono WAV runs 1-3 seconds on a typical machine, plenty of
        # wall-clock time to lock the Qt event loop and trip Windows'
        # "Not Responding" overlay (bug #31). The decode now runs on
        # a worker QThread; load() returns immediately and the
        # loaded / load_failed signals fire when the worker finishes.
        #
        # Generation counter: every load() bumps it and the new worker
        # captures the new value. Worker callbacks check the captured
        # value against the current one before mutating state, so a
        # rapid session-switch sequence (load A -> load B before A
        # finishes) lands B's buffer and drops A's even if A's decode
        # completes later.
        self._load_generation: int = 0
        self._load_worker: Optional[_DecodeWorker] = None

    # ------------------------------------------------------------------
    # Public API

    def load(self, mic_path: Optional[Path], sys_path: Optional[Path]) -> None:
        """Kick off an asynchronous decode + mix of the session's audio.

        Returns immediately. The PyAV decode + AudioResampler + mix
        runs on a worker QThread and emits `loaded(total_ms)` on
        success or `load_failed(message)` on error -- both delivered
        back on the main thread via the standard QObject signal
        machinery.

        Either side may be None / absent (mic-only or sys-only
        session); the missing side just doesn't contribute.

        Calling load() again before the previous decode finishes
        cancels the previous load: its result is dropped via a
        generation check, and the new worker takes its place. This
        keeps fast session-switching cheap -- the user clicking
        through 5 sessions in 5 seconds doesn't queue 5 decodes
        worth of work; only the last load's result is delivered.
        """
        # Bump the generation FIRST so any in-flight worker's emit
        # is dropped before we tear down playback. Otherwise the
        # in-flight worker could land its result after we cleared
        # the buffer here.
        self._load_generation += 1
        my_generation = self._load_generation
        self._stop_stream()
        self._buffer = None
        self._cursor_frames = 0
        self._total_frames = 0
        sources = [
            p for p in (mic_path, sys_path)
            if p is not None and p.exists() and p.stat().st_size > 0
        ]
        if not sources:
            # Synchronous failure -- no decode needed. Emit on the
            # next event-loop tick so the contract (load_failed is
            # never re-entrant from load()) is preserved.
            QTimer.singleShot(
                0,
                lambda g=my_generation: self._emit_load_failed_if_current(
                    g, "No audio files on disk for this session.",
                ),
            )
            return
        # Hand the previous worker off to deleteLater + let it run
        # to completion on its own; the generation check drops its
        # result. wait() would block the main thread for the rest
        # of the prior decode -- which is exactly what we're trying
        # to avoid.
        old_worker = self._load_worker
        if old_worker is not None:
            old_worker.requestInterruption()
        # The worker is intentionally NOT parented to the player so it
        # can outlive the player if the player is destroyed mid-decode
        # (e.g. main window closing while a session is loading). Qt
        # destroys child QThreads when the parent dies; if the OS
        # thread is still running that's an abort, not a graceful
        # shutdown. The worker keeps itself alive via the finished
        # signal connection and tears down via _safe_cleanup_thread.
        worker = _DecodeWorker(
            sources=sources,
            target_rate=_PLAYBACK_RATE,
            generation=my_generation,
        )
        worker.setObjectName(f"AudioPlayer.DecodeWorker[{my_generation}]")
        worker.decoded.connect(self._on_decode_done)
        # finished -> deleteLater via a wait-then-cleanup helper so
        # the OS thread is joined before the QObject is destroyed.
        worker.finished.connect(lambda w=worker: _safe_cleanup_thread(w))
        self._load_worker = worker
        worker.start()

    def close(self) -> None:
        """Stop playback (if any) and drop the loaded buffer.

        Also invalidates any in-flight decode so its result -- if it
        lands after close() returns -- is dropped.
        """
        self._load_generation += 1
        worker = self._load_worker
        if worker is not None:
            worker.requestInterruption()
            self._load_worker = None
        self._stop_stream()
        self._buffer = None
        self._cursor_frames = 0
        self._total_frames = 0

    def _on_decode_done(
        self,
        generation: int,
        mixed: Optional[np.ndarray],
        error: Optional[str],
    ) -> None:
        """Worker-result slot. Runs on the main thread."""
        if generation != self._load_generation:
            log.debug(
                "AudioPlayer: dropping stale decode result "
                "(generation=%d, current=%d)",
                generation, self._load_generation,
            )
            return
        # Worker is one-shot; clear our reference so close() doesn't
        # try to interrupt an already-finished thread.
        self._load_worker = None
        if error is not None or mixed is None:
            log.warning("AudioPlayer.load failed: %s", error)
            self.load_failed.emit(error or "decode produced no samples")
            return
        self._buffer = mixed
        self._total_frames = int(mixed.size)
        self._cursor_frames = 0
        log.info(
            "AudioPlayer: loaded %d samples (%.1f s)",
            self._total_frames,
            self._total_frames / _PLAYBACK_RATE if _PLAYBACK_RATE else 0,
        )
        self.loaded.emit(self.total_ms())

    def _emit_load_failed_if_current(
        self, generation: int, message: str,
    ) -> None:
        if generation == self._load_generation:
            self.load_failed.emit(message)

    def play(self) -> None:
        """Start playback from the current cursor."""
        if self._buffer is None or self._total_frames == 0:
            return
        if self._cursor_frames >= self._total_frames:
            # Restart from the beginning -- the user hit Play after
            # the stream reached EOF.
            self._cursor_frames = 0
        log.info(
            "AudioPlayer: play from frame=%d (pos=%d ms / total=%d ms)",
            self._cursor_frames, self.position_ms(), self.total_ms(),
        )
        self._start_stream()

    def pause(self) -> None:
        self._stop_stream()
        # One last position emit so the bar matches reality after the
        # stream stops between ticks.
        self._emit_position()

    def seek_ms(self, ms: int) -> None:
        if self._buffer is None or self._total_frames == 0:
            return
        was_playing = self.is_playing()
        if was_playing:
            self._stop_stream()
        target = max(0, min(self._total_frames - 1, int(ms * _PLAYBACK_RATE / 1000)))
        log.debug(
            "AudioPlayer: seek to %d ms (frame=%d, was_playing=%s)",
            ms, target, was_playing,
        )
        self._cursor_frames = target
        if was_playing:
            # _start_stream restarts the tick too -- avoids a
            # regression where seek-during-playback stops position
            # updates because _stop_stream paired its tick.stop() and
            # _start_stream didn't restore.
            self._start_stream()
        self._emit_position()

    def is_playing(self) -> bool:
        with self._lock:
            return self._stream is not None and self._stream.active

    def total_ms(self) -> int:
        if self._total_frames == 0:
            return 0
        return int(self._total_frames * 1000 / _PLAYBACK_RATE)

    def position_ms(self) -> int:
        if self._total_frames == 0:
            return 0
        return int(self._cursor_frames * 1000 / _PLAYBACK_RATE)

    # ------------------------------------------------------------------
    # Internals

    def _start_stream(self) -> None:
        """Construct + start the sounddevice OutputStream and the tick.

        Tick start lives here (not in play()) so any code path that
        starts a stream -- play(), seek_ms during playback -- restarts
        position updates. The previous shape paired tick.stop() inside
        _stop_stream but only put tick.start() in play(); seek_ms
        cycled the stream and the tick stayed dead.

        Each stream gets a unique generation id. The finished_callback
        closes over it; the main-thread handler compares against the
        current generation and drops stale callbacks. Without this,
        a stop() of the old stream + immediate _start_stream race
        causes the queued finished_callback to tear down the new
        stream.
        """
        with self._lock:
            if self._stream is not None:
                self._tick.start()  # be defensive; idempotent if already running
                return
            self._stream_generation += 1
            my_generation = self._stream_generation
            self._callbacks_logged = 0
            import sounddevice as sd  # noqa: PLC0415
            try:
                self._stream = sd.OutputStream(
                    samplerate=_PLAYBACK_RATE,
                    channels=1,
                    dtype="float32",
                    callback=self._audio_callback,
                    finished_callback=lambda g=my_generation: self._on_stream_finished(g),
                )
                self._stream.start()
                log.info(
                    "AudioPlayer: stream started (generation=%d, rate=%d, "
                    "blocksize=%s)",
                    my_generation, _PLAYBACK_RATE, self._stream.blocksize,
                )
            except Exception:
                log.exception("AudioPlayer: failed to start sounddevice stream")
                self._stream = None
                return
        self._tick.start()

    def _stop_stream(self) -> None:
        with self._lock:
            if self._stream is None:
                return
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                log.exception("AudioPlayer: stream stop failed")
            self._stream = None
        self._tick.stop()

    def _audio_callback(self, outdata, frames, time_info, status) -> None:
        # Called on sounddevice's PortAudio thread. Must NOT do Qt I/O
        # here -- emit only via the QTimer tick on the main thread.
        # Diagnostic logging the first few calls so playback issues
        # are debuggable from the log alone.
        if self._buffer is None or self._total_frames == 0:
            outdata[:] = 0.0
            if self._callbacks_logged < 3:
                log.info(
                    "AudioPlayer cb: empty buffer (frames=%d, total_frames=%d)",
                    frames, self._total_frames,
                )
                self._callbacks_logged += 1
            return
        start = self._cursor_frames
        end = start + frames
        chunk = self._buffer[start:end]
        if self._callbacks_logged < 3:
            log.info(
                "AudioPlayer cb #%d: frames=%d cursor=%d/%d chunk=%d "
                "status=%s outdata_shape=%s buffer_dtype=%s",
                self._callbacks_logged, frames, start, self._total_frames,
                chunk.size, status, outdata.shape, self._buffer.dtype,
            )
            self._callbacks_logged += 1
        if chunk.size < frames:
            outdata[: chunk.size, 0] = chunk
            outdata[chunk.size :, 0] = 0.0
            self._cursor_frames = self._total_frames
            log.info(
                "AudioPlayer cb: buffer drained at frame=%d/%d (chunk=%d, "
                "requested=%d); raising CallbackStop",
                start, self._total_frames, chunk.size, frames,
            )
            # Signal end-of-stream to PortAudio. CallbackStop drains
            # the device and calls finished_callback once cleanly.
            # Without this, the callback would keep producing silence
            # forever after the buffer ends; the tick would keep
            # firing and the UI would stay in "playing" state.
            import sounddevice as sd  # noqa: PLC0415
            raise sd.CallbackStop
        outdata[:, 0] = chunk
        self._cursor_frames = end

    def _on_stream_finished(self, generation: int) -> None:
        # Called on the PortAudio thread after stop()/close(). Use a
        # zero-delay QTimer to bounce onto the main thread before
        # emitting Qt signals. The generation id is passed through
        # so the main-thread handler can drop stale callbacks.
        QTimer.singleShot(
            0,
            lambda g=generation: self._handle_stream_finished_on_main_thread(g),
        )

    def _handle_stream_finished_on_main_thread(self, generation: int) -> None:
        """Tear down the stream after sounddevice signals end-of-stream.

        Fires from two paths:
        * User-initiated stop via _stop_stream (sounddevice's stop()
          calls finished_callback).
        * Buffer drained via CallbackStop in _audio_callback.

        In both cases the stream is dead; clear our reference so a
        subsequent play() builds a new one. Only emit
        playback_finished in the drained case (Stop already updated
        the UI on the way down).

        The generation check is the load-bearing piece for the
        seek-during-playback fix: stop() on the old stream queues a
        finished_callback that arrives AFTER seek_ms has already
        built a new stream. Without the guard, the queued handler
        would close the new stream we just started.
        """
        if generation != self._stream_generation:
            log.debug(
                "AudioPlayer: dropping stale finished callback "
                "(generation=%d, current=%d)",
                generation, self._stream_generation,
            )
            return
        finished_via_drain = (
            self._cursor_frames >= self._total_frames
            and self._total_frames > 0
        )
        log.info(
            "AudioPlayer: stream finished (generation=%d, drained=%s, "
            "cursor=%d, total=%d)",
            generation, finished_via_drain,
            self._cursor_frames, self._total_frames,
        )
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.close()
                except Exception:
                    log.exception("AudioPlayer: stream close after finish failed")
                self._stream = None
        self._tick.stop()
        if finished_via_drain:
            self.playback_finished.emit()
        self._emit_position()

    def _emit_position(self) -> None:
        self.position_changed.emit(self.position_ms())


class _DecodeWorker(QThread):
    """Decode + mix the session's mic / sys audio on a background thread.

    Emits `decoded(generation, mixed_or_none, error_or_none)` exactly
    once when finished. Cancellable mid-decode via the standard
    QThread.requestInterruption(); the run loop checks between
    sources, so a cancellation lands at most one decode-of-one-source
    late. We don't try to check inside _decode_to_mono_float32 because
    PyAV's decode loop is a tight C loop -- the cost of bailing
    halfway through one source is acceptable (sub-second) and the
    win in code clarity is worth it.
    """

    # arguments: (generation, mixed_float32_or_None, error_message_or_None)
    decoded = pyqtSignal(int, object, object)

    def __init__(
        self,
        *,
        sources: list[Path],
        target_rate: int,
        generation: int,
    ) -> None:
        super().__init__(None)
        self._sources = list(sources)
        self._target_rate = target_rate
        self._generation = generation

    def run(self) -> None:
        try:
            decoded: list[np.ndarray] = []
            for src in self._sources:
                if self.isInterruptionRequested():
                    self.decoded.emit(self._generation, None, "load cancelled")
                    return
                decoded.append(_decode_to_mono_float32(src, self._target_rate))
            if self.isInterruptionRequested():
                self.decoded.emit(self._generation, None, "load cancelled")
                return
            mixed = _mix_to_max_length(decoded).astype(np.float32, copy=False)
            np.clip(mixed, -1.0, 1.0, out=mixed)
            self.decoded.emit(self._generation, mixed, None)
        except Exception as exc:
            log.exception("AudioPlayer decode worker failed")
            self.decoded.emit(self._generation, None, f"Could not decode audio: {exc}")


def _safe_cleanup_thread(worker: QThread) -> None:
    """Join the OS thread before destroying the QObject.

    QThread.finished fires inside the run-wrapper before the OS
    thread has actually been joined; calling deleteLater() directly
    from that slot races against the OS-thread teardown and trips
    'QThread: Destroyed while thread is still running' on Windows
    (the same race we fixed across the export path in v0.7.0).
    """
    worker.wait()
    worker.deleteLater()


def _decode_to_mono_float32(src: Path, target_rate: int) -> np.ndarray:
    """Walk src through PyAV + AudioResampler -> mono float32 samples.

    Shared shape with audio/export._decode_to_mono_float32 but kept
    local so the two modules can evolve independently (the player's
    rate target stays at 16k for memory efficiency, while the
    exporter may grow per-format target rates).
    """
    import av  # noqa: PLC0415

    chunks: list[np.ndarray] = []
    with av.open(str(src)) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(
            format="fltp", layout="mono", rate=target_rate,
        )
        for frame in container.decode(stream):
            for rs in resampler.resample(frame):
                chunks.append(rs.to_ndarray().reshape(-1).astype(np.float32))
        for rs in resampler.resample(None) or []:
            chunks.append(rs.to_ndarray().reshape(-1).astype(np.float32))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks)


def _mix_to_max_length(buffers: list[np.ndarray]) -> np.ndarray:
    """Sum 1+ mono float32 buffers, end-aligned with leading silence.

    Recorders stop simultaneously (controller.stop_session synchronously
    stops both). A shorter buffer therefore means the recorder STARTED
    later -- typical with WASAPI loopback, which may not deliver
    samples until something actually plays through the speakers.
    Leading-zero padding aligns the END of each buffer with the
    merged track's end, so a system-audio segment that began 30 s
    into a 60 s meeting plays back at the 30 s mark instead of the
    0 s mark.

    Pre-v0.6.5 this padded TRAILING zeros, which dragged any short
    sys-audio segment to the front of the timeline -- the alignment
    bug Aaron flagged.
    """
    if len(buffers) == 1:
        return buffers[0]
    max_len = max(b.size for b in buffers)
    acc = np.zeros(max_len, dtype=np.float32)
    for b in buffers:
        if b.size < max_len:
            pad = np.zeros(max_len, dtype=np.float32)
            pad[-b.size:] = b
            acc += pad
        else:
            acc += b
    return acc / len(buffers)
