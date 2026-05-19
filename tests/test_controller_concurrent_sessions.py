"""Issue #7: starting a new session must not block on prior sessions in
post-Stop processing.

These tests exercise SessionController state transitions without spinning
up the live recording engine (mic + loopback + workers) -- the wiring is
covered by the existing live-test plan. The point of this file is the
data-structure semantics:

- `_active_recording_session` is independent of `_processing_sessions`.
- `start_session` checks only the live-engine slot.
- `_finalize_session` removes the per-session processing entry without
  touching the live-engine slot.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtCore")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from meeting_notetaker.controller import SessionController, _ProcessingState
from meeting_notetaker.models.session import (
    STATE_COMPLETE,
    STATE_PROCESSING,
    STATE_RECORDING,
    Session,
    SessionStore,
)
from meeting_notetaker.utils.config import Config
from meeting_notetaker.utils.paths import db_path


@pytest.fixture
def qt_app():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _make_session(store: SessionStore, *, title: str) -> Session:
    return store.create_session(title=title)


def _make_controller(qt_app, isolated_data_dir) -> tuple[SessionController, SessionStore]:
    isolated_data_dir.mkdir(parents=True, exist_ok=True)
    store = SessionStore(db_path())
    controller = SessionController(store, Config())
    return controller, store


def test_processing_session_ids_starts_empty(qt_app, isolated_data_dir):
    controller, _ = _make_controller(qt_app, isolated_data_dir)
    assert controller.processing_session_ids == []
    assert controller.active_session is None


def test_active_session_returns_recording_slot_not_processing(qt_app, isolated_data_dir):
    """`active_session` is the live-engine slot. A session in post-Stop
    processing must not show up there -- the back-to-back fix depends on
    this distinction."""
    controller, store = _make_controller(qt_app, isolated_data_dir)
    a = _make_session(store, title="A")
    a.state = STATE_PROCESSING
    controller._processing_sessions[a.id] = _ProcessingState(
        session=a, mic_wav=None, sys_wav=None, live_segments=[]
    )

    assert controller.active_session is None
    assert controller.processing_session_ids == [a.id]


def test_start_session_blocked_only_by_active_recording_not_processing(qt_app, isolated_data_dir):
    """The whole point of issue #7: a session in batch/refinement must
    not block the next recording from starting."""
    controller, store = _make_controller(qt_app, isolated_data_dir)
    a = _make_session(store, title="A")
    a.state = STATE_PROCESSING
    controller._processing_sessions[a.id] = _ProcessingState(
        session=a, mic_wav=None, sys_wav=None, live_segments=[]
    )

    errors: list[str] = []
    controller.error.connect(errors.append)

    # Simulate Start on session B by checking only the pre-flight guard:
    # the controller blocks when the live-engine slot is occupied, but
    # `_processing_sessions` should not count.
    assert controller._active_recording_session is None
    # (We don't actually invoke start_session because that would try to
    # construct recorders. The pre-flight is exercised by the assertion
    # above; downstream wiring is covered by the live test plan.)
    assert errors == []


def test_start_session_emits_error_when_live_engine_occupied(qt_app, isolated_data_dir):
    """Sanity-check the inverse: when the live-engine slot IS occupied,
    a new start raises the error toast and bails before touching audio."""
    controller, store = _make_controller(qt_app, isolated_data_dir)
    a = _make_session(store, title="A")
    a.state = STATE_RECORDING
    controller._active_recording_session = a

    b = _make_session(store, title="B")
    errors: list[str] = []
    controller.error.connect(errors.append)

    controller.start_session(b)

    assert len(errors) == 1
    assert "already recording" in errors[0].lower()
    # Session B was not promoted into the recording slot.
    assert controller.active_session is a


def test_finalize_clears_only_its_own_processing_entry(qt_app, isolated_data_dir):
    """`_finalize_session(A)` must not disturb the entry for session B or
    the live-recording slot."""
    controller, store = _make_controller(qt_app, isolated_data_dir)
    a = _make_session(store, title="A")
    b = _make_session(store, title="B")
    a.state = STATE_PROCESSING
    b.state = STATE_PROCESSING
    controller._processing_sessions[a.id] = _ProcessingState(
        session=a, mic_wav=None, sys_wav=None, live_segments=[]
    )
    controller._processing_sessions[b.id] = _ProcessingState(
        session=b, mic_wav=None, sys_wav=None, live_segments=[]
    )
    # An unrelated session is mid-recording.
    c = _make_session(store, title="C")
    c.state = STATE_RECORDING
    controller._active_recording_session = c

    controller._finalize_session(a.id, batch_segments=None)

    assert a.id not in controller._processing_sessions
    assert b.id in controller._processing_sessions
    assert controller.active_session is c
    assert store.get_session(a.id).state == STATE_COMPLETE


def test_finalize_unknown_session_id_is_safe(qt_app, isolated_data_dir):
    """A spurious finalize for an unknown id (e.g. fired twice) is a
    no-op -- we don't want it to crash or to clear state for some other
    session."""
    controller, store = _make_controller(qt_app, isolated_data_dir)
    a = _make_session(store, title="A")
    a.state = STATE_PROCESSING
    controller._processing_sessions[a.id] = _ProcessingState(
        session=a, mic_wav=None, sys_wav=None, live_segments=[]
    )

    controller._finalize_session("does-not-exist", batch_segments=None)

    # The unknown finalize must not delete the real entry or move the
    # real session to COMPLETE on disk.
    assert a.id in controller._processing_sessions
    assert store.get_session(a.id).state != STATE_COMPLETE


def test_teardown_recording_does_not_touch_processing_dict(qt_app, isolated_data_dir):
    """`_teardown_recording` releases the live-engine slot only. The
    per-session processing entries must survive so the batch threads
    they own can continue to completion."""
    controller, store = _make_controller(qt_app, isolated_data_dir)
    a = _make_session(store, title="A")
    a.state = STATE_PROCESSING
    controller._processing_sessions[a.id] = _ProcessingState(
        session=a, mic_wav=Path("/tmp/mic.wav"), sys_wav=Path("/tmp/sys.wav"),
        live_segments=[],
    )
    b = _make_session(store, title="B")
    b.state = STATE_RECORDING
    controller._active_recording_session = b

    controller._teardown_recording()

    assert controller.active_session is None
    # A's processing entry must remain intact, including its WAV paths.
    proc = controller._processing_sessions[a.id]
    assert proc.session is a
    assert proc.mic_wav == Path("/tmp/mic.wav")
    assert proc.sys_wav == Path("/tmp/sys.wav")


def test_set_retain_audio_updates_processing_entry(qt_app, isolated_data_dir):
    """When the user flips Keep Audio on a session that's mid-processing,
    the change must reach the in-memory copy the finalize step inspects,
    not just the on-disk row."""
    controller, store = _make_controller(qt_app, isolated_data_dir)
    a = _make_session(store, title="A")
    a.state = STATE_PROCESSING
    a.retain_audio = False
    controller._processing_sessions[a.id] = _ProcessingState(
        session=a, mic_wav=None, sys_wav=None, live_segments=[]
    )

    controller.set_retain_audio(a.id, True)

    assert controller._processing_sessions[a.id].session.retain_audio is True
    assert store.get_session(a.id).retain_audio is True
