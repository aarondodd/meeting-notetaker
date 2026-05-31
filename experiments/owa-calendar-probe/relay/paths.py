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


def native_host_manifest_paths() -> list[Path]:
    """JSON manifest locations Chrome/Edge read to learn where the
    native host lives.

    Windows: a single bundled manifest under the probe data dir; the
    HKCU registry value points at it.

    POSIX (dev box): every browser has its own NMH directory. We
    install into all of them whose parent dir exists -- Chrome,
    Chromium, Chrome Beta/Dev, Edge, Edge Beta/Dev. The browser
    only reads its own dir, so installing in all of them is a
    convenience for switching browsers without re-running the
    installer."""
    if os.name == "nt":
        return [data_dir() / "com.meeting_notetaker.probe.json"]

    candidates = [
        Path.home() / ".config" / "google-chrome",
        Path.home() / ".config" / "google-chrome-beta",
        Path.home() / ".config" / "google-chrome-dev",
        Path.home() / ".config" / "chromium",
        Path.home() / ".config" / "microsoft-edge",
        Path.home() / ".config" / "microsoft-edge-beta",
        Path.home() / ".config" / "microsoft-edge-dev",
    ]
    return [
        c / "NativeMessagingHosts" / "com.meeting_notetaker.probe.json"
        for c in candidates
        if c.exists()
    ]


def native_host_manifest_path() -> Path:
    """Legacy single-path accessor. Returns the first install target
    (Chrome's NMH dir on POSIX; the bundled path on Windows). Tests
    + uninstall use native_host_manifest_paths() for the full set."""
    paths = native_host_manifest_paths()
    if paths:
        return paths[0]
    # No browser config dir exists yet -- fall back to Chrome's path
    # so install_host can create the dir tree.
    if os.name == "nt":
        return data_dir() / "com.meeting_notetaker.probe.json"
    return (
        Path.home()
        / ".config"
        / "google-chrome"
        / "NativeMessagingHosts"
        / "com.meeting_notetaker.probe.json"
    )
