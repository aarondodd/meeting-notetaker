"""paths.recoverable_recording: find what a crashed capture left behind."""
from __future__ import annotations

import wave

from meeting_notetaker.utils.paths import recoverable_recording, session_audio_dir


def _wav(path, frames=100):
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x01\x00" * frames)


def test_no_audio_dir_is_not_usable(isolated_data_dir):
    rec = recoverable_recording("nope")
    assert not rec.usable


def test_crashed_multi_endpoint_capture(isolated_data_dir):
    d = session_audio_dir("s1")
    _wav(d / "mic.wav")
    for i in (0, 1, 10, 2):
        _wav(d / f"sys.{i}.wav")
    _wav(d / "sys.bak.wav")          # not a sidecar name
    _wav(d / "sys.3.wav", frames=0)  # header only
    rec = recoverable_recording("s1")
    assert rec.usable
    assert rec.mic == d / "mic.wav"
    assert rec.sys is None
    assert [p.name for p in rec.sidecars] == ["sys.0.wav", "sys.1.wav", "sys.2.wav", "sys.10.wav"]


def test_finalized_opus_only_session_is_not_recoverable(isolated_data_dir):
    d = session_audio_dir("s2")
    (d / "mic.opus").write_bytes(b"x" * 1000)
    (d / "sys.opus").write_bytes(b"x" * 1000)
    assert not recoverable_recording("s2").usable
