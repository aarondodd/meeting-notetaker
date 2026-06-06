"""Tests for the multi-endpoint loopback orchestrator (#85).

The orchestrator itself drives real LoopbackRecorder instances and
requires Windows / WASAPI / PyAudioWPatch to start. These tests
exercise the pure-Python helpers:

  * mix_sidecar_wavs -- sample-wise sum-and-average across N WAVs;
    end-aligned via leading silence padding so short sidecars don't
    pull the rest of the mix forward.
  * discover_output_endpoints availability gating -- returns [] in
    test envs without PyAudioWPatch.

The recorder lifecycle + sub-recorder aggregation is covered by
integration tests gated behind the `audio` marker (real WASAPI).
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from meeting_notetaker.audio.multi_loopback import (
    MultiEndpointLoopbackRecorder,
    discover_output_endpoints,
    mix_sidecar_wavs,
)


# ---- helpers -------------------------------------------------------------

def _write_wav(
    path: Path,
    samples: np.ndarray,
    *,
    rate: int = 48000,
    channels: int = 2,
) -> None:
    """Write an int16 PCM array as a WAV file. Caller controls shape."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(samples.astype(np.int16).tobytes())


def _read_wav_samples(path: Path) -> tuple[np.ndarray, int, int]:
    with wave.open(str(path), "rb") as rf:
        rate = rf.getframerate()
        ch = rf.getnchannels()
        raw = rf.readframes(rf.getnframes())
    return np.frombuffer(raw, dtype=np.int16), rate, ch


# ---- mix_sidecar_wavs ----------------------------------------------------

def test_mix_empty_list_returns_false(tmp_path):
    ok = mix_sidecar_wavs([], tmp_path / "sys.wav")
    assert ok is False
    assert not (tmp_path / "sys.wav").exists()


def test_mix_missing_files_returns_false(tmp_path):
    ok = mix_sidecar_wavs(
        [tmp_path / "ghost1.wav", tmp_path / "ghost2.wav"],
        tmp_path / "sys.wav",
    )
    assert ok is False


def test_mix_single_sidecar_passes_through_with_average_divisor(tmp_path):
    """Single sidecar -> output equals input (divisor=1)."""
    sample = np.array([1000, -1000, 500, -500, 0, 0], dtype=np.int16)
    src = tmp_path / "sys.0.wav"
    _write_wav(src, sample, rate=48000, channels=1)
    dst = tmp_path / "sys.wav"
    ok = mix_sidecar_wavs([src], dst)
    assert ok is True
    out, rate, ch = _read_wav_samples(dst)
    assert rate == 48000
    assert ch == 1
    np.testing.assert_array_equal(out, sample)


