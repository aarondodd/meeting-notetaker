"""AudioPlayer: load + mix + position tracking.

The actual sounddevice playback can't run without PortAudio, so we
patch the stream creation to skip the audio output path. The
load + buffer math is what most regressions would land on, and we
exercise that directly.
"""
from __future__ import annotations

import math
import os
import struct
import sys
import wave
from pathlib import Path
from unittest import mock

import pytest

pytest.importorskip("av")
pytest.importorskip("PyQt6.QtCore")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.audio.player import (  # noqa: E402
    AudioPlayer,
    _mix_to_max_length,
)
import numpy as np  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


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


def test_mix_to_max_length_pads_short_side():
    """Sum + attenuate. Shorter buffer is zero-padded so the long
    side determines the output length."""
    a = np.array([1.0, 1.0, 1.0, 1.0], dtype=np.float32)
    b = np.array([1.0, 1.0], dtype=np.float32)
    mixed = _mix_to_max_length([a, b])
    assert mixed.size == 4
    # First two samples are (1 + 1) / 2 = 1.0; last two are 1/2 (only a contributes).
    assert mixed[0] == pytest.approx(1.0)
    assert mixed[1] == pytest.approx(1.0)
    assert mixed[2] == pytest.approx(0.5)
    assert mixed[3] == pytest.approx(0.5)


def test_mix_to_max_length_single_buffer_passes_through():
    a = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    mixed = _mix_to_max_length([a])
    assert mixed is a or np.array_equal(mixed, a)


def test_load_emits_loaded_with_total_ms(qt_app, tmp_path):
    mic = tmp_path / "mic.wav"
    _write_sine_wav(mic, sample_rate=48000, channels=1, duration_s=1.0)
    player = AudioPlayer()
    captured: list[int] = []
    player.loaded.connect(captured.append)
    player.load(mic, None)
    qt_app.processEvents()
    assert captured, "load should emit loaded(total_ms) on success"
    # 1 second of audio -> ~1000 ms; the resample to 16k preserves it
    # within one frame's worth of jitter.
    assert 990 <= captured[0] <= 1010


def test_load_emits_load_failed_for_missing_sources(qt_app, tmp_path):
    player = AudioPlayer()
    captured: list[str] = []
    player.load_failed.connect(captured.append)
    player.load(tmp_path / "missing-mic.wav", tmp_path / "missing-sys.wav")
    qt_app.processEvents()
    assert captured, "load should emit load_failed for missing files"


def test_position_starts_at_zero_after_load(qt_app, tmp_path):
    mic = tmp_path / "mic.wav"
    _write_sine_wav(mic, sample_rate=48000, channels=1, duration_s=0.5)
    player = AudioPlayer()
    player.load(mic, None)
    qt_app.processEvents()
    assert player.position_ms() == 0


def test_seek_ms_clamps_to_bounds(qt_app, tmp_path):
    mic = tmp_path / "mic.wav"
    _write_sine_wav(mic, sample_rate=48000, channels=1, duration_s=0.5)
    player = AudioPlayer()
    player.load(mic, None)
    qt_app.processEvents()
    player.seek_ms(-100)
    assert player.position_ms() == 0
    player.seek_ms(10_000_000)  # way past end
    assert player.position_ms() <= player.total_ms()


def test_close_drops_buffer_and_resets_position(qt_app, tmp_path):
    mic = tmp_path / "mic.wav"
    _write_sine_wav(mic, sample_rate=48000, channels=1, duration_s=0.5)
    player = AudioPlayer()
    player.load(mic, None)
    qt_app.processEvents()
    assert player.total_ms() > 0
    player.close()
    assert player.total_ms() == 0
    assert player.position_ms() == 0


def test_load_twice_replaces_session(qt_app, tmp_path):
    mic1 = tmp_path / "mic1.wav"
    _write_sine_wav(mic1, sample_rate=48000, channels=1, duration_s=0.5)
    mic2 = tmp_path / "mic2.wav"
    _write_sine_wav(mic2, sample_rate=48000, channels=1, duration_s=1.0)
    player = AudioPlayer()
    captured: list[int] = []
    player.loaded.connect(captured.append)
    player.load(mic1, None)
    player.load(mic2, None)
    qt_app.processEvents()
    # The latest total should reflect the second (longer) session.
    assert captured[-1] > captured[0]


def test_play_calls_start_stream(qt_app, tmp_path):
    """play() opens a sounddevice OutputStream. Patch the
    construction so we don't need PortAudio in the test env."""
    mic = tmp_path / "mic.wav"
    _write_sine_wav(mic, sample_rate=48000, channels=1, duration_s=0.5)
    player = AudioPlayer()
    player.load(mic, None)
    qt_app.processEvents()

    fake_stream = mock.MagicMock()
    fake_stream.active = True
    fake_sd = mock.MagicMock()
    fake_sd.OutputStream = mock.MagicMock(return_value=fake_stream)

    with mock.patch.dict(sys.modules, {"sounddevice": fake_sd}):
        player.play()
    assert fake_sd.OutputStream.called
    assert fake_stream.start.called
