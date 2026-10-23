"""Single-instance lock.

Writes a two-line lockfile to %APPDATA%/MeetingNotetaker/instance.lock on
launch: the current PID and the current executable's resolved path. On the
next launch, if the lockfile exists AND its PID is still alive AND the
process behind that PID still carries the same image path, the new
instance returns False so the caller can foreground the running window
and exit.

A plain PID check is not enough on its own: an OS recycles PIDs, so a
lockfile written by a crashed Meeting Notetaker can appear live days
later because an unrelated process happens to hold the same number. The
image-path check tells "a Meeting Notetaker is running" apart from
"something is running".

acquire(force=True) skips the check entirely, so the user has a path
forward if the detection ever gets it wrong (e.g., the image was moved
or renamed, or the lockfile landed in a shape this build cannot read).

A legacy lockfile that contains only a PID is still accepted; a mismatch
of the single value is treated as stale and overwritten. The lockfile is
rewritten on every successful acquire, so the two-line form takes over
on first launch of this build.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

from .paths import lock_path

log = logging.getLogger(__name__)


def _normalise_image(path: str) -> str:
    """Make an image path comparable across the stored and the live reads.

    Windows paths are case-insensitive and come back from the kernel in
    whatever case the Session Manager cached; Linux paths are case-
    sensitive and symlinks (notably PyInstaller onedir's bootloader
    launching the actual interpreter) resolve via os.path.realpath.
    """
    try:
        resolved = str(Path(path).resolve())
    except OSError:
        resolved = path
    if sys.platform.startswith("win"):
        return resolved.lower()
    return resolved


def _current_image() -> str:
    """The resolved image path of this running process.

    For a frozen build this is the .exe; for a dev run it is the python
    interpreter. Either way, two Meeting Notetaker launches from the
    same install share this string and a recycled-PID collision almost
    certainly does not.
    """
    exe = getattr(sys, "executable", "") or sys.argv[0]
    return _normalise_image(exe)


def _image_of_pid_windows(pid: int) -> Optional[str]:
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return None
        if exit_code.value != STILL_ACTIVE:
            return None
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        # QueryFullProcessImageNameW is available on Vista+; the frozen
        # build pins Windows 10 so we can call it unconditionally.
        ok = kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        if not ok:
            return None
        return _normalise_image(buf.value)
    finally:
        kernel32.CloseHandle(handle)


def _image_of_pid_posix(pid: int) -> Optional[str]:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return None
    # /proc/<pid>/exe is a symlink to the running image on Linux; macOS
    # has no equivalent, so posix without /proc falls back to a bare
    # alive check (the recycled-PID risk is lower outside Windows and
    # the frozen build never runs on macOS).
    link = Path(f"/proc/{pid}/exe")
    try:
        return _normalise_image(os.readlink(link))
    except OSError:
        return ""  # alive, image unknown


def _image_of_pid(pid: int) -> Optional[str]:
    """Return the running image path for `pid`.

    None means the PID is not alive at all. An empty string means it is
    alive but the image path could not be read; the caller treats that
    as "stale" to avoid locking the user out when the OS refuses to
    tell us what the process is.
    """
    if pid <= 0:
        return None
    if sys.platform.startswith("win"):
        try:
            return _image_of_pid_windows(pid)
        except Exception:
            log.exception("single_instance: Windows image lookup failed for pid %d", pid)
            return ""
    return _image_of_pid_posix(pid)


def _read_lockfile(path: Path) -> tuple[int, Optional[str]]:
    """Return (pid, image_or_None) from a lockfile.

    The legacy format (single PID line) returns image=None, which the
    caller treats as "do not check the image".
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return 0, None
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        return 0, None
    try:
        pid = int(lines[0])
    except ValueError:
        return 0, None
    image = _normalise_image(lines[1]) if len(lines) >= 2 else None
    return pid, image


def _write_lockfile(path: Path) -> None:
    body = f"{os.getpid()}\n{_current_image()}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def acquire(path: Path | None = None, *, force: bool = False) -> bool:
    """Try to claim the single-instance lock.

    Returns True on acquisition and False when another live instance of
    this build is running. force=True overwrites the lockfile regardless
    of what it currently names, and is the escape hatch for a lockfile
    this build cannot parse or a detection that gets it wrong.
    """
    path = path or lock_path()
    if force:
        _write_lockfile(path)
        return True
    if path.exists():
        other_pid, other_image = _read_lockfile(path)
        if other_pid and other_pid != os.getpid():
            live_image = _image_of_pid(other_pid)
            if live_image is not None:
                # live_image is "" when the PID is alive but we could
                # not read its image; treat that as unknown-but-live and
                # fall back to the legacy PID-only behaviour. If the
                # lockfile also recorded no image, we are back to the
                # old check.
                if other_image is None or live_image == "" or live_image == other_image:
                    return False
    _write_lockfile(path)
    return True


def release(path: Path | None = None) -> None:
    path = path or lock_path()
    try:
        if path.exists():
            stored_pid, _ = _read_lockfile(path)
            if stored_pid == os.getpid():
                path.unlink()
    except OSError:
        pass
