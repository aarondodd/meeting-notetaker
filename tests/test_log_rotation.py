"""rotate_log_on_launch: archives the current log, prunes old ones."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from meeting_notetaker.utils.paths import (
    LOG_HISTORY_KEEP,
    log_path,
    rotate_log_on_launch,
)


def test_first_launch_no_rotation_needed(isolated_data_dir):
    """Fresh install: no existing log file -> rotate_log returns None
    and creates nothing."""
    assert not log_path().exists()
    result = rotate_log_on_launch()
    assert result is None
    # No historical files materialized.
    assert list(log_path().parent.glob("meeting_notetaker-*.log")) == []


def test_existing_log_moved_aside_with_timestamp(isolated_data_dir):
    """If meeting_notetaker.log exists, it gets renamed to
    meeting_notetaker-YYYYMMDD-HHMMSS.log so the new launch can
    open a fresh file."""
    log_path().write_text("prior run content\n", encoding="utf-8")
    fixed_now = dt.datetime(2026, 5, 22, 14, 30, 45)
    archived = rotate_log_on_launch(now=fixed_now)
    assert archived is not None
    assert archived.name == "meeting_notetaker-20260522-143045.log"
    assert archived.read_text(encoding="utf-8") == "prior run content\n"
    # The live log is gone -- basicConfig will create a fresh one.
    assert not log_path().exists()


def test_empty_log_not_archived(isolated_data_dir):
    """A zero-byte log isn't worth keeping; rotate skips it."""
    log_path().touch()
    assert log_path().exists()
    archived = rotate_log_on_launch()
    assert archived is None
    # Empty live log is left in place (basicConfig will append).
    assert log_path().exists()


def test_history_pruned_to_keep_limit(isolated_data_dir):
    """Once we have more than LOG_HISTORY_KEEP archives, the oldest
    are deleted on each subsequent rotate."""
    # Plant LOG_HISTORY_KEEP + 3 historical files with distinct mtimes.
    base_dir = log_path().parent
    archives = []
    for i in range(LOG_HISTORY_KEEP + 3):
        p = base_dir / f"meeting_notetaker-20260522-{i:02d}0000.log"
        p.write_text(f"archive {i}")
        # Set explicit mtime so the prune order is deterministic.
        import os
        ts = 1716000000 + i * 100  # increasing
        os.utime(p, (ts, ts))
        archives.append(p)

    # Create a current log so rotate_log adds another archive.
    log_path().write_text("current run", encoding="utf-8")
    rotate_log_on_launch(now=dt.datetime(2026, 5, 22, 23, 0, 0))

    remaining = sorted(
        base_dir.glob("meeting_notetaker-*.log"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    assert len(remaining) == LOG_HISTORY_KEEP, (
        f"expected {LOG_HISTORY_KEEP} archives after rotate, got {len(remaining)}: "
        f"{[p.name for p in remaining]}"
    )
    # The two oldest archives (indices 0 and 1) should be gone.
    assert not (base_dir / "meeting_notetaker-20260522-000000.log").exists()
    assert not (base_dir / "meeting_notetaker-20260522-010000.log").exists()
    # The newly-rotated one is in the remaining set.
    assert any("23" in p.name for p in remaining)


def test_collision_in_same_second_appends_counter(isolated_data_dir):
    """Two rotates in the same second shouldn't clobber each other --
    the second one gets a -N suffix."""
    fixed_now = dt.datetime(2026, 5, 22, 14, 30, 45)
    log_path().write_text("run 1", encoding="utf-8")
    rotate_log_on_launch(now=fixed_now)
    log_path().write_text("run 2", encoding="utf-8")
    rotate_log_on_launch(now=fixed_now)
    base_dir = log_path().parent
    files = sorted(base_dir.glob("meeting_notetaker-*.log"))
    assert len(files) == 2
    assert files[0].name == "meeting_notetaker-20260522-143045-1.log"
    assert files[1].name == "meeting_notetaker-20260522-143045.log"
