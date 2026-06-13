"""GitHub-releases-based update checker + installer-driven upgrade.

The bundled .exe distributed via Meeting Notetaker's Inno Setup installer
self-updates by downloading the latest release's installer asset and
re-running it silently. Inno Setup's stable AppId means the running
install gets upgraded in place; the CloseApplications +
RestartApplications directives in installer.iss handle "the app is
currently running" via Windows Restart Manager and relaunch the app
once the install completes.

On a successful check the latest release is fetched from:

    https://api.github.com/repos/<owner>/<repo>/releases/latest

If the response tag_name parses to a higher version tuple than the
bundled __version__, the offer is surfaced. On accept, the matching
`meeting-notetaker-setup-X.Y.Z.exe` asset is downloaded to
`%APPDATA%\\MeetingNotetaker\\updates\\`, launched with `/SILENT
/SUPPRESSMSGBOXES`, and this process exits to let Restart Manager close
us before the installer touches the .exe.

Failures degrade silently (return False or None):
- Repo is private and the request is unauthenticated -> 404
- A corporate proxy / MITM appliance rejects the call -> URLError
- DNS is sandboxed
- The release exists but has no installer asset attached (CI didn't run)

Running from source (not frozen) returns a "use your own update workflow"
message rather than trying to launch an installer.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from ..version import __version__
from .paths import app_data_dir


log = logging.getLogger(__name__)


CHECK_INTERVAL_DAYS = 7  # Weekly auto-check.

DEFAULT_GITHUB_OWNER = "aarondodd"
DEFAULT_GITHUB_REPO = "meeting-notetaker"

USER_AGENT = f"meeting-notetaker/{__version__}"

# Filename pattern produced by .github/workflows/release.yml +
# installer.iss. The version capture is used to verify the asset matches
# the release tag.
INSTALLER_ASSET_PATTERN = re.compile(
    r"^meeting-notetaker-setup-(?P<version>[\w.+-]+)\.exe$"
)


# --------- last-check persistence ------------------------------------------


def version_check_file() -> Path:
    """Path to the file that stores the last-check timestamp (ISO-8601)."""
    return app_data_dir() / "last_version_check.txt"


def get_last_version_check(now_path: Optional[Path] = None) -> Optional[datetime]:
    """Read the last-check timestamp, or None if no record exists."""
    path = now_path or version_check_file()
    if not path.exists():
        return None
    try:
        return datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def record_version_check(
    *, when: Optional[datetime] = None, now_path: Optional[Path] = None
) -> None:
    """Stamp the current time (or `when`) as the last-check moment."""
    path = now_path or version_check_file()
    stamp = (when or datetime.now()).isoformat()
    try:
        path.write_text(stamp, encoding="utf-8")
    except OSError:
        # Disk full / read-only homedir -- not worth crashing the app.
        pass


# --------- version comparison ----------------------------------------------


def parse_version(version_str: str) -> Optional[Tuple[int, ...]]:
    """Parse 'v1.2.3' or '1.2.3[-pre]' into (1, 2, 3). Returns None on failure.

    Strips a leading 'v' and any pre-release suffix (anything after the
    first '-'), so '0.5.0-dev' parses the same as '0.5.0'. Pre-release
    builds therefore compare equal to their final release, which is
    appropriate for the update-check use case: an in-place upgrade from
    a -dev build to the published x.y.z should still trigger.
    """
    if not version_str:
        return None
    cleaned = version_str.strip()
    if cleaned.startswith(("v", "V")):
        cleaned = cleaned[1:]
    # Drop pre-release suffix ('-dev', '-rc1', '+build42', etc.).
    for sep in ("-", "+"):
        if sep in cleaned:
            cleaned = cleaned.split(sep, 1)[0]
    try:
        return tuple(int(part) for part in cleaned.split("."))
    except (ValueError, AttributeError):
        return None


def is_newer_version(remote_version: str, local_version: str) -> bool:
    """True iff `remote_version` parses to a strictly higher tuple."""
    remote = parse_version(remote_version)
    local = parse_version(local_version)
    if remote is None or local is None:
        return False
    return remote > local


# --------- check + fetch ----------------------------------------------------


def should_check_for_updates(
    *, now: Optional[datetime] = None, last_check_path: Optional[Path] = None
) -> bool:
    """True if we haven't checked in CHECK_INTERVAL_DAYS days."""
    last_check = get_last_version_check(last_check_path)
    if last_check is None:
        return True
    return (now or datetime.now()) - last_check > timedelta(days=CHECK_INTERVAL_DAYS)