def test_mix_two_sidecars_averages_overlapping_samples(tmp_path):
    """Sample-wise sum / 2. Loud-on-both inputs stay within int16 range."""
    a = np.array([16000, -16000, 8000], dtype=np.int16)
    b = np.array([8000, -8000, 16000], dtype=np.int16)
    _write_wav(tmp_path / "sys.0.wav", a, channels=1)
    _write_wav(tmp_path / "sys.1.wav", b, channels=1)
    ok = mix_sidecar_wavs(
        [tmp_path / "sys.0.wav", tmp_path / "sys.1.wav"],
        tmp_path / "sys.wav",
    )
    assert ok is True
    out, _, _ = _read_wav_samples(tmp_path / "sys.wav")
    # (a + b) // 2
    expected = ((a.astype(np.int32) + b.astype(np.int32)) // 2).astype(np.int16)
    np.testing.assert_array_equal(out, expected)


def test_mix_end_aligns_shorter_buffers_with_leading_silence(tmp_path):
    """A short sidecar (joined late, hot-plug) doesn't pull the mix
    earlier in time. End-alignment via leading-zero pad keeps it
    landing at the same wallclock moment as the long sidecar."""
    long_buf = np.array([1, 2, 3, 4, 5, 6], dtype=np.int16)
    short_buf = np.array([100, 200], dtype=np.int16)
    _write_wav(tmp_path / "sys.0.wav", long_buf, channels=1)
    _write_wav(tmp_path / "sys.1.wav", short_buf, channels=1)
    ok = mix_sidecar_wavs(
        [tmp_path / "sys.0.wav", tmp_path / "sys.1.wav"],
        tmp_path / "sys.wav",
    )
    assert ok is True
    out, _, _ = _read_wav_samples(tmp_path / "sys.wav")
    # short_buf -> [0, 0, 0, 0, 100, 200]; mix = (long + padded_short) // 2
    padded = np.array([0, 0, 0, 0, 100, 200], dtype=np.int32)
    expected = ((long_buf.astype(np.int32) + padded) // 2).astype(np.int16)
    np.testing.assert_array_equal(out, expected)


def test_mix_drops_minority_shape_sidecars(tmp_path):
    """Mismatched (rate, ch) across sidecars: keep the majority shape,
    drop the rest with a log warning. Prevents a malformed sidecar
    from corrupting the canonical."""
    majority = np.array([1000, 2000, 3000], dtype=np.int16)
    _write_wav(tmp_path / "sys.0.wav", majority, rate=48000, channels=1)
    _write_wav(tmp_path / "sys.1.wav", majority, rate=48000, channels=1)
    # Minority: 44.1k stereo, different shape, should be dropped.
    minority = np.array([5000, 6000, 7000, 8000], dtype=np.int16)
    _write_wav(tmp_path / "sys.2.wav", minority, rate=44100, channels=2)
    ok = mix_sidecar_wavs(
        [tmp_path / "sys.0.wav", tmp_path / "sys.1.wav", tmp_path / "sys.2.wav"],
        tmp_path / "sys.wav",
    )
    assert ok is True
    out, rate, ch = _read_wav_samples(tmp_path / "sys.wav")
    assert rate == 48000
    assert ch == 1
    # Mix should be majority's average (= majority since both inputs equal).
    np.testing.assert_array_equal(out, majority)


def test_mix_skips_empty_wavs(tmp_path):
    """An empty WAV (recorder failed mid-flight) is skipped, not summed."""
    real = np.array([1000, 2000, 3000], dtype=np.int16)
    _write_wav(tmp_path / "sys.0.wav", real, channels=1)
    _write_wav(tmp_path / "sys.1.wav", np.zeros(0, dtype=np.int16), channels=1)
    ok = mix_sidecar_wavs(
        [tmp_path / "sys.0.wav", tmp_path / "sys.1.wav"],
        tmp_path / "sys.wav",
    )
    assert ok is True
    out, _, _ = _read_wav_samples(tmp_path / "sys.wav")
    np.testing.assert_array_equal(out, real)


# ---- discover_output_endpoints ------------------------------------------

def test_discover_returns_empty_without_pyaudiowpatch(monkeypatch):
    """In test envs without PyAudioWPatch (Linux), discovery returns []
    rather than raising. The orchestrator's start() then surfaces a
    LoopbackUnavailable so the controller can fall back to mic-only."""
    import sys
    monkeypatch.setitem(sys.modules, "pyaudiowpatch", None)
    assert discover_output_endpoints() == []


# ---- MultiEndpointLoopbackRecorder ---------------------------------------

def test_is_available_mirrors_loopback_recorder():
    """The orchestrator's availability is a pure function of the
    underlying LoopbackRecorder's availability -- no extra deps."""
    from meeting_notetaker.audio.loopback_recorder import LoopbackRecorder
    assert (
        MultiEndpointLoopbackRecorder.is_available()
        == LoopbackRecorder.is_available()
    )


def test_sidecar_path_format(tmp_path, qt_app=None):
    """sys.wav -> sys.0.wav / sys.1.wav / ... for the sidecars."""
    rec = MultiEndpointLoopbackRecorder(
        chunk_buffer=None,
        wav_path=tmp_path / "sys.wav",
    )
    assert rec._sidecar_path(0) == tmp_path / "sys.0.wav"  # noqa: SLF001
    assert rec._sidecar_path(3) == tmp_path / "sys.3.wav"  # noqa: SLF001
