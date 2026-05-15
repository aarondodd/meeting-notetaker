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


def db_path() -> Path:
    return app_data_dir() / "sessions.db"


def lock_path() -> Path:
    return app_data_dir() / "instance.lock"


def log_path() -> Path:
    return app_data_dir() / "meeting_notetaker.log"


def package_root() -> Path:
    """Root of the meeting_notetaker package (for bundled resources)."""
    return Path(__file__).resolve().parent.parent


def resource_path(*parts: str) -> Path:
    return package_root() / "resources" / Path(*parts)