def get_latest_release(
    *, owner: str = DEFAULT_GITHUB_OWNER, repo: str = DEFAULT_GITHUB_REPO
) -> Optional[Dict[str, Any]]:
    """Fetch the latest release from GitHub. Returns dict on success, else None.

    Returned dict shape: {"tag_name": "0.5.0", "assets": [...], ...}.
    Returns None on any network/parse failure -- 404 included. Callers
    should treat None as "no update available".
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    request = urllib.request.Request(url)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError):
        return None

    tag = data.get("tag_name", "")
    if not tag:
        return None
    return {
        "tag_name": tag.lstrip("vV"),
        "assets": data.get("assets", []),
        "html_url": data.get("html_url", ""),
        "body": data.get("body", ""),
    }


def get_installer_asset(release: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Locate the meeting-notetaker-setup-X.Y.Z.exe asset on a release.

    Returns (version, browser_download_url) on success, None if the
    release has no installer asset attached. This happens when:
      - The release was created manually without running the workflow.
      - The workflow failed before the upload step.
      - Someone is testing against an old release predating v0.6.6.
    """
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        match = INSTALLER_ASSET_PATTERN.match(name)
        if not match:
            continue
        url = asset.get("browser_download_url", "")
        if not url:
            continue
        return (match.group("version"), url)
    return None


def check_for_updates(
    *,
    owner: str = DEFAULT_GITHUB_OWNER,
    repo: str = DEFAULT_GITHUB_REPO,
    now: Optional[datetime] = None,
    last_check_path: Optional[Path] = None,
) -> Optional[Tuple[str, str]]:
    """Auto-check entry point. Returns (local, remote) if a newer release exists.

    Stamps the last-check time as a side-effect so callers don't have to.
    Respects the weekly interval; returns None if the interval hasn't
    elapsed yet.
    """
    if not should_check_for_updates(now=now, last_check_path=last_check_path):
        return None
    record_version_check(when=now, now_path=last_check_path)
    release = get_latest_release(owner=owner, repo=repo)
    if release is None:
        return None
    if is_newer_version(release["tag_name"], __version__):
        return (__version__, release["tag_name"])
    return None


# --------- download + install -----------------------------------------------


def download_release(url: str, dest_path: Path) -> bool:
    """Stream a release asset to dest_path. Returns True on success."""
    request = urllib.request.Request(url)
    request.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            with open(dest_path, "wb") as f:
                shutil.copyfileobj(response, f)
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False


