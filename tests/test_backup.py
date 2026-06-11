"""Backup + restore + retention tests for utils.backup (#67).

Exercises BackupManager end-to-end against a synthetic data dir in
tmp_path: writes a snapshot, verifies the zip, restores into a fresh
dir, then checks the prune math.
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from meeting_notetaker.utils.backup import (
    BACKUP_SCHEMA_VERSION,
    BackupError,
    BackupManager,
    _excluded,
    should_run_idle_backup,
)


def _seed_data_dir(root: Path) -> None:
    """Populate ``root`` with the file shapes a real install carries.

    Three sqlite stores (sessions / classification / speakers) plus a
    config.toml, a prompts/ folder, and one session dir with a couple
    of representative files. Excluded paths (models/, automation/,
    *.log, the instance lock, WAL sidecars, pre-restore archives) are
    seeded too so the test pins the exclude rules.
    """
    root.mkdir(parents=True, exist_ok=True)
    # Three sqlite DBs.
    for name in ("sessions.db", "classification.db", "speakers.db"):
        conn = sqlite3.connect(str(root / name))
        try:
            conn.execute("CREATE TABLE meta (k TEXT, v TEXT)")
            conn.execute(
                "INSERT INTO meta VALUES (?, ?)", ("origin", name),
            )
            conn.commit()
        finally:
            conn.close()
    # search.db -- present on installs that have run the indexer.
    conn = sqlite3.connect(str(root / "search.db"))
    try:
        conn.execute("CREATE TABLE meta (k TEXT, v TEXT)")
        conn.commit()
    finally:
        conn.close()

    # Top-level passthroughs.
    (root / "config.toml").write_text(
        "[audio]\nretain_audio_default = false\n", encoding="utf-8",
    )
    (root / "vocabulary.txt").write_text("Alice\nBob\n", encoding="utf-8")
    (root / "calendar_state.json").write_text("{}", encoding="utf-8")
    (root / "audio_session_state.json").write_text("{}", encoding="utf-8")

    # Prompts dir.
    prompts = root / "prompts"
    prompts.mkdir()
    (prompts / "default.md").write_text("# Default prompt\n", encoding="utf-8")

    # One session dir.
    sessions = root / "sessions"
    sess = sessions / "test-session"
    (sess / "audio").mkdir(parents=True)
    (sess / "screenshots").mkdir()
    (sess / "live_notes.md").write_text("# Notes\n- a\n", encoding="utf-8")
    (sess / "notes.md").write_text("# Synthesis\n", encoding="utf-8")
    (sess / "metadata.json").write_text("{}", encoding="utf-8")
    (sess / "audio" / "mic.opus").write_bytes(b"FAKE_OPUS_BYTES")

    # Things that must NOT make it into the snapshot.
    (root / "instance.lock").write_text("pid=123", encoding="utf-8")
    (root / "meeting_notetaker.log").write_text("logs\n", encoding="utf-8")
    (root / "meeting_notetaker-20260101-120000.log").write_text(
        "old log\n", encoding="utf-8",
    )
    (root / "sessions.db-wal").write_bytes(b"WAL")
    (root / "sessions.db-shm").write_bytes(b"SHM")
    (root / "models").mkdir()
    (root / "models" / "small.en.bin").write_bytes(b"FAKE_MODEL")
    (root / "automation").mkdir()
    (root / "automation" / "bridge.json").write_text(
        "{}", encoding="utf-8",
    )
    (root / ".pre-restore.MeetingNotetaker.1700000000").mkdir()
    (root / ".pre-restore.MeetingNotetaker.1700000000" / "stale.txt").write_text(
        "old", encoding="utf-8",
    )


def _mk_manager(tmp_path: Path) -> BackupManager:
    data_dir = tmp_path / "data"
    dest = tmp_path / "backups"
    _seed_data_dir(data_dir)
    return BackupManager(data_dir=data_dir, destination=dest)


# ---- snapshot ---------------------------------------------------------


def test_snapshot_writes_zip_with_expected_layout(tmp_path):
    mgr = _mk_manager(tmp_path)
    result = mgr.snapshot_now()
    assert result.path.exists()
    assert result.size_bytes > 0
    with zipfile.ZipFile(result.path) as zf:
        names = set(zf.namelist())
    assert "manifest.json" in names
    assert "sessions.db" in names
    assert "classification.db" in names
    assert "speakers.db" in names
    assert "search.db" in names
    assert "config.toml" in names
    assert "vocabulary.txt" in names
    assert "prompts/default.md" in names
    assert "sessions/test-session/live_notes.md" in names
    assert "sessions/test-session/audio/mic.opus" in names


def test_snapshot_excludes_logs_lockfile_models_and_wal(tmp_path):
    mgr = _mk_manager(tmp_path)
    result = mgr.snapshot_now()
    with zipfile.ZipFile(result.path) as zf:
        names = set(zf.namelist())
    assert "instance.lock" not in names
    assert "meeting_notetaker.log" not in names
    assert not any(n.startswith("meeting_notetaker-") for n in names)
    assert "sessions.db-wal" not in names
    assert "sessions.db-shm" not in names
    assert not any(n.startswith("models/") for n in names)
    assert not any(n.startswith("automation/") for n in names)
    assert not any(n.startswith(".pre-restore.") for n in names)


def test_snapshot_manifest_records_metadata(tmp_path):
    mgr = _mk_manager(tmp_path)
    result = mgr.snapshot_now()
    with zipfile.ZipFile(result.path) as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    assert manifest["schema_version"] == BACKUP_SCHEMA_VERSION
    assert manifest["session_count"] == 1
    assert "sessions.db" in manifest["db_files"]
    assert manifest["hostname"]  # populated, non-empty
    assert manifest["app_version"]  # populated


def test_snapshot_databases_survive_zip_round_trip(tmp_path):
    """sqlite backup API output must open + return the rows we wrote."""
    mgr = _mk_manager(tmp_path)
    result = mgr.snapshot_now()
    with zipfile.ZipFile(result.path) as zf:
        zf.extractall(tmp_path / "extracted")
    conn = sqlite3.connect(str(tmp_path / "extracted" / "sessions.db"))
    try:
        rows = conn.execute("SELECT k, v FROM meta").fetchall()
    finally:
        conn.close()
    assert rows == [("origin", "sessions.db")]


def test_snapshot_refuses_destination_inside_data_dir(tmp_path):
    data_dir = tmp_path / "data"
    _seed_data_dir(data_dir)
    bad_dest = data_dir / "self-bak"
    mgr = BackupManager(data_dir=data_dir, destination=bad_dest)
    with pytest.raises(BackupError, match="inside the data dir"):
        mgr.snapshot_now()


def test_snapshot_refuses_destination_equal_to_data_dir(tmp_path):
    data_dir = tmp_path / "data"
    _seed_data_dir(data_dir)
    mgr = BackupManager(data_dir=data_dir, destination=data_dir)
    with pytest.raises(BackupError):
        mgr.snapshot_now()


def test_snapshot_cleans_up_partial_on_failure(tmp_path, monkeypatch):
    mgr = _mk_manager(tmp_path)
    # Force the manifest read-back to think the zip is malformed; the
    # snapshot path should clean the .partial up so the destination
    # doesn't accumulate orphans.
    def _boom(self, zip_path):  # noqa: ARG001
        raise BackupError("simulated verify failure")
    monkeypatch.setattr(BackupManager, "_verify_zip", _boom)
    with pytest.raises(BackupError, match="simulated"):
        mgr.snapshot_now()
    assert list((tmp_path / "backups").glob("*.partial")) == []


# ---- restore ---------------------------------------------------------


def test_restore_swaps_data_dir_aside_and_unpacks_zip(tmp_path):
    mgr = _mk_manager(tmp_path)
    result = mgr.snapshot_now()
    # Mutate the data dir so we can prove the restore replaced it.
    (mgr.data_dir / "config.toml").write_text(
        "MUTATED", encoding="utf-8",
    )
    mgr.restore_from(result.path)
    assert (mgr.data_dir / "config.toml").read_text() == (
        "[audio]\nretain_audio_default = false\n"
    )
    # Pre-restore archive should be present.
    siblings = list(tmp_path.glob(".pre-restore.*"))
    assert len(siblings) == 1


def test_restore_refuses_missing_snapshot(tmp_path):
    mgr = _mk_manager(tmp_path)
    with pytest.raises(BackupError, match="not found"):
        mgr.restore_from(tmp_path / "does-not-exist.zip")


def test_restore_refuses_bad_manifest_schema(tmp_path):
    mgr = _mk_manager(tmp_path)
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr(
            "manifest.json",
            json.dumps({"schema_version": 999}),
        )
    with pytest.raises(BackupError, match="newer than this build"):
        mgr.restore_from(bad)


# ---- prune -----------------------------------------------------------


def test_prune_by_count_keeps_newest_n(tmp_path):
    mgr = _mk_manager(tmp_path)
    mgr.set_retention(count=2, days=0)
    # Seed five snapshots with monotonically increasing timestamps.
    for i in range(5):
        stamp = _dt.datetime(
            2026, 1, 1, 12, i, 0, tzinfo=_dt.timezone.utc,
        ).strftime("%Y-%m-%dT%H%M%SZ")
        path = mgr.destination / f"meeting-notetaker-backup-{stamp}.zip"
        mgr.destination.mkdir(parents=True, exist_ok=True)
        path.write_text("stub", encoding="utf-8")
    dropped = mgr.prune()
    assert len(dropped) == 3
    survivors = sorted(mgr.destination.glob("meeting-notetaker-backup-*.zip"))
    assert len(survivors) == 2


def test_prune_by_days_drops_older_than_cutoff(tmp_path):
    fixed_now = _dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
    data_dir = tmp_path / "data"
    _seed_data_dir(data_dir)
    mgr = BackupManager(
        data_dir=data_dir,
        destination=tmp_path / "backups",
        now=lambda: fixed_now,
    )
    mgr.set_retention(count=0, days=10)
    mgr.destination.mkdir(parents=True, exist_ok=True)
    for days_ago in (1, 5, 9, 12, 30):
        ts = fixed_now - _dt.timedelta(days=days_ago)
        stamp = ts.strftime("%Y-%m-%dT%H%M%SZ")
        (mgr.destination / f"meeting-notetaker-backup-{stamp}.zip").write_text(
            "stub", encoding="utf-8",
        )
    dropped = mgr.prune()
    assert len(dropped) == 2  # 12d + 30d old
    survivors = sorted(
        mgr.destination.glob("meeting-notetaker-backup-*.zip")
    )
    assert len(survivors) == 3


def test_prune_keeps_unrelated_files_in_destination(tmp_path):
    mgr = _mk_manager(tmp_path)
    mgr.set_retention(count=0, days=0)
    mgr.destination.mkdir(parents=True, exist_ok=True)
    (mgr.destination / "user-note.txt").write_text(
        "do not touch", encoding="utf-8",
    )
    dropped = mgr.prune()
    assert dropped == []
    assert (mgr.destination / "user-note.txt").exists()


def test_snapshot_then_prune_uses_configured_retention(tmp_path):
    """Snapshot + prune together: a third snapshot should evict the
    oldest when retention_count = 2."""
    mgr = _mk_manager(tmp_path)
    mgr.set_retention(count=2, days=0)
    times = [
        _dt.datetime(2026, 1, 1, h, 0, 0, tzinfo=_dt.timezone.utc)
        for h in (10, 11, 12)
    ]
    idx = {"i": 0}

    def _next_now() -> _dt.datetime:
        ts = times[idx["i"]]
        idx["i"] += 1
        return ts

    mgr._now = _next_now  # noqa: SLF001
    r1 = mgr.snapshot_now()
    r2 = mgr.snapshot_now()
    r3 = mgr.snapshot_now()
    survivors = sorted(p.name for p in mgr.destination.glob("*.zip"))
    assert r1.path.name not in survivors
    assert r2.path.name in survivors
    assert r3.path.name in survivors


# ---- verify ----------------------------------------------------------


def test_verify_detects_corrupt_archive(tmp_path):
    mgr = _mk_manager(tmp_path)
    bad = tmp_path / "broken.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(BackupError):
        mgr.verify(bad)


def test_verify_detects_missing_manifest(tmp_path):
    mgr = _mk_manager(tmp_path)
    no_manifest = tmp_path / "no-manifest.zip"
    with zipfile.ZipFile(no_manifest, "w") as zf:
        zf.writestr("foo.txt", "bar")
    with pytest.raises(BackupError, match="manifest"):
        mgr.verify(no_manifest)


# ---- exclude rules ---------------------------------------------------


def test_exclude_filters_out_logs_and_models(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    keep = root / "sessions" / "abc" / "live_notes.md"
    keep.parent.mkdir(parents=True)
    keep.write_text("notes", encoding="utf-8")
    assert _excluded(keep, root) is False

    for p in (
        root / "models" / "ct2.bin",
        root / "automation" / "bridge.json",
        root / "instance.lock",
        root / "meeting_notetaker.log",
        root / "meeting_notetaker-20260101-010101.log",
        root / "sessions.db-wal",
        root / "sessions.db-shm",
        root / ".pre-restore.X.123" / "stale.txt",
        root / ".restore-tmp.456" / "anything",
        # #102 bug 5: updater download cache must stay out of backups
        # so a 60+ MB installer per release doesn't bloat the archive.
        root / "updates" / "meeting-notetaker-setup-0.7.10.exe",
        root / "updates" / "meeting-notetaker-setup-0.7.11.exe",
    ):
        assert _excluded(p, root) is True, f"should exclude {p}"


# ---- idle scheduler --------------------------------------------------


def test_should_run_idle_backup_false_for_non_idle_schedule():
    now = _dt.datetime(2026, 6, 1, 20, 0, 0)
    assert should_run_idle_backup(
        schedule="manual",
        last_snapshot_at="",
        idle_seconds=999999,
        idle_after_minutes=30,
        idle_after_hour=19,
        now_local=now,
    ) is False
    assert should_run_idle_backup(
        schedule="on_close",
        last_snapshot_at="",
        idle_seconds=999999,
        idle_after_minutes=30,
        idle_after_hour=19,
        now_local=now,
    ) is False


def test_should_run_idle_backup_blocks_before_configured_hour():
    early = _dt.datetime(2026, 6, 1, 18, 59, 0)
    assert should_run_idle_backup(
        schedule="when_idle",
        last_snapshot_at="",
        idle_seconds=999999,
        idle_after_minutes=30,
        idle_after_hour=19,
        now_local=early,
    ) is False


def test_should_run_idle_backup_blocks_under_idle_threshold():
    now = _dt.datetime(2026, 6, 1, 21, 0, 0)
    assert should_run_idle_backup(
        schedule="when_idle",
        last_snapshot_at="",
        idle_seconds=10 * 60,  # 10 min
        idle_after_minutes=30,
        idle_after_hour=19,
        now_local=now,
    ) is False


def test_should_run_idle_backup_blocks_within_24h_of_last():
    now = _dt.datetime(2026, 6, 2, 20, 0, 0)
    last = _dt.datetime(2026, 6, 1, 22, 0, 0).isoformat()  # 22h ago
    assert should_run_idle_backup(
        schedule="when_idle",
        last_snapshot_at=last,
        idle_seconds=999999,
        idle_after_minutes=30,
        idle_after_hour=19,
        now_local=now,
    ) is False


def test_should_run_idle_backup_fires_when_all_conditions_met():
    now = _dt.datetime(2026, 6, 2, 22, 0, 0)
    last = _dt.datetime(2026, 6, 1, 19, 0, 0).isoformat()  # 27h ago
    assert should_run_idle_backup(
        schedule="when_idle",
        last_snapshot_at=last,
        idle_seconds=45 * 60,
        idle_after_minutes=30,
        idle_after_hour=19,
        now_local=now,
    ) is True


def test_should_run_idle_backup_fires_on_first_run():
    now = _dt.datetime(2026, 6, 2, 22, 0, 0)
    assert should_run_idle_backup(
        schedule="when_idle",
        last_snapshot_at="",
        idle_seconds=45 * 60,
        idle_after_minutes=30,
        idle_after_hour=19,
        now_local=now,
    ) is True
