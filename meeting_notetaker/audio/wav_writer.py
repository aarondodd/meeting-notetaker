"""Background WAV writer for audio-capture callbacks.

PortAudio callback threads must complete in well under the buffer
duration -- 21 ms for our typical 1024 frames at 48 kHz -- or
PortAudio drops samples. Calling wave.Wave_write.writeframes()
directly from the callback is fine on a quiet system, but Windows
disk write latency is bursty: antivirus scans, filesystem cache
pressure as the WAV grows past a few hundred MB, and concurrent I/O
from the rest of the app can all stall a write for hundreds of
milliseconds. When that happens, the next several callbacks miss
their deadlines and the audio corrupts -- the "choppy starting at
~10-15 min" symptom from issue #31.

AsyncWavWriter moves the disk write to a dedicated daemon thread.
The callback thread pushes raw int16 bytes onto a bounded queue and
returns immediately. The writer thread drains the queue and calls
writeframes() at its own pace. The queue is bounded generously
(~30 s worth of audio) so transient write stalls don't trip the
drop path; if the writer ever falls that far behind something is
catastrophically wrong with the disk subsystem and dropping further
frames is better than letting RAM grow unbounded.

The same machinery handles silence-fill writes (for WASAPI
loopback gaps): the callback pushes a `("silence", frame_count)`
item rather than materializing a giant zero-byte buffer on the
audio thread.
"""
from __future__ import annotations

import logging
import queue
import threading
import wave
from pathlib import Path
from typing import Optional, Tuple, Union


log = logging.getLogger(__name__)


# Per-item the queue holds either raw PCM bytes or a silence-frame count.
# Tuple discriminator makes the writer loop trivial to read.
_QueueItem = Union[Tuple[str, bytes], Tuple[str, int], None]


# Bound the queue at roughly 30 seconds of audio for the largest typical
# capture (48 kHz stereo int16 = 192 KB/s -> ~5.7 MB). At 48 kHz mono mic
# that's ~60 s. Plenty of headroom; if we hit it the disk has stopped
# responding entirely and dropping further frames is the lesser evil.
_MAX_QUEUED_ITEMS = 2048


# When materializing silence on the writer thread, write in chunks so a
# very long gap doesn't allocate a giant byte string.
_SILENCE_BLOCK_FRAMES = 8192


