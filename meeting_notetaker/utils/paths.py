"""Path resolution for app state directories.

On Windows:      %APPDATA%/MeetingNotetaker/
On macOS:        ~/Library/Application Support/MeetingNotetaker/
On Linux/other:  $XDG_CONFIG_HOME/MeetingNotetaker/ (default ~/.config/MeetingNotetaker/)

Set MEETING_NOTETAKER_DATA_DIR to override for tests or portable installs.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


APP_NAME = "MeetingNotetaker"


def app_data_dir() -> Path:
    """Per-user app-state directory. Created on first access."""
    override = os.environ.get("MEETING_NOTETAKER_DATA_DIR")
    if override:
        path = Path(override)
    elif sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        path = Path(base) / APP_NAME
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def sessions_dir() -> Path:
    path = app_data_dir() / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def session_dir(session_id: str) -> Path:
    path = sessions_dir() / session_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def session_audio_dir(session_id: str) -> Path:
    path = session_dir(session_id) / "audio"
    path.mkdir(parents=True, exist_ok=True)
    return path


# Audio file basenames the recorder writes plus the suffixes a retained
# session may end up with after re-encoding. Order within each tuple
# is search order: WAV first (still present mid-retention or for the
# "wav" retain_format), then compressed.
_AUDIO_BASENAMES = ("mic", "sys")
_AUDIO_SUFFIXES = (".wav", ".opus", ".flac")


def session_audio_files(session_id: str) -> list[Path]:
    """Return every audio file on disk for this session, mic+sys.

    Each session can have a mic and a system-loopback recording (one,
    both, or neither). Each may be in its original .wav form or
    re-encoded to .opus / .flac, depending on retain_format. We search
    the audio dir and return whichever files actually exist, in stable
    order (mic before sys).

    Returns an empty list when no audio is retained -- safe to call
    on any session.
    """
    audio_dir = session_dir(session_id) / "audio"
    if not audio_dir.is_dir():
        return []
    out: list[Path] = []
    for base in _AUDIO_BASENAMES:
        for suffix in _AUDIO_SUFFIXES:
            candidate = audio_dir / f"{base}{suffix}"
            if candidate.exists():
                out.append(candidate)
                break  # one extension per base; don't double-count
    return out


def has_retained_audio(session_id: str) -> bool:
    return bool(session_audio_files(session_id))


def models_dir() -> Path:
    path = app_data_dir() / "models"
    path.mkdir(parents=True, exist_ok=True)
    return path


def prompts_dir() -> Path:
    """User-editable prompt-template directory (seeded on first run)."""
    path = app_data_dir() / "prompts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return app_data_dir() / "config.toml"


def vocabulary_path() -> Path:
    return app_data_dir() / "vocabulary.txt"


def calendar_state_path() -> Path:
    return app_data_dir() / "calendar_state.json"


def audio_session_state_path() -> Path:
    return app_data_dir() / "audio_session_state.json"


def db_path() -> Path:
    return app_data_dir() / "sessions.db"


def lock_path() -> Path:
    return app_data_dir() / "instance.lock"


def log_path() -> Path:
    return app_data_dir() / "meeting_notetaker.log"


# How many historical log files to keep alongside the current one.
# Total on disk = LOG_HISTORY_KEEP + the live meeting_notetaker.log
# (so 5 historical + 1 current = 6 files max).
LOG_HISTORY_KEEP = 5


def rotate_log_on_launch(now: object | None = None) -> Path | None:
    """Move the existing meeting_notetaker.log aside before the new
    session opens its file handler, so each launch gets a fresh log.

    Returns the archived path (or None if there was nothing to rotate
    -- first launch on a fresh install). Also prunes the historical
    list down to LOG_HISTORY_KEEP entries, oldest first.

    ``now`` is overridable for tests; in production it's datetime.now().
    """
    import datetime as _dt  # noqa: PLC0415

    if now is None:
        now = _dt.datetime.now()
    current = log_path()
    archived: Path | None = None
    if current.exists() and current.stat().st_size > 0:
        stamp = now.strftime("%Y%m%d-%H%M%S")
        candidate = current.parent / f"meeting_notetaker-{stamp}.log"
        # Highly unlikely but guard against collision (two launches in
        # the same second -- e.g. supervisor restart loops).
        counter = 1
        while candidate.exists():
            candidate = current.parent / f"meeting_notetaker-{stamp}-{counter}.log"
            counter += 1
        try:
            current.rename(candidate)
            archived = candidate
        except OSError:
            # If the rename fails (Windows handles, AV scanner, etc),
            # we'd rather have appended logs than a missing log. Leave
            # the existing file in place; basicConfig will append.
            archived = None
    # Prune historical files. Sort by mtime descending; keep the first
    # LOG_HISTORY_KEEP, delete the rest.
    history = sorted(
        current.parent.glob("meeting_notetaker-*.log"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for stale in history[LOG_HISTORY_KEEP:]:
        try:
            stale.unlink()
        except OSError:
            pass
    return archived


def package_root() -> Path:
    """Root of the meeting_notetaker package (for bundled resources)."""
    return Path(__file__).resolve().parent.parent


def resource_path(*parts: str) -> Path:
    return package_root() / "resources" / Path(*parts)


def automation_dir() -> Path:
    """Where the unpacked Chrome extension + native-host manifest live."""
    path = app_data_dir() / "automation"
    path.mkdir(parents=True, exist_ok=True)
    return path


def extension_dir() -> Path:
    """The unpacked extension folder the user loads via chrome://extensions."""
    path = automation_dir() / "extension"
    path.mkdir(parents=True, exist_ok=True)
    return path


def native_host_manifest_path() -> Path:
    """The native-messaging-host manifest JSON Chrome reads."""
    return automation_dir() / "com.meeting_notetaker.bridge.json"


def bridge_handshake_path() -> Path:
    """Loopback port + auth token, written by the running app for the host
    to read. JSON: {"port": int, "token": str, "pid": int}."""
    return automation_dir() / "bridge.json"
