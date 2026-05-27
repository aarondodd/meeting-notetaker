"""MicRecorder + LoopbackRecorder capture-stall detection (issue #44).

The recorders don't crash when PortAudio stops calling their callback
mid-recording -- _maybe_pad_wav silently inserts trailing silence to
maintain wall-clock alignment with the sibling track. That's correct
for alignment but invisible to the user: they end up with N seconds
of silence at the end of their recording and no idea why.

The fix is detection: at Stop, compare the last_callback_wallclock
to stop_wallclock; if the gap exceeds the threshold (10 s), log a
clear warning + emit capture_warning so the app surfaces it.

These tests pin the detection contract without standing up
PortAudio. We invoke _check_for_trailing_capture_stall directly with
synthetic wallclock state.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtCore")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication  # noqa: E402

from meeting_notetaker.audio.chunk_buffer import ChunkBuffer  # noqa: E402
from meeting_notetaker.audio.loopback_recorder import LoopbackRecorder  # noqa: E402
from meeting_notetaker.audio.mic_recorder import MicRecorder  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QCoreApplication.instance() or QCoreApplication(sys.argv)


def _make_mic(tmp_path):
    buf = ChunkBuffer(["mic"])
    return MicRecorder(buf, tmp_path / "mic.wav")


def _make_loopback(tmp_path):
    buf = ChunkBuffer(["sys"])
    return LoopbackRecorder(buf, tmp_path / "sys.wav")


# ---- MicRecorder ----------------------------------------------------------


def test_mic_emits_warning_when_callback_stopped_well_before_stop(
    qt_app, tmp_path,
):
    """20 s gap between last callback and stop -> warning fires."""
    rec = _make_mic(tmp_path)
    rec._last_callback_wallclock = 100.0  # noqa: SLF001
    rec._stop_wallclock = 120.0  # noqa: SLF001
    msgs: list[str] = []
    rec.capture_warning.connect(msgs.append)
    rec._check_for_trailing_capture_stall()  # noqa: SLF001
    qt_app.processEvents()
    assert len(msgs) == 1
    assert "20.0 s" in msgs[0] or "20 s" in msgs[0]
    # Message should hint at USB power-management since that's the
    # most common cause for the mic side.
    assert "USB" in msgs[0] or "selective suspend" in msgs[0].lower()


def test_mic_no_warning_when_gap_below_threshold(qt_app, tmp_path):
    """5 s gap stays under the 10 s threshold -> silent."""
    rec = _make_mic(tmp_path)
    rec._last_callback_wallclock = 100.0  # noqa: SLF001
    rec._stop_wallclock = 105.0  # noqa: SLF001
    msgs: list[str] = []
    rec.capture_warning.connect(msgs.append)
    rec._check_for_trailing_capture_stall()  # noqa: SLF001
    qt_app.processEvents()
    assert msgs == []


def test_mic_no_warning_when_recorder_never_received_a_callback(
    qt_app, tmp_path,
):
    """A recorder that never got a single callback (e.g. device
    immediately failed) shouldn't fire this warning -- the lack of
    audio is a different failure mode caught by the error signal."""
    rec = _make_mic(tmp_path)
    rec._last_callback_wallclock = None  # noqa: SLF001
    rec._stop_wallclock = 120.0  # noqa: SLF001
    msgs: list[str] = []
    rec.capture_warning.connect(msgs.append)
    rec._check_for_trailing_capture_stall()  # noqa: SLF001
    qt_app.processEvents()
    assert msgs == []


# ---- LoopbackRecorder -----------------------------------------------------


def test_loopback_emits_warning_when_callback_stopped_well_before_stop(
    qt_app, tmp_path,
):
    """Same shape as the mic test, but the message wording differs --
    loopback's expected cause is WASAPI audio-engine idle, not USB
    power-management."""
    rec = _make_loopback(tmp_path)
    rec._last_callback_wallclock = 50.0  # noqa: SLF001
    rec._stop_wallclock = 85.0  # noqa: SLF001
    msgs: list[str] = []
    rec.capture_warning.connect(msgs.append)
    rec._check_for_trailing_capture_stall()  # noqa: SLF001
    qt_app.processEvents()
    assert len(msgs) == 1
    assert "35" in msgs[0]
    assert "audio engine" in msgs[0].lower() or "idle" in msgs[0].lower()


def test_loopback_no_warning_when_gap_below_threshold(qt_app, tmp_path):
    rec = _make_loopback(tmp_path)
    rec._last_callback_wallclock = 50.0  # noqa: SLF001
    rec._stop_wallclock = 52.0  # noqa: SLF001
    msgs: list[str] = []
    rec.capture_warning.connect(msgs.append)
    rec._check_for_trailing_capture_stall()  # noqa: SLF001
    qt_app.processEvents()
    assert msgs == []


# ---- Cumulative-loss detection (issue #47) -----------------------------------


def test_mic_emits_warning_when_trailing_pad_is_large(qt_app, tmp_path):
    """Aaron's 2026-05-27 test: 4.5-min trailing pad from ~17% cumulative
    sample loss. last_callback_gap was only 6 s (below the tail-silence
    threshold) but the file was 4.5 min short of wallclock. The new
    detection branch fires on trailing-pad-seconds >= 30."""
    rec = _make_mic(tmp_path)
    rec._last_callback_wallclock = 100.0  # noqa: SLF001
    rec._stop_wallclock = 106.0  # 6s gap, under the tail-silence threshold
    # 90s of trailing pad at 44.1k mono = 3,969,000 frames
    rec._trailing_pad_frames = 90 * rec._native_rate  # noqa: SLF001
    msgs: list[str] = []
    rec.capture_warning.connect(msgs.append)
    rec._check_for_trailing_capture_stall()  # noqa: SLF001
    qt_app.processEvents()
    assert len(msgs) == 1
    # The cumulative-loss message has different wording from the
    # tail-silence message; confirm we got the right one.
    assert "under-delivered" in msgs[0]
    assert "90" in msgs[0]


def test_mic_warning_mentions_overflow_count_when_present(qt_app, tmp_path):
    """The cumulative-loss message should hint at paInputOverflow when
    PortAudio flagged it, so the diagnostic trail is clear."""
    rec = _make_mic(tmp_path)
    rec._last_callback_wallclock = 100.0  # noqa: SLF001
    rec._stop_wallclock = 106.0  # noqa: SLF001
    rec._trailing_pad_frames = 60 * rec._native_rate  # noqa: SLF001
    rec._input_overflow_count = 42  # noqa: SLF001
    msgs: list[str] = []
    rec.capture_warning.connect(msgs.append)
    rec._check_for_trailing_capture_stall()  # noqa: SLF001
    qt_app.processEvents()
    assert len(msgs) == 1
    assert "paInputOverflow" in msgs[0]
    assert "42" in msgs[0]


def test_mic_no_double_warning_when_both_conditions_hold(qt_app, tmp_path):
    """If the recorder both stopped getting callbacks AND has a large
    trailing pad, only one warning should fire (the tail-silence message
    has priority since it points at the more specific cause)."""
    rec = _make_mic(tmp_path)
    rec._last_callback_wallclock = 100.0  # noqa: SLF001
    rec._stop_wallclock = 130.0  # 30s gap -- crosses both thresholds
    rec._trailing_pad_frames = 60 * rec._native_rate  # noqa: SLF001
    msgs: list[str] = []
    rec.capture_warning.connect(msgs.append)
    rec._check_for_trailing_capture_stall()  # noqa: SLF001
    qt_app.processEvents()
    assert len(msgs) == 1
    # Tail-silence path took priority -- look for its distinctive wording.
    assert "stopped delivering audio" in msgs[0]


def test_loopback_emits_warning_on_cumulative_loss(qt_app, tmp_path):
    """Same cumulative-loss path on the loopback side, different cause hint."""
    rec = _make_loopback(tmp_path)
    rec._last_callback_wallclock = 50.0  # noqa: SLF001
    rec._stop_wallclock = 55.0  # noqa: SLF001
    rec._trailing_pad_frames = 45 * rec._native_rate  # noqa: SLF001
    msgs: list[str] = []
    rec.capture_warning.connect(msgs.append)
    rec._check_for_trailing_capture_stall()  # noqa: SLF001
    qt_app.processEvents()
    assert len(msgs) == 1
    assert "under-delivered" in msgs[0]
    assert "45" in msgs[0]


# ---- chunk_buffer = None contract (issue #47) -------------------------------


def test_mic_accepts_none_chunk_buffer(tmp_path):
    """In capture-only mode the controller passes chunk_buffer=None. The
    recorder must construct cleanly + not crash later when the callback
    would have called chunk_buffer.write."""
    from meeting_notetaker.audio.mic_recorder import MicRecorder
    rec = MicRecorder(None, tmp_path / "mic.wav")
    assert rec.chunk_buffer is None


def test_loopback_accepts_none_chunk_buffer(tmp_path):
    """Same contract as the mic side."""
    from meeting_notetaker.audio.loopback_recorder import LoopbackRecorder
    rec = LoopbackRecorder(None, tmp_path / "sys.wav")
    assert rec.chunk_buffer is None