class AsyncWavWriter:
    """Wraps wave.Wave_write with a background writer thread.

    Construct with the destination path + WAV format, call start(),
    push frames from the audio callback via write_frames() /
    write_silence_frames(), and call close() when capture ends.
    close() flushes the queue, joins the writer, and finalizes the
    WAV header.

    Thread-safety: write_frames + write_silence_frames are safe from
    any thread (designed for the audio callback). start + close are
    expected from the owning recorder's main control flow. Concurrent
    close calls are serialized via the internal lock.
    """

    def __init__(
        self,
        wav_path: Path,
        *,
        channels: int,
        sample_width: int,
        sample_rate: int,
    ) -> None:
        self.wav_path = Path(wav_path)
        self._channels = int(channels)
        self._sample_width = int(sample_width)
        self._sample_rate = int(sample_rate)
        self._wf: Optional[wave.Wave_write] = None
        self._queue: queue.Queue[_QueueItem] = queue.Queue(maxsize=_MAX_QUEUED_ITEMS)
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._dropped_frames: int = 0
        self._dropped_logs: int = 0  # count of "queue full" log lines emitted
        self._closed = False

    # ---- lifecycle ----------------------------------------------------

    def start(self) -> None:
        """Open the WAV and start the writer thread."""
        self.wav_path.parent.mkdir(parents=True, exist_ok=True)
        self._wf = wave.open(str(self.wav_path), "wb")
        self._wf.setnchannels(self._channels)
        self._wf.setsampwidth(self._sample_width)
        self._wf.setframerate(self._sample_rate)
        # daemon=True so a stuck writer doesn't keep the process alive
        # at shutdown. The OS will reclaim the FD; we lose the WAV
        # header finalization, which is acceptable on emergency exit.
        self._thread = threading.Thread(
            target=self._writer_loop,
            name=f"AsyncWavWriter[{self.wav_path.name}]",
            daemon=True,
        )
        self._thread.start()

    def close(self, timeout: float = 5.0) -> None:
        """Drain the queue, join the writer, finalize the WAV."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        # Sentinel tells the writer loop to exit. put() blocks if the
        # queue is full -- that's intentional here, we want close() to
        # wait for the backlog to clear.
        try:
            self._queue.put(None, timeout=timeout)
        except queue.Full:
            log.warning(
                "AsyncWavWriter[%s]: queue still full at close; "
                "forcing writer thread exit",
                self.wav_path.name,
            )
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                log.warning(
                    "AsyncWavWriter[%s]: writer thread didn't exit "
                    "within %.1fs; abandoning",
                    self.wav_path.name, timeout,
                )
            self._thread = None
        # Close the WAV file under the lock so a late put_nowait from
        # the audio callback doesn't see a half-closed file.
        with self._lock:
            if self._wf is not None:
                try:
                    self._wf.close()
                except Exception:
                    log.exception(
                        "AsyncWavWriter[%s]: wave.close failed",
                        self.wav_path.name,
                    )
                self._wf = None
        if self._dropped_frames:
            log.warning(
                "AsyncWavWriter[%s]: total dropped frames during capture: %d",
                self.wav_path.name, self._dropped_frames,
            )

    # ---- audio-callback API ------------------------------------------

    def write_frames(self, data: bytes) -> bool:
        """Push raw PCM bytes onto the writer queue. Non-blocking.

        Returns True if queued, False if the queue is full (in which
        case the frames are dropped to keep the callback unblocked).
        """
        if self._closed:
            return False
        try:
            self._queue.put_nowait(("data", data))
            return True
        except queue.Full:
            self._record_drop(len(data))
            return False

    def write_silence_frames(self, frame_count: int) -> bool:
        """Push N frames of silence onto the writer queue. Non-blocking."""
        if self._closed or frame_count <= 0:
            return False
        try:
            self._queue.put_nowait(("silence", int(frame_count)))
            return True
        except queue.Full:
            bytes_per_frame = self._channels * self._sample_width
            self._record_drop(frame_count * bytes_per_frame)
            return False

    # ---- diagnostics --------------------------------------------------

    @property
    def dropped_frame_bytes(self) -> int:
        """Total bytes worth of audio dropped to keep the callback fast."""
        return self._dropped_frames

    def queue_depth(self) -> int:
        return self._queue.qsize()

    # ---- internals ----------------------------------------------------

    def _record_drop(self, byte_count: int) -> None:
        self._dropped_frames += byte_count
        # Log the first 5 drops then every 100th to avoid log spam.
        if self._dropped_logs < 5 or self._dropped_logs % 100 == 0:
            log.warning(
                "AsyncWavWriter[%s]: write queue full (depth=%d), "
                "dropping %d bytes -- writer thread is falling "
                "behind the audio callback",
                self.wav_path.name, self._queue.qsize(), byte_count,
            )
        self._dropped_logs += 1

    def _writer_loop(self) -> None:
        bytes_per_frame = self._channels * self._sample_width
        silence_block = b"\x00" * (_SILENCE_BLOCK_FRAMES * bytes_per_frame)
        while True:
            try:
                item = self._queue.get()
            except Exception:
                log.exception(
                    "AsyncWavWriter[%s]: queue.get failed",
                    self.wav_path.name,
                )
                continue
            if item is None:
                # Sentinel -- drain whatever is still queued, then exit.
                # We don't want to drop tail-end frames just because the
                # owner pushed the sentinel before all data items.
                # queue.get() returns sentinel FIFO so anything queued
                # before it has already been written; nothing to do.
                return
            kind, payload = item
            try:
                with self._lock:
                    if self._wf is None:
                        continue
                    if kind == "data":
                        self._wf.writeframes(payload)
                    elif kind == "silence":
                        remaining = int(payload)
                        while remaining >= _SILENCE_BLOCK_FRAMES:
                            self._wf.writeframes(silence_block)
                            remaining -= _SILENCE_BLOCK_FRAMES
                        if remaining > 0:
                            self._wf.writeframes(b"\x00" * (remaining * bytes_per_frame))
            except Exception:
                log.exception(
                    "AsyncWavWriter[%s]: writeframes failed; "
                    "continuing to drain queue",
                    self.wav_path.name,
                )
