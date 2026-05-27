"""OutlookCalendarMonitor: tick dispatches a worker thread (issue #48).

The COM call inside `fetch_imminent_meetings` runs ~800-1000 ms on
the main thread when invoked directly; before #48 the QTimer fired
it every 60 s and the UI stuttered audibly. The fix dispatches a
worker QThread; the main thread only emits `meeting_imminent` for
newly-seen meetings when the worker reports back.

These tests pin the dispatch contract without needing a real
Outlook install. fetch_imminent_meetings is monkey-patched to
return synthetic data; the tests then assert that the worker is
spawned correctly, the signal lands on the main thread, the dedup
gate works, and concurrent ticks don't overlap.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

pytest.importorskip("PyQt6.QtCore")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication  # noqa: E402

from meeting_notetaker.integrations import outlook_calendar  # noqa: E402
from meeting_notetaker.integrations.outlook_calendar import (  # noqa: E402
    MeetingInfo,
    OutlookCalendarMonitor,
)


@pytest.fixture(scope="module")
def qt_app():
    return QCoreApplication.instance() or QCoreApplication(sys.argv)


def _make_monitor(tmp_path) -> OutlookCalendarMonitor:
    """Construct a monitor with a tmp dedup state file. start() is
    NOT called -- we drive _tick directly so we can sequence the
    test against the worker."""
    return OutlookCalendarMonitor(
        tmp_path / "dedup.json", window_minutes=5, poll_interval_sec=60,
    )


def _meeting(entry_id: str, *, subject: str = "Test") -> MeetingInfo:
    from datetime import datetime, timezone, timedelta
    start = datetime.now(timezone.utc) + timedelta(minutes=2)
    return MeetingInfo(
        entry_id=entry_id,
        subject=subject,
        start_time=start,
        end_time=start + timedelta(minutes=30),
    )


def _wait_for_worker(monitor, qt_app, *, timeout: float = 5.0) -> None:
    """Spin processEvents until the monitor's in-flight worker (if
    any) has finished + been retired. Required because the worker
    runs on a real QThread + emits a queued signal back to the main
    thread."""
    deadline = time.monotonic() + timeout
    qt_app.processEvents()
    while monitor._worker is not None and time.monotonic() < deadline:  # noqa: SLF001
        qt_app.processEvents()
        time.sleep(0.01)
    qt_app.processEvents()


# ---- worker dispatch -------------------------------------------------------


def test_tick_dispatches_worker_without_blocking(qt_app, tmp_path, monkeypatch):
    """The whole point of issue #48: _tick must return quickly. We
    measure the wall-clock cost of calling _tick() with a synthetic
    fetch that sleeps for 200 ms; the call itself should return in
    well under that since the sleep runs on the worker."""
    def slow_fetch(window_minutes):
        time.sleep(0.2)
        return [_meeting("m1")]
    monkeypatch.setattr(
        outlook_calendar, "fetch_imminent_meetings", slow_fetch,
    )
    monitor = _make_monitor(tmp_path)

    start = time.monotonic()
    monitor._tick()  # noqa: SLF001
    elapsed = time.monotonic() - start
    assert elapsed < 0.05, (
        f"_tick must dispatch off-thread; took {elapsed * 1000:.0f}ms"
    )
    # The worker should be running at this point.
    assert monitor._worker is not None  # noqa: SLF001
    # Let it finish + verify the result lands.
    _wait_for_worker(monitor, qt_app)


def test_tick_skips_when_prior_worker_still_running(qt_app, tmp_path, monkeypatch):
    """Two _tick calls in rapid succession should produce only ONE
    worker. Overlapping COM access from two threads is asking for
    trouble; skip the new tick rather than queue it behind the prior."""
    block = []  # accumulates a sentinel so we can release the slow fetch

    def slow_fetch(window_minutes):
        # Spin until we're allowed to return. The second _tick call
        # has to land while this is still running.
        for _ in range(100):
            if block:
                break
            time.sleep(0.01)
        return [_meeting("m1")]

    monkeypatch.setattr(
        outlook_calendar, "fetch_imminent_meetings", slow_fetch,
    )
    monitor = _make_monitor(tmp_path)

    monitor._tick()  # noqa: SLF001
    first_worker = monitor._worker  # noqa: SLF001
    assert first_worker is not None
    # Second call while the first is still in fetch -- should be a no-op.
    monitor._tick()  # noqa: SLF001
    assert monitor._worker is first_worker, (  # noqa: SLF001
        "second _tick must not replace the in-flight worker"
    )
    # Release the fetch + let the first worker finish.
    block.append(1)
    _wait_for_worker(monitor, qt_app)


def test_on_fetch_done_emits_meeting_imminent_for_new(qt_app, tmp_path):
    """The slot that runs on the main thread when the worker reports
    back: emit meeting_imminent for each unseen meeting."""
    monitor = _make_monitor(tmp_path)
    captured = []
    monitor.meeting_imminent.connect(captured.append)
    monitor._on_fetch_done([_meeting("a"), _meeting("b")])  # noqa: SLF001
    qt_app.processEvents()
    assert len(captured) == 2
    assert {m.entry_id for m in captured} == {"a", "b"}


def test_on_fetch_done_skips_already_seen(qt_app, tmp_path):
    """Dedup gate: if the entry_id is in the dedup store, no signal."""
    monitor = _make_monitor(tmp_path)
    # Pre-mark "a" as seen.
    monitor._dedup.mark_seen("a")  # noqa: SLF001
    captured = []
    monitor.meeting_imminent.connect(captured.append)
    monitor._on_fetch_done([_meeting("a"), _meeting("b")])  # noqa: SLF001
    qt_app.processEvents()
    assert len(captured) == 1
    assert captured[0].entry_id == "b"


def test_full_round_trip_via_worker(qt_app, tmp_path, monkeypatch):
    """End-to-end: _tick dispatches a worker that runs the (mocked)
    fetch; the result lands as a meeting_imminent signal on the main
    thread. Proves the worker thread + queued connection are wired
    correctly."""
    def fake_fetch(window_minutes):
        return [_meeting("x"), _meeting("y")]
    monkeypatch.setattr(
        outlook_calendar, "fetch_imminent_meetings", fake_fetch,
    )
    monitor = _make_monitor(tmp_path)
    captured = []
    monitor.meeting_imminent.connect(captured.append)
    monitor._tick()  # noqa: SLF001
    _wait_for_worker(monitor, qt_app)
    assert {m.entry_id for m in captured} == {"x", "y"}


def test_worker_handles_fetch_exception_gracefully(qt_app, tmp_path, monkeypatch):
    """A raise inside fetch_imminent_meetings must not crash the
    worker thread + must not abort the next tick. The worker emits
    done([]) and the monitor moves on."""
    def boom_fetch(window_minutes):
        raise RuntimeError("simulated COM failure")
    monkeypatch.setattr(
        outlook_calendar, "fetch_imminent_meetings", boom_fetch,
    )
    monitor = _make_monitor(tmp_path)
    captured = []
    monitor.meeting_imminent.connect(captured.append)
    monitor._tick()  # noqa: SLF001
    _wait_for_worker(monitor, qt_app)
    # No meetings + monitor is ready for the next tick.
    assert captured == []
    assert monitor._worker is None  # noqa: SLF001
