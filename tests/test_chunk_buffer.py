"""ChunkBuffer windowing + drain semantics."""
from __future__ import annotations

import numpy as np
import pytest

from meeting_notetaker.audio.chunk_buffer import (
    ChunkBuffer,
    SAMPLE_RATE,
    dedupe_overlap,
)


def _ramp(n: int) -> np.ndarray:
    """Deterministic int16 PCM with values 0,1,2,...,n-1 (wrapping)."""
    return (np.arange(n) % 32000).astype(np.int16)


def test_pop_window_returns_none_when_short():
    buf = ChunkBuffer(["mic"])
    buf.write("mic", _ramp(int(5 * SAMPLE_RATE)))   # 5s, window is 10s
    assert buf.pop_window("mic") is None


def test_pop_window_yields_overlapping_windows():
    buf = ChunkBuffer(["mic"], window_sec=2.0, overlap_sec=1.0, sample_rate=100)
    # 1 second = 100 samples, window 2s = 200, step 1s = 100
    buf.write("mic", _ramp(500))   # 5 "seconds" written
    w1 = buf.pop_window("mic")
    w2 = buf.pop_window("mic")
    w3 = buf.pop_window("mic")
    assert w1 is not None and w2 is not None and w3 is not None
    assert w1.t_start == 0.0 and w1.t_end == 2.0
    assert w2.t_start == 1.0 and w2.t_end == 3.0
    assert w3.t_start == 2.0 and w3.t_end == 4.0
    # Overlap: w1[100:200] == w2[0:100] (the overlap region)
    assert np.array_equal(w1.pcm[100:200], w2.pcm[0:100])
    # Buffer should still have enough left for w4 only after more writes
    w4 = buf.pop_window("mic")
    assert w4 is not None
    assert w4.t_start == 3.0


def test_drain_returns_remainder_after_stop():
    buf = ChunkBuffer(["mic"], window_sec=2.0, overlap_sec=1.0, sample_rate=100)
    buf.write("mic", _ramp(150))   # 1.5s, below window
    drained = buf.drain("mic")
    assert drained is not None
    assert len(drained.pcm) == 150
    assert drained.t_start == 0.0
    assert drained.t_end == 1.5
    # After drain, the buffer is empty
    assert buf.drain("mic") is None


def test_window_sec_must_exceed_overlap():
    with pytest.raises(ValueError):
        ChunkBuffer(["mic"], window_sec=2.0, overlap_sec=2.0)


def test_per_source_isolation():
    buf = ChunkBuffer(["mic", "sys"], window_sec=2.0, overlap_sec=1.0, sample_rate=100)
    buf.write("mic", _ramp(200))   # exactly one window
    buf.write("sys", _ramp(50))
    assert buf.pop_window("mic") is not None
    assert buf.pop_window("sys") is None
    assert buf.pop_window("mic") is None


def test_written_seconds_tracks_input():
    buf = ChunkBuffer(["mic"], window_sec=2.0, overlap_sec=1.0, sample_rate=100)
    buf.write("mic", _ramp(150))
    buf.write("mic", _ramp(100))
    assert buf.written_seconds("mic") == pytest.approx(2.5)
