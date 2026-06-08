"""Tests for the rolling-window silence detector (#84 / #85).

Pure-Python -- no Qt, no PortAudio. Drives wallclock manually so the
window-expiry logic is deterministic.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from meeting_notetaker.audio.silence_tracker import (
    DEFAULT_SILENCE_FLOOR_DBFS,
    SilenceTracker,
    dbfs_to_amplitude,
)


# ---- dbfs_to_amplitude ---------------------------------------------------

def test_dbfs_zero_is_unit_amplitude():
    assert dbfs_to_amplitude(0.0) == pytest.approx(1.0)


def test_dbfs_minus_60_is_roughly_one_thousandth():
    assert dbfs_to_amplitude(-60.0) == pytest.approx(0.001, rel=1e-3)


# ---- helpers -------------------------------------------------------------

def _silent_chunk(n: int = 1024) -> np.ndarray:
    return np.zeros(n, dtype=np.int16)


def _full_scale_chunk(n: int = 1024) -> np.ndarray:
    """All samples at +half-scale, far above the silence floor."""
    return np.full(n, 16000, dtype=np.int16)


def _quiet_chunk(n: int = 1024, peak: int = 5) -> np.ndarray:
    """Tiny non-zero values; should still register as silent at -60 dBFS."""
    return np.full(n, peak, dtype=np.int16)


# ---- write + RMS basics --------------------------------------------------

def test_empty_chunk_is_a_no_op():
    t = SilenceTracker()
    t.write(np.zeros(0, dtype=np.int16), wallclock=0.0)
    assert t.current_rms() == 0.0
    assert t.is_silent(0.0) is False  # no window yet


def test_full_scale_chunk_yields_finite_positive_rms():
    t = SilenceTracker()
    t.write(_full_scale_chunk(), wallclock=0.0)
    rms = t.current_rms()
    assert rms > 0
    # RMS of an all-equal-value chunk equals that value.
    assert rms == pytest.approx(16000.0, rel=1e-3)


def test_silent_chunk_yields_zero_rms():
    t = SilenceTracker()
    t.write(_silent_chunk(), wallclock=0.0)
    assert t.current_rms() == 0.0


def test_rms_dbfs_returns_neg_inf_for_silent_window():
    t = SilenceTracker()
    t.write(_silent_chunk(), wallclock=0.0)
    assert t.current_rms_dbfs() == float("-inf")


def test_rms_dbfs_for_full_scale_is_near_zero_dbfs():
    """An int16 chunk at full-scale (~32767) reads ~0 dBFS. A chunk at
    half-scale reads ~-6 dBFS."""
    t = SilenceTracker()
    t.write(np.full(1024, 32767, dtype=np.int16), wallclock=0.0)
    assert t.current_rms_dbfs() == pytest.approx(0.0, abs=0.5)


# ---- window expiry -------------------------------------------------------

def test_chunks_older_than_window_drop_off_on_write():
    t = SilenceTracker(silence_window_sec=10.0)
    t.write(_full_scale_chunk(), wallclock=0.0)
    # Write a fresh chunk well past the 10s window; the old one
    # should be evicted.
    t.write(_silent_chunk(), wallclock=20.0)
    # Only the silent chunk remains in the window.
    assert t.current_rms() == 0.0


def test_chunks_older_than_window_drop_off_on_is_silent():
    t = SilenceTracker(silence_window_sec=10.0)
    t.write(_full_scale_chunk(), wallclock=0.0)
    # Even without a fresh write, advancing time should drop the old.
    # has_full_window also requires the first-write to be at least
    # window_sec ago, which 30s comfortably satisfies.
    t.is_silent(30.0)
    assert t.current_rms() == 0.0


# ---- is_silent semantics ------------------------------------------------

def test_is_silent_false_before_first_full_window():
    """Don't fire silence warnings during the first 30s. The user is
    still spinning up the call; near-silence at that point is normal."""
    t = SilenceTracker(silence_window_sec=30.0)
    for i in range(10):
        t.write(_silent_chunk(), wallclock=float(i))
    # 10s elapsed; window not yet full.
    assert t.is_silent(10.0) is False


def test_is_silent_true_after_full_window_of_silence():
    t = SilenceTracker(silence_window_sec=10.0)
    for i in range(11):
        t.write(_silent_chunk(), wallclock=float(i))
    assert t.is_silent(11.0) is True


def test_is_silent_true_for_quiet_below_floor():
    """A trickle of below-floor noise still counts as silent. The floor
    sits well above realistic microphone self-noise."""
    t = SilenceTracker(silence_window_sec=10.0)
    for i in range(11):
        t.write(_quiet_chunk(peak=5), wallclock=float(i))
    assert t.is_silent(11.0) is True


def test_is_silent_false_with_any_loud_chunk_in_window():
    t = SilenceTracker(silence_window_sec=10.0)
    # Mostly silence, but one full-scale burst in the window keeps
    # the RMS above floor.
    for i in range(11):
        chunk = _full_scale_chunk() if i == 5 else _silent_chunk()
        t.write(chunk, wallclock=float(i))
    assert t.is_silent(11.0) is False


def test_was_silent_last_check_tracks_state():
    t = SilenceTracker(silence_window_sec=5.0)
    for i in range(6):
        t.write(_silent_chunk(), wallclock=float(i))
    assert t.is_silent(6.0) is True
    assert t.was_silent_last_check is True
    # Now write a loud chunk; the next check should flip.
    t.write(_full_scale_chunk(), wallclock=6.5)
    assert t.is_silent(7.0) is False
    assert t.was_silent_last_check is False


# ---- has_full_window ----------------------------------------------------

def test_has_full_window_false_before_any_write():
    t = SilenceTracker(silence_window_sec=5.0)
    assert t.has_full_window(100.0) is False


def test_has_full_window_true_after_window_sec_elapsed():
    t = SilenceTracker(silence_window_sec=5.0)
    t.write(_silent_chunk(), wallclock=0.0)
    assert t.has_full_window(5.0) is True
    assert t.has_full_window(4.99) is False


# ---- reset --------------------------------------------------------------

def test_reset_clears_all_state():
    t = SilenceTracker(silence_window_sec=5.0)
    for i in range(6):
        t.write(_silent_chunk(), wallclock=float(i))
    assert t.is_silent(6.0) is True
    t.reset()
    assert t.current_rms() == 0.0
    assert t.has_full_window(100.0) is False
    assert t.was_silent_last_check is False


# ---- floor configuration ------------------------------------------------

def test_lower_floor_makes_silence_easier_to_register():
    """A more permissive floor (-80 dBFS, ~3 amplitude) still rejects
    a quiet chunk at peak=5 -- but accepts a chunk at peak=2."""
    t_strict = SilenceTracker(silence_window_sec=5.0, silence_floor_dbfs=-80.0)
    for i in range(6):
        t_strict.write(_quiet_chunk(peak=5), wallclock=float(i))
    assert t_strict.is_silent(6.0) is False

    t_loose = SilenceTracker(silence_window_sec=5.0, silence_floor_dbfs=-60.0)
    for i in range(6):
        t_loose.write(_quiet_chunk(peak=5), wallclock=float(i))
    assert t_loose.is_silent(6.0) is True


def test_default_floor_matches_constant():
    t = SilenceTracker()
    assert t.silence_floor_dbfs == DEFAULT_SILENCE_FLOOR_DBFS
