"""single_instance: a stale PID must not keep a user out of the app.

The old check only asked "is there a process with this PID?", which the
OS answers True for any recycled PID. The replacement reads the running
image path and compares it against the one stored at acquire time, so a
Meeting Notetaker lockfile stays authoritative only while a Meeting
Notetaker is actually running.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from meeting_notetaker.utils import single_instance


def _write_lock(path, pid, image=None):
    body = f"{pid}\n" if image is None else f"{pid}\n{image}\n"
    path.write_text(body, encoding="utf-8")


def _spawn_dummy():
    """Live process whose image is not meeting-notetaker's.

    `sleep` is used because it is on PATH on Linux CI and macOS, and it
    is the simplest "stays alive until killed" shape.
    """
    proc = subprocess.Popen(["sleep", "30"])
    # Give the kernel a tick to publish /proc/<pid>/exe.
    for _ in range(50):
        if (f"/proc/{proc.pid}/exe"):
            break
        time.sleep(0.01)
    return proc


# ---- fresh acquire -----------------------------------------------------

def test_fresh_acquire_writes_pid_and_image(tmp_path):
    lock = tmp_path / "instance.lock"
    assert single_instance.acquire(lock) is True
    pid, image = single_instance._read_lockfile(lock)
    assert pid == os.getpid()
    assert image == single_instance._current_image()


def test_acquire_is_idempotent_for_same_process(tmp_path):
    lock = tmp_path / "instance.lock"
    assert single_instance.acquire(lock) is True
    assert single_instance.acquire(lock) is True


# ---- stale lockfiles ---------------------------------------------------

def test_dead_pid_is_treated_as_stale(tmp_path):
    lock = tmp_path / "instance.lock"
    # A PID the kernel will never allocate: 2^22 is above
    # /proc/sys/kernel/pid_max on every Linux kernel in common use.
    _write_lock(lock, 4_194_300, single_instance._current_image())
    assert single_instance.acquire(lock) is True


def test_recycled_pid_running_different_image_is_treated_as_stale(tmp_path):
    """The core bug: a live PID whose image is not ours used to win."""
    if sys.platform.startswith("win"):
        pytest.skip("Posix helper; Windows path covered by image-mismatch test below.")
    proc = _spawn_dummy()
    try:
        lock = tmp_path / "instance.lock"
        # Lockfile was written by a prior Meeting Notetaker run; the OS
        # later handed its PID to `sleep`, which runs a different image.
        _write_lock(lock, proc.pid, "/opt/meeting-notetaker/meeting-notetaker.exe")
        assert single_instance.acquire(lock) is True
        # The new acquire must have overwritten the lockfile with our
        # own PID and image.
        new_pid, new_image = single_instance._read_lockfile(lock)
        assert new_pid == os.getpid()
        assert new_image == single_instance._current_image()
    finally:
        proc.kill()
        proc.wait()


def test_live_matching_image_is_a_live_instance(tmp_path):
    """A real Meeting Notetaker holding the lock keeps the next launch out.

    Spawning the same Python interpreter the tests run under gives a
    live PID whose image matches `_current_image()` on both Linux and
    Windows CI, so the live-match branch is actually exercised.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        lock = tmp_path / "instance.lock"
        # Wait for /proc/<pid>/exe to publish (Linux) and for
        # QueryFullProcessImageNameW to work (Windows). On Linux the
        # symlink exists immediately after fork; the loop is a cheap
        # defence against a race on busy CI.
        for _ in range(100):
            if single_instance._image_of_pid(proc.pid):
                break
            time.sleep(0.01)
        _write_lock(lock, proc.pid, single_instance._current_image())
        assert single_instance.acquire(lock) is False
    finally:
        proc.kill()
        proc.wait()


def test_legacy_pid_only_lockfile_still_blocks_live_match(tmp_path):
    """Backwards compat: a lockfile from an older build (PID only)
    falls back to the PID-alive check. Live PID + no recorded image
    cannot be discriminated, so the old behaviour stands."""
    proc = subprocess.Popen(["sleep", "30"])
    try:
        lock = tmp_path / "instance.lock"
        _write_lock(lock, proc.pid)  # legacy single-line format
        assert single_instance.acquire(lock) is False
    finally:
        proc.kill()
        proc.wait()


# ---- force -------------------------------------------------------------

def test_force_overrides_a_live_matching_lock(tmp_path):
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        for _ in range(100):
            if single_instance._image_of_pid(proc.pid):
                break
            time.sleep(0.01)
        lock = tmp_path / "instance.lock"
        _write_lock(lock, proc.pid, single_instance._current_image())
        assert single_instance.acquire(lock) is False
        assert single_instance.acquire(lock, force=True) is True
        pid, image = single_instance._read_lockfile(lock)
        assert pid == os.getpid()
        assert image == single_instance._current_image()
    finally:
        proc.kill()
        proc.wait()


# ---- malformed input ---------------------------------------------------

def test_garbage_lockfile_is_overwritten(tmp_path):
    lock = tmp_path / "instance.lock"
    lock.write_text("not a pid\n", encoding="utf-8")
    assert single_instance.acquire(lock) is True


def test_empty_lockfile_is_overwritten(tmp_path):
    lock = tmp_path / "instance.lock"
    lock.write_text("", encoding="utf-8")
    assert single_instance.acquire(lock) is True


# ---- release -----------------------------------------------------------

def test_release_removes_our_own_lockfile(tmp_path):
    lock = tmp_path / "instance.lock"
    assert single_instance.acquire(lock) is True
    single_instance.release(lock)
    assert not lock.exists()


def test_release_leaves_someone_elses_lockfile_alone(tmp_path):
    """release only unlinks when the stored PID matches our own."""
    lock = tmp_path / "instance.lock"
    # Any PID other than ours works: release reads the first line and
    # bails on the mismatch without ever touching the alive check.
    _write_lock(lock, os.getpid() + 1, single_instance._current_image())
    single_instance.release(lock)
    assert lock.exists()


# ---- helpers -----------------------------------------------------------

def test_normalise_image_lowercases_on_windows(monkeypatch, tmp_path):
    target = tmp_path / "MeetingNotetaker.exe"
    target.write_text("x")
    monkeypatch.setattr(sys, "platform", "win32")
    assert single_instance._normalise_image(str(target)) == str(target).lower()


def test_normalise_image_preserves_case_on_posix(monkeypatch, tmp_path):
    target = tmp_path / "MeetingNotetaker"
    target.write_text("x")
    monkeypatch.setattr(sys, "platform", "linux")
    assert single_instance._normalise_image(str(target)) == str(target)
