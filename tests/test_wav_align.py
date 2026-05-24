"""WAV-alignment helper: pad leading/trailing silence + offset math.

The recorders use this at stop time to make mic.wav and sys.wav
both span the full [start, stop] wall-clock window. Without it,
WASAPI loopback delivers samples only when audio is actually
playing, so sys.wav can be missing seconds of leading silence
relative to mic.wav -- which shows up as misaligned playback +
mis-ordered transcription.
"""
from __future__ import annotations

import struct
import wave
from pathlib import Path

import pytest

from meeting_notetaker.audio.wav_align import (
    compute_pad_frames,
    gap_frames_to_fill,
    pad_wav,
)


def _write_wav(
    path: Path, *, sample_rate: int, channels: int,
    sample_count: int, fill_value: int = 1,
) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        for _ in range(sample_count):
            if channels == 1:
                w.writeframes(struct.pack("<h", fill_value))
            else:
                w.writeframes(struct.pack(
                    "<" + "h" * channels, *([fill_value] * channels),
                ))


def _read_wav(path: Path) -> tuple[bytes, int, int, int]:
    """Return (raw_pcm, rate, channels, n_frames)."""
    with wave.open(str(path), "rb") as rf:
        return (
            rf.readframes(rf.getnframes()),
            rf.getframerate(),
            rf.getnchannels(),
            rf.getnframes(),
        )


def test_pad_wav_noop_when_both_zero(tmp_path):
    p = tmp_path / "x.wav"
    _write_wav(p, sample_rate=48000, channels=1, sample_count=100)
    original = p.read_bytes()
    pad_wav(p, leading_frames=0, trailing_frames=0)
    assert p.read_bytes() == original


def test_pad_wav_leading_silence(tmp_path):
    p = tmp_path / "x.wav"
    _write_wav(p, sample_rate=48000, channels=1, sample_count=100, fill_value=1234)
    pad_wav(p, leading_frames=50, trailing_frames=0)
    pcm, rate, channels, n_frames = _read_wav(p)
    assert n_frames == 150
    # First 50 frames are silence (int16 = 2 bytes).
    assert pcm[:50 * 2] == b"\x00" * 100
    # Remaining 100 frames are the original 1234 fill.
    expected_tail = b"".join(struct.pack("<h", 1234) for _ in range(100))
    assert pcm[50 * 2 :] == expected_tail


def test_pad_wav_trailing_silence(tmp_path):
    p = tmp_path / "x.wav"
    _write_wav(p, sample_rate=48000, channels=1, sample_count=100, fill_value=1234)
    pad_wav(p, leading_frames=0, trailing_frames=30)
    pcm, rate, channels, n_frames = _read_wav(p)
    assert n_frames == 130
    # First 100 frames are original.
    expected_head = b"".join(struct.pack("<h", 1234) for _ in range(100))
    assert pcm[: 100 * 2] == expected_head
    # Last 30 frames are silence.
    assert pcm[100 * 2 :] == b"\x00" * (30 * 2)


def test_pad_wav_both_sides(tmp_path):
    p = tmp_path / "x.wav"
    _write_wav(p, sample_rate=48000, channels=2, sample_count=10, fill_value=99)
    pad_wav(p, leading_frames=5, trailing_frames=7)
    pcm, rate, channels, n_frames = _read_wav(p)
    assert n_frames == 22  # 5 + 10 + 7
    assert channels == 2
    # 2 channels x 2 bytes = 4 bytes per frame.
    assert pcm[: 5 * 4] == b"\x00" * 20
    assert pcm[(5 + 10) * 4 :] == b"\x00" * 28


def test_pad_wav_missing_file_is_noop(tmp_path):
    """No file -> no exception; pad_wav silently skips."""
    pad_wav(
        tmp_path / "does-not-exist.wav",
        leading_frames=10, trailing_frames=10,
    )


def test_compute_pad_frames_first_sample_late(tmp_path):
    """Recorder started at t=0, first sample arrived at t=2.0 s,
    stopped at t=10.0 s. Actual frames captured = 8 s worth.
    Expected: leading=2 s of frames, trailing=0."""
    start = 100.0
    stop = 110.0
    first = 102.0  # 2 s after start
    rate = 48000
    actual = 8 * rate  # 8 s captured
    leading, trailing = compute_pad_frames(
        start_wallclock=start,
        first_sample_wallclock=first,
        stop_wallclock=stop,
        actual_frames=actual,
        sample_rate=rate,
    )
    assert leading == 2 * rate
    assert trailing == 0


