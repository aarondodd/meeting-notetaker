"""Single-instance lock.

Writes the current PID to %APPDATA%/MeetingNotetaker/instance.lock. On launch,
if the lockfile exists AND its PID is still alive, the new instance returns
False so the caller can foreground the running window and exit.

Stale lockfiles (process not alive) are silently overwritten.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .paths import lock_path


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                ok = ctypes.windll.kernel32.GetExitCodeProcess(  # type: ignore[attr-defined]
                    handle, ctypes.byref(exit_code)
                )
                if not ok:
                    return False
                return exit_code.value == STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def acquire(path: Path | None = None) -> bool:
    """Try to claim the single-instance lock. True = acquired; False = another instance is live."""
    path = path or lock_path()
    if path.exists():
        try:
            other = int(path.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            other = 0
        if _pid_alive(other) and other != os.getpid():
            return False
    path.write_text(str(os.getpid()), encoding="utf-8")
    return True


def release(path: Path | None = None) -> None:
    path = path or lock_path()
    try:
        if path.exists():
            content = path.read_text(encoding="utf-8").strip()
            if content == str(os.getpid()):
                path.unlink()
    except OSError:
        pass
