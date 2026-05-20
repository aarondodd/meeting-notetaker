"""SessionController.tag_speaker() -- per-session click-to-tag plumbing.

Exercises the controller's tag persistence + pause-aware elapsed-time
accounting without spinning up the live audio engine. The recording
state is stubbed by directly manipulating
`controller._active_recording_session` + the per-session tag store, so
the tests stay portable across hosts without PortAudio.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

pytest.importorskip("PyQt6.QtCore")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from meeting_notetaker.controller import SessionController
from meeting_notetaker.models.session import (
    STATE_PAUSED,
    STATE_RECORDING,
    Session,
    SessionStore,
)
from meeting_notetaker.models.speaker_tags import SpeakerTagStore
from meeting_notetaker.utils.config import Config
from meeting_notetaker.utils.paths import db_path, session_dir


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


def _prime_recording(controller: SessionController, session: Session) -> None:
    """Bring `controller` into the same internal state `start_session`
    would land in, without invoking the live recording engine."""
    session.state = STATE_RECORDING
    controller._active_recording_session = session
    controller._t_start_wall = time.monotonic()
    controller._active_elapsed_accumulated_sec = 0.0
    controller._active_span_start_monotonic = controller._t_start_wall
    controller._tag_stores[session.id] = SpeakerTagStore(session_dir(session.id))


def test_tag_speaker_is_noop_when_no_active_session(qt_app, isolated_data_dir):
    controller, _ = _make_controller(qt_app, isolated_data_dir)
    emitted: list[tuple[str, dict]] = []
    controller.speaker_tags_changed.connect(lambda sid, c: emitted.append((sid, c)))
    controller.tag_speaker("Pat")
    assert emitted == []


def test_tag_speaker_strips_and_rejects_empty(qt_app, isolated_data_dir):
    controller, store = _make_controller(qt_app, isolated_data_dir)
    session = _make_session(store, title="t")
    _prime_recording(controller, session)
    emitted: list[tuple[str, dict]] = []
    controller.speaker_tags_changed.connect(lambda sid, c: emitted.append((sid, c)))
    controller.tag_speaker("   ")
    controller.tag_speaker("")
    assert emitted == []


def test_tag_speaker_persists_and_emits_counts(qt_app, isolated_data_dir):
    controller, store = _make_controller(qt_app, isolated_data_dir)
    session = _make_session(store, title="t")
    _prime_recording(controller, session)
    emitted: list[tuple[str, dict]] = []
    controller.speaker_tags_changed.connect(lambda sid, c: emitted.append((sid, c)))

    controller.tag_speaker("Pat")
    controller.tag_speaker("Sam")
    controller.tag_speaker("Pat")

    counts_seen = [c for _sid, c in emitted]
    assert counts_seen[-1] == {"Pat": 2, "Sam": 1}

    # On-disk persistence.
    persisted = controller._tag_stores[session.id].load()
    assert [t.name for t in persisted] == ["Pat", "Sam", "Pat"]
    # Each tag captured a non-negative t_seconds.
    assert all(t.t_seconds >= 0 for t in persisted)


def test_tag_speaker_works_while_paused(qt_app, isolated_data_dir):
    """User should be able to tag mid-pause -- the tag time stays
    WAV-aligned because pause doesn't advance the elapsed accumulator."""
    controller, store = _make_controller(qt_app, isolated_data_dir)
    session = _make_session(store, title="t")
    _prime_recording(controller, session)
    # Simulate a pause: roll the active span into accumulator + null the
    # active-span start the way pause_session() would.
    elapsed_before_pause = time.monotonic() - controller._active_span_start_monotonic
    controller._active_elapsed_accumulated_sec += elapsed_before_pause
    controller._active_span_start_monotonic = None
    session.state = STATE_PAUSED

    emitted: list[dict] = []
    controller.speaker_tags_changed.connect(lambda _sid, c: emitted.append(c))
    controller.tag_speaker("Pat")
    assert emitted and emitted[-1] == {"Pat": 1}


def test_remove_last_speaker_tag_undoes_most_recent(qt_app, isolated_data_dir):
    controller, store = _make_controller(qt_app, isolated_data_dir)
    session = _make_session(store, title="t")
    _prime_recording(controller, session)
    controller.tag_speaker("Pat")
    controller.tag_speaker("Pat")
    controller.tag_speaker("Sam")

    emitted: list[dict] = []
    controller.speaker_tags_changed.connect(lambda _sid, c: emitted.append(c))
    controller.remove_last_speaker_tag("Pat")
    assert emitted[-1] == {"Pat": 1, "Sam": 1}


def test_self_improve_store_from_tagged_clusters(qt_app, isolated_data_dir):
    """Each tagged-cluster centroid lands in the cross-meeting SpeakerStore
    via add_sample so the store self-improves on each meeting."""
    import numpy as np
    from meeting_notetaker.diarization.refiner import (
        ClusterSummary,
        RefinementResult,
    )

    controller, _ = _make_controller(qt_app, isolated_data_dir)
    # Hand-build a refinement result with one tagged cluster + one untagged.
    centroid_pat = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    centroid_unknown = np.array([0.0, 0.0, 1.0], dtype=np.float32)
    result = RefinementResult(
        turns=[],
        clusters=[
            ClusterSummary(
                cluster_id=0, centroid=centroid_pat, turn_indices=[0],
                name="Pat", was_user_tagged=True,
            ),
            ClusterSummary(
                cluster_id=1, centroid=centroid_unknown, turn_indices=[1],
                name=None, was_user_tagged=False,
            ),
        ],
        segment_labels=[],
    )

    controller._self_improve_store_from_tagged_clusters(result)

    # Open a fresh speaker store and verify Pat now has a sample; the
    # untagged cluster was NOT auto-named (would defeat the walker).
    from meeting_notetaker.diarization.store import open_speaker_store

    store = open_speaker_store()
    try:
        pat = store.get_by_name("Pat")
        assert pat is not None
        assert pat.sample_count == 1
        # No phantom entries for the untagged cluster.
        assert store.get_by_name("Speaker 2") is None
    finally:
        store.close()


def test_recording_active_elapsed_excludes_pause_time(qt_app, isolated_data_dir):
    """The elapsed accounting must subtract pause durations so tag
    timestamps stay aligned with WAV time."""
    controller, store = _make_controller(qt_app, isolated_data_dir)
    session = _make_session(store, title="t")
    _prime_recording(controller, session)
    # Accumulate 10s of "real recording" and then 5 minutes of "pause"
    # in synthetic monotonic terms.
    controller._active_elapsed_accumulated_sec = 10.0
    controller._active_span_start_monotonic = None
    session.state = STATE_PAUSED
    elapsed_during_pause = controller._recording_active_elapsed_sec()
    assert elapsed_during_pause == pytest.approx(10.0)

    # Resume: start a fresh active span and ensure elapsed grows from
    # the previous accumulated value, not from 0.
    controller._active_span_start_monotonic = time.monotonic()
    session.state = STATE_RECORDING
    immediately_after_resume = controller._recording_active_elapsed_sec()
    assert immediately_after_resume == pytest.approx(10.0, abs=0.1)