def test_compute_pad_frames_trailing_dropout(tmp_path):
    """Recorder started immediately, ran the full 10 s, but the
    last 1 s wasn't captured (e.g. WASAPI went idle). Expected:
    leading=0, trailing=1 s of frames."""
    start = 0.0
    stop = 10.0
    first = 0.0  # delivered immediately
    rate = 48000
    actual = 9 * rate  # 9 s captured
    leading, trailing = compute_pad_frames(
        start_wallclock=start,
        first_sample_wallclock=first,
        stop_wallclock=stop,
        actual_frames=actual,
        sample_rate=rate,
    )
    assert leading == 0
    assert trailing == 1 * rate


def test_compute_pad_frames_both_sides(tmp_path):
    """First sample at t=1 s, capture ended at t=8 s, stop at t=10 s.
    Captured = 7 s of frames. Expected: leading=1s, trailing=2s."""
    start = 0.0
    stop = 10.0
    first = 1.0
    rate = 48000
    actual = 7 * rate
    leading, trailing = compute_pad_frames(
        start_wallclock=start,
        first_sample_wallclock=first,
        stop_wallclock=stop,
        actual_frames=actual,
        sample_rate=rate,
    )
    assert leading == rate
    assert trailing == 2 * rate


def test_compute_pad_frames_no_callbacks_returns_zero(tmp_path):
    """first_sample is None -> recorder never delivered a callback.
    Return zeros; the caller can decide whether to pad an entirely-
    empty file with silence or leave it alone."""
    leading, trailing = compute_pad_frames(
        start_wallclock=0.0,
        first_sample_wallclock=None,
        stop_wallclock=10.0,
        actual_frames=0,
        sample_rate=48000,
    )
    assert leading == 0
    assert trailing == 0


def test_gap_frames_to_fill_zero_for_continuous_callbacks():
    """Continuous-rate callbacks (actual delta ~= frame_time) -> no gap."""
    rate = 48000
    frame_count = 1024  # ~21 ms at 48k
    # The audio thread's normal jitter: actual delta = 25 ms vs expected 21 ms.
    gap = gap_frames_to_fill(
        now_wallclock=10.025,
        last_callback_wallclock=10.000,
        frame_count=frame_count,
        sample_rate=rate,
    )
    assert gap == 0


def test_gap_frames_to_fill_detects_engine_sleep():
    """WASAPI slept for 5 s between callbacks -> fill 5 s of silence."""
    rate = 48000
    frame_count = 1024
    gap = gap_frames_to_fill(
        now_wallclock=15.021,  # 5 s + ~21 ms after previous callback
        last_callback_wallclock=10.000,
        frame_count=frame_count,
        sample_rate=rate,
    )
    # gap_s = (15.021 - 10.000) - (1024 / 48000) = 5.021 - 0.0213 = 4.999...
    # at 48k -> ~240000 frames
    assert 239000 <= gap <= 240500


def test_gap_frames_to_fill_threshold_default_100ms():
    """A 50 ms gap is below the default 100 ms threshold -> no pad."""
    rate = 48000
    frame_count = 1024
    gap = gap_frames_to_fill(
        now_wallclock=10.071,  # 50 ms + frame_time after previous
        last_callback_wallclock=10.000,
        frame_count=frame_count,
        sample_rate=rate,
    )
    assert gap == 0


def test_gap_frames_to_fill_threshold_custom():
    """Callers can tighten the threshold for a stricter test fixture."""
    rate = 48000
    frame_count = 1024
    gap = gap_frames_to_fill(
        now_wallclock=10.071,  # 50 ms gap
        last_callback_wallclock=10.000,
        frame_count=frame_count,
        sample_rate=rate,
        threshold_ms=10,  # 10 ms threshold
    )
    # gap_s = 0.071 - 0.0213 = ~0.05 s
    assert gap > 0


def test_compute_pad_frames_sub_threshold_returns_zero():
    """Sub-1ms jitter is below the default threshold; don't churn
    the file for that."""
    rate = 48000
    leading, trailing = compute_pad_frames(
        start_wallclock=0.0,
        first_sample_wallclock=0.0005,  # 0.5 ms
        stop_wallclock=10.0,
        actual_frames=10 * rate - 5,  # ~0.1 ms short
        sample_rate=rate,
    )
    assert leading == 0
    assert trailing == 0
