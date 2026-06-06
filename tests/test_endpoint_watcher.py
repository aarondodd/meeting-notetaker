"""Tests for the WASAPI endpoint hot-plug watcher (#85.6).

Pure-Python -- drives the debouncer with explicit wallclocks so
the window-expiry logic is deterministic. The live EndpointWatcher
needs Windows + PyAudioWPatch + Qt and is exercised end-to-end
behind the `audio` marker.
"""
from __future__ import annotations

from meeting_notetaker.audio.endpoint_watcher import (
    DEFAULT_DEBOUNCE_SEC,
    EndpointChange,
    EndpointDebouncer,
    is_pycaw_available,
)


# ---- EndpointDebouncer ---------------------------------------------------

def test_seed_then_no_change_returns_none():
    d = EndpointDebouncer(window_sec=2.0)
    d.seed({"A", "B"})
    assert d.observe({"A", "B"}, wallclock=0.0) is None
    assert d.observe({"A", "B"}, wallclock=5.0) is None


def test_added_endpoint_held_for_window_fires_once():
    d = EndpointDebouncer(window_sec=2.0)
    d.seed({"A"})
    # First observation: change seen, debouncer starts the window.
    assert d.observe({"A", "B"}, wallclock=0.0) is None
    # Same snapshot, still within window: no fire.
    assert d.observe({"A", "B"}, wallclock=1.5) is None
    # Same snapshot, window satisfied: fires.
    change = d.observe({"A", "B"}, wallclock=2.5)
    assert change == EndpointChange(added=("B",), removed=())
    # After firing the committed set advances, so subsequent stable
    # observations don't refire.
    assert d.observe({"A", "B"}, wallclock=10.0) is None


def test_added_then_reverted_does_not_fire():
    """USB connector flap: device appears, disappears within window."""
    d = EndpointDebouncer(window_sec=2.0)
    d.seed({"A"})
    assert d.observe({"A", "B"}, wallclock=0.0) is None
    # Revert to seed within window -> debouncer clears pending.
    assert d.observe({"A"}, wallclock=1.0) is None
    # Even past the original window, no fire.
    assert d.observe({"A"}, wallclock=5.0) is None


def test_removed_endpoint_fires_after_window():
    d = EndpointDebouncer(window_sec=2.0)
    d.seed({"A", "B"})
    assert d.observe({"A"}, wallclock=0.0) is None
    change = d.observe({"A"}, wallclock=2.0)
    assert change == EndpointChange(added=(), removed=("B",))


def test_added_and_removed_simultaneously():
    d = EndpointDebouncer(window_sec=2.0)
    d.seed({"A", "B"})
    assert d.observe({"A", "C"}, wallclock=0.0) is None
    change = d.observe({"A", "C"}, wallclock=2.0)
    assert change.added == ("C",)
    assert change.removed == ("B",)


def test_changing_pending_resets_the_window():
    """If the snapshot moves to a new value mid-window, the timer
    restarts from there rather than firing for the new state too
    early."""
    d = EndpointDebouncer(window_sec=2.0)
    d.seed({"A"})
    assert d.observe({"A", "B"}, wallclock=0.0) is None
    # New candidate at 1.5s -- not the same as previous candidate.
    assert d.observe({"A", "B", "C"}, wallclock=1.5) is None
    # 1.5s after the new candidate isn't 2s yet.
    assert d.observe({"A", "B", "C"}, wallclock=3.0) is None
    # 2s after the new candidate fires.
    change = d.observe({"A", "B", "C"}, wallclock=3.5)
    assert change == EndpointChange(added=("B", "C"), removed=())


def test_committed_property_reflects_baseline():
    d = EndpointDebouncer(window_sec=2.0)
    d.seed({"A", "B"})
    assert d.committed == {"A", "B"}
    d.observe({"A", "C"}, wallclock=0.0)
    d.observe({"A", "C"}, wallclock=3.0)
    assert d.committed == {"A", "C"}


def test_default_window_constant_is_reasonable():
    """Two seconds is enough to absorb USB connector chatter without
    making a real hot-plug feel laggy."""
    assert 1.0 <= DEFAULT_DEBOUNCE_SEC <= 5.0


# ---- availability gates --------------------------------------------------

def test_is_pycaw_available_returns_false_on_non_windows(monkeypatch):
    """Pure-Python test envs always read False here."""
    import sys
    monkeypatch.setattr(sys, "platform", "linux")
    assert is_pycaw_available() is False
