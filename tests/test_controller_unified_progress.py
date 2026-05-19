"""Unified post-Stop progress: batch + refinement share one 0..100 bar.

The percent label in the UI is driven by a single signal
(`batch_progress`) whose value spans both phases. These tests pin the
phase-plan math so 100% truly means done -- the previous behavior left
the label stuck at 100 during diarization, which is the bug this
addresses.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtCore")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from meeting_notetaker.controller import SessionController
from meeting_notetaker.models.session import SessionStore
from meeting_notetaker.utils.config import Config
from meeting_notetaker.utils.paths import db_path


@pytest.fixture
def qt_app():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _controller(qt_app, isolated_data_dir) -> SessionController:
    isolated_data_dir.mkdir(parents=True, exist_ok=True)
    store = SessionStore(db_path())
    return SessionController(store, Config())


# ---- _build_phase_plan --------------------------------------------------


def test_phase_plan_both_phases_splits_70_30(qt_app, isolated_data_dir):
    c = _controller(qt_app, isolated_data_dir)
    plan = c._build_phase_plan(will_run_batch=True, will_run_refinement=True)
    assert plan == {"batch": (0, 70), "refinement": (70, 100)}


def test_phase_plan_batch_only_fills_entire_bar(qt_app, isolated_data_dir):
    c = _controller(qt_app, isolated_data_dir)
    plan = c._build_phase_plan(will_run_batch=True, will_run_refinement=False)
    assert plan == {"batch": (0, 100)}


def test_phase_plan_refinement_only_fills_entire_bar(qt_app, isolated_data_dir):
    c = _controller(qt_app, isolated_data_dir)
    plan = c._build_phase_plan(will_run_batch=False, will_run_refinement=True)
    assert plan == {"refinement": (0, 100)}


def test_phase_plan_no_phases(qt_app, isolated_data_dir):
    c = _controller(qt_app, isolated_data_dir)
    plan = c._build_phase_plan(will_run_batch=False, will_run_refinement=False)
    assert plan == {}


# ---- _emit_unified_progress ---------------------------------------------


def test_emit_unified_maps_batch_into_first_slice(qt_app, isolated_data_dir):
    c = _controller(qt_app, isolated_data_dir)
    c._phase_plans["s"] = {"batch": (0, 70), "refinement": (70, 100)}
    emitted: list[int] = []
    c.batch_progress.connect(lambda _sid, pct: emitted.append(pct))
    c._emit_unified_progress("s", "batch", 0)
    c._emit_unified_progress("s", "batch", 50)
    c._emit_unified_progress("s", "batch", 100)
    assert emitted == [0, 35, 70]


def test_emit_unified_maps_refinement_into_second_slice(qt_app, isolated_data_dir):
    c = _controller(qt_app, isolated_data_dir)
    c._phase_plans["s"] = {"batch": (0, 70), "refinement": (70, 100)}
    emitted: list[int] = []
    c.batch_progress.connect(lambda _sid, pct: emitted.append(pct))
    c._emit_unified_progress("s", "refinement", 0)
    c._emit_unified_progress("s", "refinement", 50)
    c._emit_unified_progress("s", "refinement", 100)
    # refinement 0..100 -> unified 70..100
    assert emitted == [70, 85, 100]


def test_emit_unified_single_phase_fills_to_100(qt_app, isolated_data_dir):
    """When only one phase runs, that phase's 100 IS the bar's 100."""
    c = _controller(qt_app, isolated_data_dir)
    c._phase_plans["s"] = {"batch": (0, 100)}
    emitted: list[int] = []
    c.batch_progress.connect(lambda _sid, pct: emitted.append(pct))
    c._emit_unified_progress("s", "batch", 100)
    assert emitted == [100]


def test_emit_unified_ignores_phase_not_in_plan(qt_app, isolated_data_dir):
    """If only batch will run, a stray refinement-progress emit must
    not advance the bar (no double-counting if the phases somehow
    overlap)."""
    c = _controller(qt_app, isolated_data_dir)
    c._phase_plans["s"] = {"batch": (0, 100)}
    emitted: list[int] = []
    c.batch_progress.connect(lambda _sid, pct: emitted.append(pct))
    c._emit_unified_progress("s", "refinement", 100)
    assert emitted == []


def test_emit_unified_safety_falls_through_when_no_plan(qt_app, isolated_data_dir):
    """Defensive: if no phase plan exists (programming error / hot-path
    timing), the raw percent is still forwarded so the UI never gets
    stuck at the previous value."""
    c = _controller(qt_app, isolated_data_dir)
    emitted: list[int] = []
    c.batch_progress.connect(lambda _sid, pct: emitted.append(pct))
    c._emit_unified_progress("unknown_session", "batch", 42)
    assert emitted == [42]


def test_emit_unified_clamps_out_of_range_raw(qt_app, isolated_data_dir):
    c = _controller(qt_app, isolated_data_dir)
    c._phase_plans["s"] = {"batch": (0, 70), "refinement": (70, 100)}
    emitted: list[int] = []
    c.batch_progress.connect(lambda _sid, pct: emitted.append(pct))
    c._emit_unified_progress("s", "batch", -10)
    c._emit_unified_progress("s", "batch", 200)
    assert emitted == [0, 70]
