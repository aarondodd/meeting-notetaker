"""Filesystem locations the relay reads + writes.

Per the user's answers (2026-05-31): data dir lives inside the
experiments folder, not %APPDATA%. Keeps captures next to the source
so copying samples back to Aaron is a single `git diff` away.
"""
from __future__ import annotations

import os
from pathlib import Path


# experiments/owa-calendar-probe/ is the conventional root. We resolve
# relative to this file so `python relay/probe_app.py` from any cwd
# lands in the right place.
PROBE_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROBE_ROOT / "data"
EXTENSION_DIR = PROBE_ROOT / "extension"


def data_dir() -> Path:
    # MN_PROBE_DATA_DIR lets smoke tests + scripted invocations point
    # the relay at a sandboxed dir without touching the project tree.
    # bridge.json, bridge.log, capture files, the host wrapper, and
    # (on POSIX dev) the Chrome NMH manifest all derive from here.
    override = os.environ.get("MN_PROBE_DATA_DIR")
    target = Path(override).expanduser() if override else DATA_DIR
    target.mkdir(parents=True, exist_ok=True)
    return target


def handshake_path() -> Path:
    """Where the relay writes its loopback port + auth token. The
    native host reads this on every Chrome connectNative() call.

    Lives under data/ so the gitignore covers it -- tokens rotate per
    launch but there's no reason to commit even a stale one."""
    return data_dir() / "bridge.json"


def bridge_log_path() -> Path:
    return data_dir() / "bridge.log"


def host_wrapper_path() -> Path:
    """The .cmd / .sh wrapper Chrome's native-messaging manifest points
    at. The wrapper invokes the Python interpreter + native_host.py."""
    if os.name == "nt":
        return data_dir() / "native_host.cmd"
    return data_dir() / "native_host.sh"


def native_host_manifest_path() -> Path:
    """JSON manifest Chrome reads to learn where the host lives. On
    Windows the registry value points HERE; on POSIX (dev only),
    Chrome looks under ~/.config/google-chrome/NativeMessagingHosts/."""
    if os.name == "nt":
        return data_dir() / "com.meeting_notetaker.probe.json"
    return (
        Path.home()
        / ".config"
        / "google-chrome"
        / "NativeMessagingHosts"
        / "com.meeting_notetaker.probe.json"
    )
