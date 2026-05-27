"""AudioPlayer: load + mix + position tracking.

The actual sounddevice playback can't run without PortAudio, so we
patch the stream creation to skip the audio output path. The
load + buffer math is what most regressions would land on, and we
exercise that directly.

As of v0.7.1 (issue #31), AudioPlayer.load is asynchronous: the
PyAV decode runs on a worker QThread and emits loaded / load_failed
when finished. Tests wait for completion via `_wait_for_load`
rather than assuming the buffer is ready after a single
processEvents() call.
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


def _wait_for_load(player, qt_app, *, timeout: float = 5.0) -> None:
    """Spin processEvents until the AudioPlayer's pending load finishes.

    The async decode worker runs on a QThread; its emit lands as a
    queued event on the main thread. We have to (a) wait for the OS
    thread to finish AND (b) let the event loop run so the queued
    decoded signal fires. processEvents alone does neither -- it
    drains whatever is already queued and returns. The loop here
    combines a short sleep with processEvents until the player
    reports no in-flight load.
    """
    deadline = time.monotonic() + timeout
    qt_app.processEvents()
    while player._load_worker is not None and time.monotonic() < deadline:  # noqa: SLF001
        qt_app.processEvents()
        time.sleep(0.01)
    qt_app.processEvents()  # one more pass to drain the final signal


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


def test_mix_to_max_length_pads_short_side_at_front():
    """End-aligned mix: shorter buffer gets LEADING silence so its
    END aligns with the merged track's end. Reflects the v0.6.5
    fix where WASAPI loopback (sys) may start producing samples
    after mic does; both stop simultaneously at controller.stop,
    so end-alignment is the natural anchor."""
    a = np.array([1.0, 1.0, 1.0, 1.0], dtype=np.float32)
    b = np.array([1.0, 1.0], dtype=np.float32)
    mixed = _mix_to_max_length([a, b])
    assert mixed.size == 4
    # First two: only a contributes (b's leading silence), so 0.5
    # each. Last two: both contribute, so 1.0 each.
    assert mixed[0] == pytest.approx(0.5)
    assert mixed[1] == pytest.approx(0.5)
    assert mixed[2] == pytest.approx(1.0)
    assert mixed[3] == pytest.approx(1.0)


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
    _wait_for_load(player, qt_app)
    assert captured, "load should emit loaded(total_ms) on success"
    # 1 second of audio -> ~1000 ms; the resample to 16k preserves it
    # within one frame's worth of jitter.
    assert 990 <= captured[0] <= 1010


def test_load_returns_immediately(qt_app, tmp_path):
    """The whole point of moving decode to a worker (issue #31) is that
    load() must not block the caller. Even with a large WAV the
    method should return in milliseconds; the decode happens on a
    background QThread.
    """
    mic = tmp_path / "mic.wav"
    # 5 seconds is small enough to keep the test fast but long enough
    # that a synchronous decode would take measurably longer than the
    # < 50 ms budget for an async load call.
    _write_sine_wav(mic, sample_rate=48000, channels=1, duration_s=5.0)
    player = AudioPlayer()
    start = time.monotonic()
    player.load(mic, None)
    elapsed = time.monotonic() - start
    assert elapsed < 0.1, (
        f"load() must return immediately (worker-thread decode), "
        f"took {elapsed * 1000:.0f}ms"
    )
    # Drain the worker so the test fixture tears down cleanly.
    _wait_for_load(player, qt_app)


def test_load_emits_load_failed_for_missing_sources(qt_app, tmp_path):
    player = AudioPlayer()
    captured: list[str] = []
    player.load_failed.connect(captured.append)
    player.load(tmp_path / "missing-mic.wav", tmp_path / "missing-sys.wav")
    _wait_for_load(player, qt_app)
    assert captured, "load should emit load_failed for missing files"


def test_position_starts_at_zero_after_load(qt_app, tmp_path):
    mic = tmp_path / "mic.wav"
    _write_sine_wav(mic, sample_rate=48000, channels=1, duration_s=0.5)
    player = AudioPlayer()
    player.load(mic, None)
    _wait_for_load(player, qt_app)
    assert player.position_ms() == 0


def test_seek_ms_clamps_to_bounds(qt_app, tmp_path):
    mic = tmp_path / "mic.wav"
    _write_sine_wav(mic, sample_rate=48000, channels=1, duration_s=0.5)
    player = AudioPlayer()
    player.load(mic, None)
    _wait_for_load(player, qt_app)
    player.seek_ms(-100)
    assert player.position_ms() == 0
    player.seek_ms(10_000_000)  # way past end
    assert player.position_ms() <= player.total_ms()


def test_close_drops_buffer_and_resets_position(qt_app, tmp_path):
    mic = tmp_path / "mic.wav"
    _write_sine_wav(mic, sample_rate=48000, channels=1, duration_s=0.5)
    player = AudioPlayer()
    player.load(mic, None)
    _wait_for_load(player, qt_app)
    assert player.total_ms() > 0
    player.close()
    assert player.total_ms() == 0
    assert player.position_ms() == 0


def test_load_twice_replaces_session(qt_app, tmp_path):
    """Second load() supersedes the first.

    Async decode means the first load's result might land before or
    after the second load completes. Either way, only the SECOND
    load's buffer should be active: the generation check in the
    player drops the first decode's emit if it arrives late.

    Asserts the final total_ms reflects the second (longer) WAV
    rather than counting emit calls, since the first one may have
    been dropped entirely.
    """
    mic1 = tmp_path / "mic1.wav"
    _write_sine_wav(mic1, sample_rate=48000, channels=1, duration_s=0.5)
    mic2 = tmp_path / "mic2.wav"
    _write_sine_wav(mic2, sample_rate=48000, channels=1, duration_s=1.0)
    player = AudioPlayer()
    captured: list[int] = []
    player.loaded.connect(captured.append)
    player.load(mic1, None)
    player.load(mic2, None)
    _wait_for_load(player, qt_app)
    # The active buffer must reflect mic2 (1.0s = ~1000ms), never
    # mic1 (0.5s = ~500ms).
    assert 990 <= player.total_ms() <= 1010, (
        f"second load's buffer must win; got total_ms={player.total_ms()}"
    )
    # If both signals fired, the last one must be the second load.
    if len(captured) >= 2:
        assert captured[-1] > captured[0]


def test_close_during_load_drops_pending_decode(qt_app, tmp_path):
    """close() while a load is in-flight must cancel the load so the
    result -- if it lands after close() returns -- is dropped. The
    generation counter is the load-bearing piece here.
    """
    mic = tmp_path / "mic.wav"
    _write_sine_wav(mic, sample_rate=48000, channels=1, duration_s=0.5)
    player = AudioPlayer()
    captured: list[int] = []
    player.loaded.connect(captured.append)
    player.load(mic, None)
    # Close immediately, before the worker can finish.
    player.close()
    # Now drain the worker thread.
    _wait_for_load(player, qt_app)
    assert player.total_ms() == 0
    assert not captured, "loaded must not fire after close()"


def test_seek_during_playback_restarts_position_tick(qt_app, tmp_path):
    """Regression: a seek while playing used to leave the position tick
    stopped (audio kept playing via sounddevice but the UI scrubber
    froze). Confirm the tick is running after a mid-play seek."""
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
        assert player._tick.isActive(), "tick should run while playing"  # noqa: SLF001
        # Mid-play seek: the old code stopped the tick (in _stop_stream)
        # and never restarted it (only play() did, not _start_stream).
        player.seek_ms(100)
        assert player._tick.isActive(), (  # noqa: SLF001
            "tick must still run after a seek-during-playback so the "
            "UI scrubber + transcript highlight keep updating"
        )


def test_stale_finished_callback_does_not_close_new_stream(qt_app, tmp_path):
    """Regression: stop() on the old stream schedules a queued
    finished_callback. seek_ms then builds a new stream BEFORE the
    queued callback fires. The handler must ignore the stale
    callback (generation mismatch) instead of closing the new
    stream and stopping the tick."""
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
        # After play, generation = 1; we'd be processing finished
        # callbacks for generation 1.
        gen_first = player._stream_generation  # noqa: SLF001
        # Simulate the seek+restart that bumps the generation.
        player.seek_ms(100)
        gen_after_seek = player._stream_generation  # noqa: SLF001
        assert gen_after_seek > gen_first, (
            "seek-during-playback must bump the generation so the old "
            "stream's queued finished_callback is treated as stale"
        )
        # Now simulate the OLD stream's queued finished callback
        # arriving on the main thread. Pass the OLD generation.
        player._handle_stream_finished_on_main_thread(gen_first)  # noqa: SLF001
        # The NEW stream must still be set; the tick must still be
        # running.
        assert player._stream is not None, (  # noqa: SLF001
            "the stale callback should NOT have torn down the new stream"
        )
        assert player._tick.isActive()  # noqa: SLF001


def test_play_calls_start_stream(qt_app, tmp_path):
    """play() opens a sounddevice OutputStream. Patch the
    construction so we don't need PortAudio in the test env."""
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
    assert fake_stream.start.called
