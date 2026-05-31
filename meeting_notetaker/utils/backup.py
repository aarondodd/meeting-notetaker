"""Local-machine SPOF mitigation: snapshot + restore + retention (#67).

The user picks a destination folder (typically OneDrive / external drive /
NAS); the app writes timestamped zip files containing a consistent snapshot
of every sqlite store plus the on-disk session content. Restoration unzips
into a sibling tmp dir + atomically swaps the original data dir aside.

Why zip-per-snapshot, not incremental:
- Closed-session audio + screenshots don't change after the meeting ends,
  so per-file dedup gains are small relative to the implementation cost.
- A retention policy keeps the snapshot count bounded; the typical user
  with 1-2 weeks of retention sits in the 50-100 GB range.

Why a separate sqlite backup pass:
- sqlite WAL + shared-memory files (`*-wal`, `*-shm`) are not safe to copy
  while the live connection still holds them open. The sqlite backup API
  (`Connection.backup()`) produces a transactionally consistent snapshot
  without disturbing the live connection.

Exclusions (never end up in the zip):
- `instance.lock` -- transient, recreated on next launch
- `meeting_notetaker.log` + rotated `meeting_notetaker-*.log` -- ops-only
- `models/` -- faster-whisper checkpoints, multi-GB, re-downloadable
- `automation/` -- Chrome extension assets, re-extracted on launch
- pre-restore archives (``.pre-restore.*`` dirs) and any backup folder
  the user happened to point inside the data dir
- WAL / SHM sidecar files (`*-wal`, `*-shm`) -- the backup API writes
  a self-contained db file
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import platform
import re
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional


log = logging.getLogger(__name__)

# Snapshot filename pattern -- ISO-ish timestamp in basic format so it
# sorts lexically + survives Windows filename rules. The trailing Z is
# UTC; the timezone offset is encoded into the filename to keep them
# unambiguous across DST transitions.
_SNAPSHOT_PREFIX = "meeting-notetaker-backup-"
_SNAPSHOT_SUFFIX = ".zip"
_SNAPSHOT_TS_FMT = "%Y-%m-%dT%H%M%SZ"
_SNAPSHOT_RE = re.compile(
    rf"^{re.escape(_SNAPSHOT_PREFIX)}(\d{{4}}-\d{{2}}-\d{{2}}T\d{{6}}Z){re.escape(_SNAPSHOT_SUFFIX)}$"
)

# DB files that live in the data dir root. The backup API copies each
# into the zip; we explicitly list these rather than glob *.db so a
# stray .db file the user dropped in doesn't break the snapshot.
_DB_FILES = ("sessions.db", "classification.db", "speakers.db", "search.db")

# Top-level paths inside app_data_dir that get straight-copied into
# the zip (relative paths preserved). Anything not in this list and
# not a known db is skipped silently.
_TOP_LEVEL_FILES = (
    "config.toml",
    "vocabulary.txt",
    "calendar_state.json",
    "audio_session_state.json",
)
_TOP_LEVEL_DIRS = ("prompts", "sessions")

# Locking file written into the destination folder while a snapshot
# is being assembled. Cleared on success; presence at backup start
# means a previous run crashed mid-write and the partial zip should
# be cleaned up.
_LOCK_FILENAME = ".backup-in-progress"

# Bumped on any zip-layout change so a restore can refuse incompatible
# snapshots up front instead of half-restoring a v2 archive into a v1
# data dir.
BACKUP_SCHEMA_VERSION = 1


class BackupError(RuntimeError):
    """Raised on any failure of the backup / restore / prune pipeline.

    Always include enough context that a Teams notice or log entry is
    actionable without re-reading the code (path, errno-ish reason).
    """


@dataclass
class SnapshotInfo:
    """Summary of one snapshot zip in the destination folder."""
    path: Path
    timestamp: _dt.datetime  # UTC, parsed from filename
    size_bytes: int

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class BackupResult:
    """Returned by ``snapshot_now`` for callers that want to report up."""
    path: Path
    size_bytes: int
    pruned: list[Path]
    duration_seconds: float


class BackupManager:
    """Snapshot + restore + retention for the per-user data dir.

    Constructed with the live data dir and the user-chosen destination.
    The class is intentionally stateless across calls beyond a single
    ``threading.Lock`` so a UI thread asking "is a backup running?"
    can read ``is_running`` without coordination.

    Callers from a Qt thread should run ``snapshot_now`` on a worker
    (the sqlite backup API + shutil.copytree are blocking). ``verify``,
    ``list_snapshots``, ``prune`` are cheap and fine on the GUI thread.
    """

    def __init__(
        self,
        *,
        data_dir: Path,
        destination: Path,
        now: Optional[Callable[[], _dt.datetime]] = None,
    ) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.destination = Path(destination).resolve()
        # Injectable so tests can pin timestamps without freezegun.
        self._now: Callable[[], _dt.datetime] = now or (
            lambda: _dt.datetime.now(_dt.timezone.utc)
        )
        self._lock = threading.Lock()
        self._running = False

    # ---- introspection ----------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running

    def list_snapshots(self) -> list[SnapshotInfo]:
        """Existing snapshots in the destination, newest first.

        Files that don't match the snapshot naming pattern are ignored
        so a user dropping unrelated files into the backup folder
        doesn't confuse the retention pass.
        """
        if not self.destination.is_dir():
            return []
        out: list[SnapshotInfo] = []
        for entry in self.destination.iterdir():
            if not entry.is_file():
                continue
            ts = _parse_snapshot_timestamp(entry.name)
            if ts is None:
                continue
            try:
                size = entry.stat().st_size
            except OSError:
                continue
            out.append(SnapshotInfo(path=entry, timestamp=ts, size_bytes=size))
        out.sort(key=lambda s: s.timestamp, reverse=True)
        return out

    # ---- main entry points ------------------------------------------

    def snapshot_now(self) -> BackupResult:
        """Write a timestamped zip of the data dir into the destination.

        Raises BackupError on any failure. On success, the destination
        contains the new zip and the retention prune has already run.
        """
        self._validate_paths()
        start = time.monotonic()
        with self._lock:
            if self._running:
                raise BackupError(
                    "another backup is already running (refusing to "
                    "start a concurrent snapshot)"
                )
            self._running = True
        try:
            self.destination.mkdir(parents=True, exist_ok=True)
            self._cleanup_stale_lock()
            stamp_dt = self._now()
            stamp = stamp_dt.strftime(_SNAPSHOT_TS_FMT)
            target = self.destination / f"{_SNAPSHOT_PREFIX}{stamp}{_SNAPSHOT_SUFFIX}"
            tmp_target = target.with_suffix(".zip.partial")
            lock = self.destination / _LOCK_FILENAME
            try:
                lock.write_text(
                    json.dumps(
                        {
                            "started_at": stamp_dt.isoformat(),
                            "data_dir": str(self.data_dir),
                            "target": str(target),
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            except OSError as exc:
                raise BackupError(
                    f"cannot write backup lockfile under {self.destination}: {exc}"
                ) from exc

            try:
                self._write_snapshot(tmp_target, stamp_dt)
                # Verify before promoting -- a corrupt zip is worse
                # than no zip because retention may delete the prior
                # good snapshot.
                self._verify_zip(tmp_target)
                # Atomic-ish promote. os.replace is atomic on the same
                # filesystem; if dest fs is different (e.g. user picks
                # a USB drive), we still get the last-writer-wins
                # behavior, which is fine since the partial zip was
                # held under a unique name.
                os.replace(tmp_target, target)
            except Exception:
                # Best-effort cleanup of the partial zip on any failure
                # so the destination doesn't accumulate .partial files.
                if tmp_target.exists():
                    try:
                        tmp_target.unlink()
                    except OSError:
                        pass
                raise
            finally:
                try:
                    lock.unlink()
                except OSError:
                    pass

            pruned = self.prune()
            size = target.stat().st_size
            duration = time.monotonic() - start
            log.info(
                "Backup snapshot written: %s (%.1f MB, %d pruned, %.1fs)",
                target, size / (1024 * 1024), len(pruned), duration,
            )
            return BackupResult(
                path=target,
                size_bytes=size,
                pruned=pruned,
                duration_seconds=duration,
            )
        finally:
            with self._lock:
                self._running = False

    def restore_from(self, zip_path: Path) -> None:
        """Replace the data dir with the contents of ``zip_path``.

        Caller is responsible for shutting down the app first -- live
        sqlite connections to the data dir's databases will lose their
        handles when the dir is renamed aside. The old data dir is
        preserved as ``.pre-restore.<stamp>/`` for one-shot rollback.

        Raises BackupError on a malformed zip or filesystem failure.
        """
        zip_path = Path(zip_path).resolve()
        if not zip_path.is_file():
            raise BackupError(f"snapshot not found: {zip_path}")
        if self.data_dir == self.destination or \
                _is_subpath(self.destination, self.data_dir):
            raise BackupError(
                "backup destination is inside the data dir; refusing "
                "to restore because the operation would race itself"
            )
        with zipfile.ZipFile(zip_path) as zf:
            self._verify_manifest(zf)
            extract_root = self.data_dir.parent / f".restore-tmp.{int(time.time())}"
            extract_root.mkdir(parents=True, exist_ok=False)
            try:
                zf.extractall(extract_root)
            except (OSError, zipfile.BadZipFile) as exc:
                shutil.rmtree(extract_root, ignore_errors=True)
                raise BackupError(
                    f"snapshot extraction failed: {exc}"
                ) from exc
        # Swap. Move the live data dir aside first so a failure during
        # the second rename surfaces with the original dir already
        # quarantined; the user can roll back manually if needed.
        pre_restore = self.data_dir.parent / (
            f".pre-restore.{self.data_dir.name}.{int(time.time())}"
        )
        try:
            os.replace(self.data_dir, pre_restore)
        except OSError as exc:
            shutil.rmtree(extract_root, ignore_errors=True)
            raise BackupError(
                f"cannot move existing data dir aside: {exc}"
            ) from exc
        try:
            os.replace(extract_root, self.data_dir)
        except OSError as exc:
            # Try to roll the original back into place before bailing.
            try:
                os.replace(pre_restore, self.data_dir)
            except OSError:
                pass
            shutil.rmtree(extract_root, ignore_errors=True)
            raise BackupError(
                f"cannot install restored data dir: {exc}"
            ) from exc
        log.info(
            "Restore complete from %s; previous data dir preserved at %s",
            zip_path, pre_restore,
        )

    def prune(self) -> list[Path]:
        """Delete snapshots beyond retention.

        Returns the list of paths that were removed. Silent per Aaron's
        decision (#67 open-question 4) -- the feature is wasted if a
        daily click is required.

        Retention math:
        - ``retention_count`` keeps at most N newest; older drop.
        - ``retention_days`` drops any snapshot older than D days.
        - Both apply -- the intersection of "keep" survives.
        - Either set to 0 disables that gate.
        """
        keep_count = self._retention_count
        keep_days = self._retention_days
        snapshots = self.list_snapshots()
        if not snapshots:
            return []
        # Newest -> oldest. Apply count first; mark all-up-to-N as
        # keep, the rest as drop.
        to_drop: list[Path] = []
        cutoff: Optional[_dt.datetime] = None
        if keep_days > 0:
            cutoff = self._now() - _dt.timedelta(days=keep_days)
        for idx, snap in enumerate(snapshots):
            drop = False
            if keep_count > 0 and idx >= keep_count:
                drop = True
            if cutoff is not None and snap.timestamp < cutoff:
                drop = True
            if drop:
                to_drop.append(snap.path)
        for path in to_drop:
            try:
                path.unlink()
            except OSError as exc:
                log.warning("Could not prune backup %s: %s", path, exc)
        return to_drop

    def verify(self, zip_path: Path) -> None:
        """Confirm ``zip_path`` opens cleanly and carries the expected
        manifest + at least one db file. Raises BackupError otherwise.

        Used by both ``snapshot_now`` (post-write) and the Restore
        dialog so a malformed snapshot fails loudly up front."""
        zip_path = Path(zip_path)
        if not zip_path.is_file():
            raise BackupError(f"snapshot not found: {zip_path}")
        self._verify_zip(zip_path)

    # ---- knob accessors so callers can plug in BackupConfig ---------

    @property
    def _retention_count(self) -> int:
        return getattr(self, "_override_count", 7)

    @property
    def _retention_days(self) -> int:
        return getattr(self, "_override_days", 30)

    def set_retention(self, count: int, days: int) -> None:
        """Override the per-instance retention values. Tests + the
        Settings dialog use this to drive the prune pass."""
        self._override_count = max(0, int(count))
        self._override_days = max(0, int(days))

    # ---- internals --------------------------------------------------

    def _validate_paths(self) -> None:
        if not self.destination:
            raise BackupError("backup destination not configured")
        if self.destination == self.data_dir:
            raise BackupError(
                "backup destination cannot be the data dir itself"
            )
        if _is_subpath(self.destination, self.data_dir):
            raise BackupError(
                "backup destination cannot be inside the data dir"
            )
        if not self.data_dir.is_dir():
            raise BackupError(f"data dir not found: {self.data_dir}")

    def _cleanup_stale_lock(self) -> None:
        """Remove any leftover .backup-in-progress and the matching
        .partial file from a crash on a prior run. Idempotent."""
        lock = self.destination / _LOCK_FILENAME
        if not lock.is_file():
            return
        try:
            lock.unlink()
        except OSError:
            pass
        for entry in self.destination.glob(f"{_SNAPSHOT_PREFIX}*.partial"):
            try:
                entry.unlink()
            except OSError:
                pass

    def _write_snapshot(self, target: Path, stamp_dt: _dt.datetime) -> None:
        """Assemble the snapshot zip at ``target``.

        Layout (relative paths inside the zip):
        - ``manifest.json``        -- schema version, source host,
                                       session count, created_at
        - ``<db>.db``              -- one per file in ``_DB_FILES``,
                                       via sqlite backup API
        - ``config.toml`` etc.    -- top-level file passthrough
        - ``prompts/...``         -- user-customized templates
        - ``sessions/<id>/...``   -- session content (audio +
                                       transcript + notes + images +
                                       attachments + sidecars)
        """
        # Use ZIP_DEFLATED at the zipfile default level 6 -- a higher
        # level squeezes maybe 5% off the size for ~3x the wall time,
        # and audio + image content is already compressed so the gains
        # diminish fast. zlib's standard compresslevel keyword is
        # supported since Python 3.7.
        with tempfile.TemporaryDirectory(prefix="mnt-backup-") as td:
            tmpdir = Path(td)
            db_snapshots = self._snapshot_databases(tmpdir)
            session_count = self._count_sessions()
            manifest = {
                "schema_version": BACKUP_SCHEMA_VERSION,
                "app_version": _read_app_version(),
                "created_at": stamp_dt.isoformat(),
                "hostname": socket.gethostname(),
                "platform": platform.system(),
                "session_count": session_count,
                "db_files": [p.name for p in db_snapshots],
            }
            with zipfile.ZipFile(
                target, "w", compression=zipfile.ZIP_DEFLATED,
            ) as zf:
                zf.writestr(
                    "manifest.json",
                    json.dumps(manifest, indent=2, sort_keys=True),
                )
                # DB snapshots written by the backup API.
                for snap in db_snapshots:
                    zf.write(snap, arcname=snap.name)
                # Top-level passthrough files (config + small state).
                for name in _TOP_LEVEL_FILES:
                    src = self.data_dir / name
                    if src.is_file():
                        zf.write(src, arcname=name)
                # Top-level dirs (prompts + sessions) walked manually
                # so we can apply exclusions.
                for name in _TOP_LEVEL_DIRS:
                    src = self.data_dir / name
                    if not src.is_dir():
                        continue
                    for path in _walk_files(src):
                        if _excluded(path, self.data_dir):
                            continue
                        arcname = path.relative_to(self.data_dir).as_posix()
                        try:
                            zf.write(path, arcname=arcname)
                        except OSError as exc:
                            # Skip files we can't read (Windows locked
                            # handles, stale symlinks); record + move
                            # on rather than failing the whole snapshot.
                            log.warning(
                                "Skipping unreadable file during "
                                "backup: %s (%s)", path, exc,
                            )

    def _snapshot_databases(self, dest_dir: Path) -> list[Path]:
        """Copy each live sqlite DB into ``dest_dir`` via the backup API.

        Returns the list of written paths so the caller can decide what
        to embed. Files that don't exist on disk are skipped silently
        (e.g. search.db on a fresh install before the index is built).
        """
        out: list[Path] = []
        for name in _DB_FILES:
            src = self.data_dir / name
            if not src.is_file():
                continue
            dst = dest_dir / name
            try:
                _sqlite_backup(src, dst)
            except sqlite3.Error as exc:
                raise BackupError(
                    f"sqlite backup of {name} failed: {exc}"
                ) from exc
            out.append(dst)
        return out

    def _count_sessions(self) -> int:
        sessions = self.data_dir / "sessions"
        if not sessions.is_dir():
            return 0
        try:
            return sum(1 for p in sessions.iterdir() if p.is_dir())
        except OSError:
            return 0

    def _verify_zip(self, zip_path: Path) -> None:
        """Open the zip, walk its entries, check the manifest is present
        + valid + every promised DB file is also present.

        Raises BackupError on any inconsistency.
        """
        try:
            with zipfile.ZipFile(zip_path) as zf:
                names = set(zf.namelist())
                if "manifest.json" not in names:
                    raise BackupError(
                        f"snapshot {zip_path.name} is missing manifest.json"
                    )
                with zf.open("manifest.json") as fh:
                    try:
                        manifest = json.loads(fh.read().decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        raise BackupError(
                            f"snapshot {zip_path.name} has unreadable "
                            f"manifest: {exc}"
                        ) from exc
                schema = manifest.get("schema_version")
                if not isinstance(schema, int) or schema > BACKUP_SCHEMA_VERSION:
                    raise BackupError(
                        f"snapshot {zip_path.name} schema_version "
                        f"{schema!r} is newer than this build "
                        f"(supports up to {BACKUP_SCHEMA_VERSION})"
                    )
                for db in manifest.get("db_files", []):
                    if db not in names:
                        raise BackupError(
                            f"snapshot {zip_path.name} manifest claims "
                            f"{db} but the zip is missing that entry"
                        )
                # CRC scan -- catches torn writes, partial uploads, etc.
                bad = zf.testzip()
                if bad:
                    raise BackupError(
                        f"snapshot {zip_path.name} has a corrupt "
                        f"entry: {bad}"
                    )
        except zipfile.BadZipFile as exc:
            raise BackupError(
                f"snapshot {zip_path.name} is not a valid zip: {exc}"
            ) from exc

    def _verify_manifest(self, zf: zipfile.ZipFile) -> None:
        """Restore-time manifest check (separate from snapshot-time
        verify so callers in the restore path don't double-open the
        zip)."""
        names = set(zf.namelist())
        if "manifest.json" not in names:
            raise BackupError("snapshot is missing manifest.json")
        with zf.open("manifest.json") as fh:
            try:
                manifest = json.loads(fh.read().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise BackupError(
                    f"snapshot manifest is unreadable: {exc}"
                ) from exc
        schema = manifest.get("schema_version")
        if not isinstance(schema, int) or schema > BACKUP_SCHEMA_VERSION:
            raise BackupError(
                f"snapshot schema_version {schema!r} is newer than this "
                f"build (supports up to {BACKUP_SCHEMA_VERSION})"
            )


# -----------------------------------------------------------------------
# Schedule helpers (pure functions so tests can drive them without a
# running Qt event loop).


def should_run_idle_backup(
    *,
    schedule: str,
    last_snapshot_at: str,
    idle_seconds: float,
    idle_after_minutes: int,
    idle_after_hour: int,
    now_local: _dt.datetime,
) -> bool:
    """True when the idle-mode trigger should fire a backup now.

    Inputs come from BackupConfig + an idle-time tracker. Returns False
    for non-idle schedules so the caller can use this as a single gate.

    Conditions, all must hold:
      - schedule == 'when_idle'
      - now_local.hour >= idle_after_hour
      - idle_seconds >= idle_after_minutes * 60
      - last_snapshot_at empty OR more than 24h ago (the daily cap)
    """
    if schedule != "when_idle":
        return False
    if now_local.hour < idle_after_hour:
        return False
    if idle_seconds < idle_after_minutes * 60:
        return False
    if last_snapshot_at:
        try:
            last = _dt.datetime.fromisoformat(last_snapshot_at)
        except ValueError:
            last = None
        if last is not None:
            # Strip tz for the diff if needed; both sides should be
            # local-naive but we tolerate either.
            if last.tzinfo and not now_local.tzinfo:
                last = last.replace(tzinfo=None)
            elif now_local.tzinfo and not last.tzinfo:
                last = last.replace(tzinfo=now_local.tzinfo)
            if now_local - last < _dt.timedelta(hours=24):
                return False
    return True


# -----------------------------------------------------------------------
# Module-private helpers (no leading underscore on the module API so the
# Settings + Tools wiring can import them directly).


def _sqlite_backup(src: Path, dst: Path) -> None:
    """Copy ``src`` to ``dst`` via the sqlite backup API.

    Uses a fresh connection on each side so the running app's
    connections are untouched. The DB is opened read-only via the
    standard `Connection.backup()` driver; the dst connection is a
    fresh empty file.
    """
    # PRAGMA fullfsync etc are out of scope -- backup() is the
    # canonical "snapshot to a different file" tool for sqlite.
    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _walk_files(root: Path) -> Iterable[Path]:
    """Yield every file under ``root`` recursively, skipping symlinks
    that resolve outside the tree."""
    for dirpath, dirnames, filenames in os.walk(root):
        # Stable order so test diffs are deterministic.
        dirnames.sort()
        for name in sorted(filenames):
            yield Path(dirpath) / name


# Path components / suffixes that never make it into a snapshot. Match
# is done on POSIX-form relative paths so Windows + Linux behave the
# same in tests.
_EXCLUDE_TOP_LEVEL = {
    "models", "automation", "instance.lock",
}
_EXCLUDE_SUFFIXES = ("-wal", "-shm")
_EXCLUDE_NAME_PREFIXES = (
    "meeting_notetaker.log",
    "meeting_notetaker-",  # rotated logs (meeting_notetaker-YYYYMMDD-HHMMSS.log)
)
# Pre-restore archive prefix so a re-snapshot after a restore doesn't
# sweep the old data dir copy back into the new one.
_EXCLUDE_DIR_PREFIXES = (".pre-restore.", ".restore-tmp.")


def _excluded(path: Path, root: Path) -> bool:
    """True if ``path`` (under ``root``) should be skipped in a snapshot.

    Decisions:
    - any path under a top-level dir in _EXCLUDE_TOP_LEVEL
    - any sqlite WAL / SHM sidecar
    - any rotated log file (caller already handles the live log)
    - any path inside a pre-restore archive
    """
    try:
        rel = path.relative_to(root)
    except ValueError:
        # Path isn't under root -- shouldn't happen via _walk_files,
        # but if it did, skip rather than crash.
        return True
    parts = rel.parts
    if not parts:
        return True
    if parts[0] in _EXCLUDE_TOP_LEVEL:
        return True
    if any(p.startswith(_EXCLUDE_DIR_PREFIXES) for p in parts):
        return True
    last = parts[-1]
    if last.endswith(_EXCLUDE_SUFFIXES):
        return True
    if any(last.startswith(prefix) for prefix in _EXCLUDE_NAME_PREFIXES):
        return True
    return False


def _is_subpath(child: Path, parent: Path) -> bool:
    """True when ``child`` is a path under ``parent`` (or equal)."""
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _parse_snapshot_timestamp(name: str) -> Optional[_dt.datetime]:
    m = _SNAPSHOT_RE.match(name)
    if not m:
        return None
    try:
        return _dt.datetime.strptime(
            m.group(1), _SNAPSHOT_TS_FMT,
        ).replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return None


def _read_app_version() -> str:
    """Best-effort version string for the manifest. Falls back to
    'unknown' when the version module isn't importable (e.g. when the
    backup module is reused in a stripped-down context)."""
    try:
        from ..version import __version__  # noqa: PLC0415
        return str(__version__)
    except Exception:
        return "unknown"
