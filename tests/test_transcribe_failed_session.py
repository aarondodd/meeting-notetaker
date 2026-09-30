"""SessionController.transcribe_failed_session: recover a crashed recording."""
from __future__ import annotations

import os
import sys
import wave

import numpy as np
import pytest

pytest.importorskip("PyQt6.QtCore")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from meeting_notetaker.controller import SessionController
from meeting_notetaker.models.session import (
    STATE_ERROR,
    STATE_PROCESSING,
    SessionStore,
)
from meeting_notetaker.utils.config import Config
from meeting_notetaker.utils.paths import db_path, session_audio_dir


@pytest.fixture
def qt_app():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _wav(path, samples, *, rate=48000, channels=2):
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(np.asarray(samples, dtype=np.int16).tobytes())


@pytest.fixture
def ctl(qt_app, isolated_data_dir, monkeypatch):
    isolated_data_dir.mkdir(parents=True, exist_ok=True)
    store = SessionStore(db_path())
    controller = SessionController(store, Config())
    calls = []
    monkeypatch.setattr(
        controller, "_start_disk_processing",
        lambda session, **kw: calls.append((session.id, kw)),
    )
    return controller, store, calls


def _failed_session(store):
    s = store.create_session(title="Crashed", retain_audio=False)
    store.update_session(s.id, state=STATE_ERROR, started_at="2026-09-29T15:03:30Z")
    return store.get_session(s.id)


def _wait_for(qt_app, predicate, timeout_ms=5000):
    from PyQt6.QtCore import QElapsedTimer
    t = QElapsedTimer()
    t.start()
    while not predicate() and t.elapsed() < timeout_ms:
        qt_app.processEvents()
    return predicate()


def test_mixes_orphaned_sidecars_then_processes(ctl, qt_app):
    controller, store, calls = ctl
    s = _failed_session(store)
    d = session_audio_dir(s.id)
    _wav(d / "mic.wav", np.ones(48000), channels=1)
    _wav(d / "sys.0.wav", np.full(96000, 100))
    _wav(d / "sys.1.wav", np.full(96000, 300))

    assert controller.transcribe_failed_session(s) is True
    assert store.get_session(s.id).state == STATE_PROCESSING
    assert _wait_for(qt_app, lambda: bool(calls))

    sid, kw = calls[0]
    assert sid == s.id
    assert kw["mic_wav"] == d / "mic.wav"
    assert kw["sys_wav"] == d / "sys.wav"
    assert kw["force_batch"] is True
    assert kw["keep_audio"] is True
    assert kw["run_diarization"] is True
    assert kw["duration_seconds"] == 1
    assert kw["ended_at"] == "2026-09-29T15:03:31Z"
    with wave.open(str(d / "sys.wav"), "rb") as rf:
        mixed = np.frombuffer(rf.readframes(rf.getnframes()), dtype=np.int16)
    assert set(mixed.tolist()) == {200}
    assert not (d / "sys.0.wav").exists()
    assert not (d / "sys.1.wav").exists()
    assert controller._recovery_mixers == {}


def test_existing_sys_wav_skips_the_mix(ctl):
    controller, store, calls = ctl
    s = _failed_session(store)
    d = session_audio_dir(s.id)
    _wav(d / "sys.wav", np.ones(4800))
    assert controller.transcribe_failed_session(s) is True
    assert calls and calls[0][1]["sys_wav"] == d / "sys.wav"
    assert calls[0][1]["mic_wav"] is None


def test_refuses_without_audio(ctl):
    controller, store, calls = ctl
    s = _failed_session(store)
    errors = []
    controller.error.connect(errors.append)
    assert controller.transcribe_failed_session(s) is False
    assert errors and "No recording" in errors[0]
    assert store.get_session(s.id).state == STATE_ERROR
    assert calls == []


def test_refuses_while_recording(ctl):
    controller, store, calls = ctl
    s = _failed_session(store)
    _wav(session_audio_dir(s.id) / "mic.wav", np.ones(480), channels=1)
    controller._active_recording_session = store.create_session(title="live")
    assert controller.transcribe_failed_session(s) is False
    assert calls == []


def test_keep_audio_survives_finalize_for_non_retained_session(qt_app, isolated_data_dir):
    """The real finalize must not delete a recovered recording's WAVs."""
    from meeting_notetaker.controller import _ProcessingState
    isolated_data_dir.mkdir(parents=True, exist_ok=True)
    store = SessionStore(db_path())
    cfg = Config()
    cfg.audio.retain_format = "wav"
    controller = SessionController(store, cfg)
    s = _failed_session(store)
    mic = session_audio_dir(s.id) / "mic.wav"
    _wav(mic, np.ones(480), channels=1)
    controller._processing_sessions[s.id] = _ProcessingState(
        session=s, mic_wav=mic, sys_wav=None, live_segments=[], keep_audio=True,
    )
    controller._finalize_session(s.id, batch_segments=None)
    assert mic.exists()
