"""AudioPlayer int16 storage path.

v0.8.x+ stores the player's mixed buffer as int16 rather than
float32 so a 2.5h meeting peaks at ~287 MiB instead of 573 MiB and
the load doesn't OOM on bundled Windows builds (issue #137).

The sounddevice output stream is also set to dtype="int16" so the
callback writes int16 samples straight through with no per-callback
dtype conversion.
"""
from __future__ import annotations

import math
import os
import struct
import sys
import time
import wave
from pathlib import Path
from unittest import mock

import numpy as np
import pytest

pytest.importorskip("av")
pytest.importorskip("PyQt6.QtCore")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.audio.player import (  # noqa: E402
    AudioPlayer,
    _decode_to_mono_int16,
    _mix_int16_to_max_length,
)


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _wait_for_load(player, qt_app, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    qt_app.processEvents()
    while player._load_worker is not None and time.monotonic() < deadline:  # noqa: SLF001
        qt_app.processEvents()
        time.sleep(0.01)
    qt_app.processEvents()


def _write_sine_wav(
    path: Path, *, sample_rate: int, channels: int, duration_s: float,
    freq_hz: float = 440.0,
) -> None:
    n = int(sample_rate * duration_s)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        for i in range(n):
            sample = int(32767 * 0.3 * math.sin(2 * math.pi * freq_hz * i / sample_rate))
            if channels == 1:
                w.writeframes(struct.pack("<h", sample))
            else:
                w.writeframes(struct.pack("<" + "h" * channels, *([sample] * channels)))


# ---------------------------------------------------------------------------
# _decode_to_mono_int16


def test_decode_to_mono_int16_dtype_and_length(tmp_path: Path):
    wav = tmp_path / "sine.wav"
    _write_sine_wav(wav, sample_rate=48000, channels=1, duration_s=0.5)
    audio = _decode_to_mono_int16(wav, target_rate=16000)
    assert audio.dtype == np.int16
    # ~0.5s at 16 kHz -> 8000 samples, resampler slop +/- a few frames.
    assert 7900 <= audio.shape[0] <= 8100


def test_decode_to_mono_int16_memory_is_half_of_float32(tmp_path: Path):
    """The whole point of the int16 path: nbytes is exactly half what
    the float32 path would produce for the same audio."""
    wav = tmp_path / "sine.wav"
    _write_sine_wav(wav, sample_rate=48000, channels=1, duration_s=0.5)
    audio = _decode_to_mono_int16(wav, target_rate=16000)
    # Sanity check: 2 bytes per sample.
    assert audio.nbytes == 2 * audio.shape[0]


# ---------------------------------------------------------------------------
# _mix_int16_to_max_length


def test_mix_int16_single_buffer_passes_through():
    a = np.array([100, 200, 300], dtype=np.int16)
    mixed = _mix_int16_to_max_length([a])
    assert mixed is a or np.array_equal(mixed, a)


def test_mix_int16_pads_short_side_at_front():
    """Shorter buffer gets LEADING silence so its END aligns with the
    merged track's end -- same contract as _mix_to_max_length but on
    int16."""
    a = np.array([10000, 10000, 10000, 10000], dtype=np.int16)
    b = np.array([10000, 10000], dtype=np.int16)
    mixed = _mix_int16_to_max_length([a, b])
    assert mixed.dtype == np.int16
    assert mixed.shape[0] == 4
    # First two: only a contributes, divided by 2 (n=2 sources).
    assert mixed[0] == 5000
    assert mixed[1] == 5000
    # Last two: both contribute equal values, sum / 2 = 10000.
    assert mixed[2] == 10000
    assert mixed[3] == 10000


def test_mix_int16_handles_extreme_values_without_overflow():
    """Two int16 buffers at full-scale sum would overflow int16; the
    accumulator must stay in int32 and the per-sample division
    brings it back in range."""
    a = np.full(10, 32767, dtype=np.int16)
    b = np.full(10, 32767, dtype=np.int16)
    mixed = _mix_int16_to_max_length([a, b])
    # (32767 + 32767) // 2 = 32767 (exact).
    assert (mixed == 32767).all()
    # And the negative extreme.
    a = np.full(10, -32768, dtype=np.int16)
    b = np.full(10, -32768, dtype=np.int16)
    mixed = _mix_int16_to_max_length([a, b])
    # (-32768 + -32768) // 2 = -32768.
    assert (mixed == -32768).all()


def test_mix_int16_block_boundary_matches_single_block(monkeypatch):
    """The blockwise path must produce byte-identical output to a
    single-block path for the same input. Shrink the block size so a
    small buffer crosses a boundary."""
    from meeting_notetaker.audio import player as player_mod
    rng = np.random.default_rng(0xC0FFEE)
    a = rng.integers(-10000, 10000, size=5000, dtype=np.int16)
    b = rng.integers(-10000, 10000, size=3000, dtype=np.int16)
    monkeypatch.setattr(player_mod, "_MIX_BLOCK_SAMPLES", 100)
    blockwise = _mix_int16_to_max_length([a, b])
    monkeypatch.setattr(player_mod, "_MIX_BLOCK_SAMPLES", 10**9)
    single = _mix_int16_to_max_length([a, b])
    assert np.array_equal(blockwise, single)


# ---------------------------------------------------------------------------
# AudioPlayer integration: buffer dtype after load


def test_player_buffer_is_int16_after_load(qt_app, tmp_path):
    """The decode worker now emits int16. If this ever regresses to
    float32 a 2.5h meeting will OOM again."""
    mic = tmp_path / "mic.wav"
    _write_sine_wav(mic, sample_rate=48000, channels=1, duration_s=0.5)
    player = AudioPlayer()
    player.load(mic, None)
    _wait_for_load(player, qt_app)
    assert player._buffer is not None  # noqa: SLF001
    assert player._buffer.dtype == np.int16  # noqa: SLF001


def test_player_output_stream_dtype_is_int16(qt_app, tmp_path):
    """sounddevice.OutputStream must be opened with dtype='int16' so
    the callback can write int16 chunks straight through."""
    mic = tmp_path / "mic.wav"
    _write_sine_wav(mic, sample_rate=48000, channels=1, duration_s=0.5)
    player = AudioPlayer()
    player.load(mic, None)
    _wait_for_load(player, qt_app)

    fake_stream = mock.MagicMock()
    fake_stream.active = True
    fake_sd = mock.MagicMock()
    fake_sd.OutputStream = mock.MagicMock(return_value=fake_stream)

    with mock.patch.dict(sys.modules, {"sounddevice": fake_sd}):
        player.play()
    assert fake_sd.OutputStream.called
    kwargs = fake_sd.OutputStream.call_args.kwargs
    assert kwargs["dtype"] == "int16"


def test_player_two_source_load_mixes_to_int16(qt_app, tmp_path):
    """mic + sys mixed load lands as a single int16 buffer; the mix
    arithmetic is the int16 blockwise path, not the float32 helpers."""
    mic = tmp_path / "mic.wav"
    sysw = tmp_path / "sys.wav"
    _write_sine_wav(mic, sample_rate=48000, channels=1, duration_s=0.5)
    _write_sine_wav(sysw, sample_rate=48000, channels=2, duration_s=0.3)
    player = AudioPlayer()
    player.load(mic, sysw)
    _wait_for_load(player, qt_app)
    assert player._buffer is not None  # noqa: SLF001
    assert player._buffer.dtype == np.int16  # noqa: SLF001
    # Total frames should match the longer side (mic, 0.5s).
    assert 7900 <= player._buffer.shape[0] <= 8100  # noqa: SLF001
