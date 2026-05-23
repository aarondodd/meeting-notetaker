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
from PyQt6.QtCore import QObject, QTimer, pyqtSignal

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
        # The mixed buffer as int16 PCM, mono. None when no session
        # is loaded.
        self._buffer: Optional[np.ndarray] = None
        # Playback cursor in frames (int). Read/written by the
        # audio callback thread; main thread reads it on the
        # position-tick timer. Single-int reads/writes are atomic in
        # CPython, so no lock is required for the cursor itself.
        self._cursor_frames: int = 0
        self._total_frames: int = 0
        self._stream = None  # sounddevice OutputStream
        self._lock = threading.Lock()  # protects _stream lifecycle
        # 100 ms tick for position updates. Runs while playing; one-
        # shot after a seek so the UI snaps to the new position even
        # if playback is paused.
        self._tick = QTimer(self)
        self._tick.setInterval(100)
        self._tick.timeout.connect(self._emit_position)

    # ------------------------------------------------------------------
    # Public API

    def load(self, mic_path: Optional[Path], sys_path: Optional[Path]) -> None:
        """Decode + mix the session's audio into the in-memory buffer.

        Either side may be None / absent (mic-only or sys-only
        session); the missing side just doesn't contribute. Emits
        loaded(total_ms) on success or load_failed(message) on error.
        Replaces any previously-loaded session.
        """
        self.close()
        sources = [
            p for p in (mic_path, sys_path)
            if p is not None and p.exists() and p.stat().st_size > 0
        ]
        if not sources:
            self.load_failed.emit("No audio files on disk for this session.")
            return
        try:
            decoded = [_decode_to_mono_float32(p, _PLAYBACK_RATE) for p in sources]
        except Exception as exc:
            log.exception("AudioPlayer.load: decode failed")
            self.load_failed.emit(f"Could not decode audio: {exc}")
            return
        mixed_float = _mix_to_max_length(decoded)
        # Convert to int16 once; sounddevice handles int16 natively
        # and the float32 buffer would be 2x the memory.
        mixed_int16 = np.clip(mixed_float * 32767, -32768, 32767).astype(np.int16)
        self._buffer = mixed_int16
        self._total_frames = int(mixed_int16.size)
        self._cursor_frames = 0
        self.loaded.emit(self.total_ms())

    def close(self) -> None:
        """Stop playback (if any) and drop the loaded buffer."""
        self._stop_stream()
        self._buffer = None
        self._cursor_frames = 0
        self._total_frames = 0

    def play(self) -> None:
        """Start playback from the current cursor."""
        if self._buffer is None or self._total_frames == 0:
            return
        if self._cursor_frames >= self._total_frames:
            # Restart from the beginning -- the user hit Play after
            # the stream reached EOF.
            self._cursor_frames = 0
        self._start_stream()
        self._tick.start()

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
        self._cursor_frames = target
        if was_playing:
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
        with self._lock:
            if self._stream is not None:
                return
            import sounddevice as sd  # noqa: PLC0415
            self._stream = sd.OutputStream(
                samplerate=_PLAYBACK_RATE,
                channels=1,
                dtype="int16",
                callback=self._audio_callback,
                finished_callback=self._on_stream_finished,
            )
            self._stream.start()

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
        if self._buffer is None:
            outdata[:] = 0
            return
        start = self._cursor_frames
        end = start + frames
        chunk = self._buffer[start:end]
        if chunk.size < frames:
            outdata[: chunk.size, 0] = chunk
            outdata[chunk.size :, 0] = 0
            self._cursor_frames = self._total_frames
            # Don't raise CallbackStop here; let _on_stream_finished
            # land naturally after the buffer drains.
        else:
            outdata[:, 0] = chunk
            self._cursor_frames = end

    def _on_stream_finished(self) -> None:
        # Called on the PortAudio thread after stop()/close(). Use a
        # zero-delay QTimer to bounce onto the main thread before
        # emitting Qt signals.
        QTimer.singleShot(0, self._handle_stream_finished_on_main_thread)

    def _handle_stream_finished_on_main_thread(self) -> None:
        # Stream stopped: either user-paused, or playback ran past
        # the end. We only fire playback_finished in the run-past-end
        # case so the UI knows to flip Play <-> Pause.
        if self._cursor_frames >= self._total_frames and self._total_frames > 0:
            self.playback_finished.emit()
            self._tick.stop()
            self._emit_position()

    def _emit_position(self) -> None:
        self.position_changed.emit(self.position_ms())


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
    """Sum 1+ mono float32 buffers, padded to longest, attenuated."""
    if len(buffers) == 1:
        return buffers[0]
    max_len = max(b.size for b in buffers)
    acc = np.zeros(max_len, dtype=np.float32)
    for b in buffers:
        if b.size < max_len:
            pad = np.zeros(max_len, dtype=np.float32)
            pad[: b.size] = b
            acc += pad
        else:
            acc += b
    return acc / len(buffers)
