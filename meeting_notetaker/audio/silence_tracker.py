"""Rolling-window silence detector for live capture streams.

Tracks per-stream RMS over a configurable wall-clock window. Reports
silence when every chunk in the window stays below a tiny amplitude
floor. Designed to run inside an audio callback hot path, so each
write is O(1) amortized: we keep a deque of (timestamp, sum_sq, count)
chunks and recompute the rolling RMS from those summary stats rather
than the raw PCM.

Two consumers in v0.7.9:

  * LoopbackRecorder / MicRecorder use a single tracker to surface a
    mid-recording capture_warning when the stream stays silent past
    a threshold (issue #84). Combined with a meeting-app activity
    cross-check in the controller, the banner copy can distinguish
    "everything is quiet" from "everything is quiet here, but Teams
    is rendering audio on a different endpoint."

  * MultiEndpointLoopbackRecorder uses one tracker per endpoint to
    gate disk writes: endpoints silent for >gate_silence_sec stop
    writing to disk until audio resumes. Each tracker emits silent_*
    / active state transitions which the orchestrator wires to the
    per-endpoint AsyncWavWriter.

Pure-Python module; no Qt, no PortAudio. The numeric path uses numpy
which is already a hard dependency for everything that touches PCM.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Optional

import numpy as np


# A floor of -60 dBFS is well below any meaningful speech but well
# above the noise floor of any modern WASAPI loopback stream that's
# actually rendering content. Below this we consider the stream silent.
DEFAULT_SILENCE_FLOOR_DBFS = -60.0

# Convert to amplitude (0..1 against full-scale int16 range, 32767).
# 10 ** (-60/20) ~ 0.001. Multiplied by 32767 gives ~33 -- so any int16
# chunk with RMS below ~33 counts as silence.
def dbfs_to_amplitude(dbfs: float) -> float:
    return float(10.0 ** (dbfs / 20.0))


@dataclass
class _ChunkSummary:
    """Per-chunk rolling-window entry. Sum-of-squares + sample count
    is enough to derive RMS for any prefix without retaining the PCM."""

    wallclock: float
    sum_sq: float
    count: int


class SilenceTracker:
    """Rolling-window RMS tracker.

    Call `write(pcm_int16, wallclock)` from the audio callback. Call
    `is_silent(now)` to ask whether the window has been silent for
    `silence_window_sec` continuously. State transitions are observable
    via the `was_silent` / `was_active` snapshots.

    Thread-safety: not safe for concurrent writes. Each tracker is
    expected to be owned by a single producer (one audio callback).
    Reads are safe to do from another thread because the deque
    structure tolerates concurrent observers per Python's GIL semantics.
    """

    def __init__(
        self,
        *,
        silence_window_sec: float = 30.0,
        silence_floor_dbfs: float = DEFAULT_SILENCE_FLOOR_DBFS,
        full_scale: int = 32767,
    ) -> None:
        self._window_sec = float(silence_window_sec)
        self._floor_dbfs = float(silence_floor_dbfs)
        self._floor_amp = dbfs_to_amplitude(silence_floor_dbfs) * full_scale
        self._chunks: deque[_ChunkSummary] = deque()
        self._first_write_wallclock: Optional[float] = None
        self._was_silent_last_check: bool = False

    @property
    def silence_window_sec(self) -> float:
        return self._window_sec

    @property
    def silence_floor_dbfs(self) -> float:
        return self._floor_dbfs

    def write(self, pcm_int16: np.ndarray, wallclock: float) -> None:
        """Append a chunk's summary stats. Drops chunks older than
        `silence_window_sec` to keep the deque bounded."""
        if pcm_int16.size == 0:
            return
        # int32 to avoid int16 overflow on the square. The square of
        # the int16 minimum (-32768) is 1_073_741_824, which fits in
        # int32 but the cumulative sum across a chunk does not, so we
        # promote to float64 for the sum itself.
        as_f = pcm_int16.astype(np.float64)
        sum_sq = float(np.sum(as_f * as_f))
        count = int(pcm_int16.size)
        self._chunks.append(_ChunkSummary(
            wallclock=wallclock, sum_sq=sum_sq, count=count,
        ))
        if self._first_write_wallclock is None:
            self._first_write_wallclock = wallclock
        self._drop_expired(wallclock)

    def _drop_expired(self, now: float) -> None:
        cutoff = now - self._window_sec
        while self._chunks and self._chunks[0].wallclock < cutoff:
            self._chunks.popleft()

    def current_rms(self) -> float:
        """RMS of the chunks currently in the window. 0 when empty."""
        if not self._chunks:
            return 0.0
        total_sq = sum(c.sum_sq for c in self._chunks)
        total_count = sum(c.count for c in self._chunks)
        if total_count == 0:
            return 0.0
        return math.sqrt(total_sq / total_count)

    def current_rms_dbfs(self, *, full_scale: int = 32767) -> float:
        """RMS expressed in dBFS; -inf if window is silent or empty."""
        rms = self.current_rms()
        if rms <= 0.0:
            return float("-inf")
        ratio = rms / float(full_scale)
        if ratio <= 0.0:
            return float("-inf")
        return 20.0 * math.log10(ratio)

    def has_full_window(self, now: float) -> bool:
        """True when we've been writing for at least window_sec. Until
        the first window completes the silence check is suppressed so
        recordings don't fire a false warning during their first 30s
        of legitimate near-silence (e.g. waiting for the call to
        start)."""
        if self._first_write_wallclock is None:
            return False
        return (now - self._first_write_wallclock) >= self._window_sec

    def is_silent(self, now: float) -> bool:
        """True iff the entire window has stayed under the silence
        floor. False before the window is full, since we don't yet
        have a complete window to judge."""
        self._drop_expired(now)
        if not self.has_full_window(now):
            self._was_silent_last_check = False
            return False
        rms = self.current_rms()
        silent = rms < self._floor_amp
        self._was_silent_last_check = silent
        return silent

    @property
    def was_silent_last_check(self) -> bool:
        """Result of the last `is_silent()` call. Lets the caller see
        the transition without re-running the check."""
        return self._was_silent_last_check

    def reset(self) -> None:
        """Drop accumulated state. Used after a re-bind so the next
        is_silent() doesn't inherit the old endpoint's silence."""
        self._chunks.clear()
        self._first_write_wallclock = None
        self._was_silent_last_check = False