def is_frozen() -> bool:
    """True iff this Python is running from a pyinstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def current_exe_path() -> Optional[Path]:
    """Path to the currently running bundled executable, or None.

    Returns None when running from source (sys.executable would point
    at the python interpreter), so callers can skip the installer
    launch and surface a "use your own update workflow" message instead.
    """
    if not is_frozen():
        return None
    try:
        path = Path(sys.executable)
        if path.exists():
            return path
    except OSError:
        return None
    return None


def updates_dir() -> Path:
    """Per-user directory where downloaded installer .exes are cached.

    Lives under app_data_dir() so Inno Setup's silent invocation can
    read it after the host app has exited (Windows temp cleanup runs
    later and would race the install otherwise).
    """
    path = app_data_dir() / "updates"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


def prune_updates_cache(*, keep_newest: int = 1) -> list[Path]:
    """Delete cached installer .exe files older than the most-recently-
    modified ``keep_newest`` payloads in ``updates_dir()`` (#102 bug 5).

    The updater downloads each release's installer to
    ``%APPDATA%\\MeetingNotetaker\\updates\\meeting-notetaker-setup-X.Y.Z.exe``
    and never sweeps the old ones, so the directory bloats by one
    fat installer per upgrade. Once Inno Setup has finished its job
    the .exe is dead weight. Call this on app startup to keep the
    cache bounded.

    Default ``keep_newest=1`` retains the installer that produced the
    currently-running build (handy if the user wants to inspect or
    re-run it). Pass ``keep_newest=0`` to delete every cached
    installer.

    Returns the list of paths that were deleted (best-effort -- a
    failure to unlink a single file is logged + skipped, not
    propagated).
    """
    cache = updates_dir()
    if not cache.is_dir():
        return []
    installers = sorted(
        cache.glob("meeting-notetaker-setup-*.exe"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    deleted: list[Path] = []
    for installer in installers[max(0, keep_newest):]:
        try:
            installer.unlink()
            deleted.append(installer)
        except OSError as exc:
            log.warning(
                "Could not delete cached installer %s: %s",
                installer, exc,
            )
    return deleted


def launch_installer(installer_path: Path) -> Tuple[bool, str]:
    """Spawn the installer with silent flags and return immediately.

    The installer runs detached so its lifetime survives this process
    exiting. /SILENT suppresses the wizard UI; /SUPPRESSMSGBOXES
    auto-confirms any prompts. /NORESTART tells Inno Setup not to
    request a Windows reboot even if it normally would -- our installer
    never sets that flag, but pass it defensively.

    Inno Setup's CloseApplications=yes + RestartApplications=yes (in
    installer.iss) handle the "app is currently running" case via
    Restart Manager and relaunch the app after install.
    """
    if not installer_path.exists():
        return False, f"Installer not found at {installer_path}."
    cmd = [
        str(installer_path),
        "/SILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
    ]
    kwargs: Dict[str, Any] = {}
    if sys.platform.startswith("win"):
        # DETACHED_PROCESS so the child doesn't inherit our console
        # handles; CREATE_NEW_PROCESS_GROUP so a Ctrl-C in a parent
        # terminal doesn't kill it.
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
        kwargs["close_fds"] = True
    try:
        subprocess.Popen(cmd, **kwargs)
    except OSError as exc:
        return False, f"Could not launch installer: {exc}"
    return True, f"Installer launched: {installer_path.name}"


def upgrade(
    *,
    owner: str = DEFAULT_GITHUB_OWNER,
    repo: str = DEFAULT_GITHUB_REPO,
    progress_callback: Optional[Callable[[str, str], None]] = None,
) -> Tuple[bool, str]:
    """End-to-end upgrade: fetch -> find installer -> download -> launch silently.

    progress_callback receives (stage, message) tuples; stages are:
    'fetch', 'download', 'launch', 'done'.

    Returns (True, msg) when the installer has been launched and the
    caller should close the app to let Restart Manager + Inno Setup take
    over. Returns (False, msg) for any failure (no network, no asset,
    download failed, launch failed).

    Running from source (not pyinstaller-frozen) returns
    (False, msg) with guidance to upgrade via the user's own workflow
    (git pull + rebuild). There's no installed .exe to upgrade in that
    case and silently kicking off a system-wide install would be
    surprising behavior.
    """
    def notify(stage: str, message: str) -> None:
        if progress_callback:
            progress_callback(stage, message)

    if not is_frozen():
        return False, (
            "This Meeting Notetaker is running from source, not from the "
            "Inno Setup installer. The built-in updater only upgrades "
            "installer-managed installs.\n\n"
            "To update a source / portable build, use your own workflow "
            "(for example: git pull + .\\build.ps1, or re-download a "
            "portable .exe from the Releases page)."
        )

    notify("fetch", "Checking GitHub for the latest release...")
    release = get_latest_release(owner=owner, repo=repo)
    if release is None:
        return False, "Could not fetch release information (check network connectivity)."

    remote_version = release["tag_name"]
    if not is_newer_version(remote_version, __version__):
        return True, f"Already on the latest version ({__version__})."

    asset = get_installer_asset(release)
    if asset is None:
        return False, (
            f"Release {remote_version} exists but has no installer asset "
            f"attached (expected `meeting-notetaker-setup-{remote_version}.exe`). "
            "The release workflow may have failed; check "
            f"https://github.com/{owner}/{repo}/releases."
        )
    asset_version, asset_url = asset

    download_dir = updates_dir()
    installer_path = download_dir / f"meeting-notetaker-setup-{asset_version}.exe"

    notify("download", f"Downloading installer for {remote_version}...")
    if not download_release(asset_url, installer_path):
        return False, "Failed to download installer."

    notify("launch", f"Launching installer for {remote_version}...")
    ok_launch, launch_msg = launch_installer(installer_path)
    if not ok_launch:
        return False, launch_msg + f"\n\nDownloaded installer: {installer_path}"

    notify("done", f"Installer launched for {remote_version}.")
    return True, (
        f"Installer for {remote_version} has been launched silently. "
        "Meeting Notetaker will close shortly; Windows Restart Manager "
        "handles the upgrade and relaunches the app when the install "
        "completes.\n\n"
        f"Downloaded to: {installer_path}"
    )
