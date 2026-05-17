"""GitHub-releases-based update checker + in-place upgrade.

On a successful check the latest release is fetched from:

    https://api.github.com/repos/<owner>/<repo>/releases/latest

If the response tag_name parses to a higher version tuple than the
bundled __version__, the offer is surfaced and -- on accept -- the
source zipball is downloaded, extracted, and build.ps1 (Windows) or
build.sh (POSIX) is invoked to drive pyinstaller. The freshly built
executable is then copied over the currently running .exe via the
NTFS rename-while-running trick: the old binary becomes <name>.old
and the new one takes its place. A subsequent startup cleans the
.old file up.

Failures are deliberately swallowed and reported back as "no update
available" rather than raising:
- Repo is private and the request is unauthenticated -> 404
- A corporate proxy / MITM appliance rejects the call -> URLError
- DNS is sandboxed
None of those should crash the host app.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from ..version import __version__
from .paths import app_data_dir


log = logging.getLogger(__name__)


CHECK_INTERVAL_DAYS = 7  # Weekly auto-check; same cadence as progman-py.

DEFAULT_GITHUB_OWNER = "aarondodd"
DEFAULT_GITHUB_REPO = "meeting-notetaker"

USER_AGENT = f"meeting-notetaker/{__version__}"


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
    """Parse 'v1.2.3' or '1.2.3' into (1, 2, 3). Returns None on failure."""
    if not version_str:
        return None
    cleaned = version_str.strip()
    if cleaned.startswith(("v", "V")):
        cleaned = cleaned[1:]
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

    Returned dict shape: {"tag_name": "0.5.0", "zipball_url": "..."}.
    Returns None on any network/parse failure -- private-repo 404
    included. Callers should treat None as "no update available".
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
    zipball = data.get("zipball_url", "")
    if not tag or not zipball:
        return None
    return {
        "tag_name": tag.lstrip("vV"),
        "zipball_url": zipball,
        "html_url": data.get("html_url", ""),
        "body": data.get("body", ""),
    }


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


# --------- download + build -------------------------------------------------


def download_release(zipball_url: str, dest_path: Path) -> bool:
    """Stream the release zipball to dest_path. Returns True on success."""
    request = urllib.request.Request(zipball_url)
    request.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            with open(dest_path, "wb") as f:
                shutil.copyfileobj(response, f)
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False


def extract_archive(zip_path: Path, extract_dir: Path) -> Optional[Path]:
    """Extract the zipball; returns the top-level source directory."""
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)
    except (zipfile.BadZipFile, OSError):
        return None
    entries = list(extract_dir.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return extract_dir


def find_build_script(source_dir: Path) -> Optional[Path]:
    """Locate build.ps1 (Windows) or build.sh (POSIX) within source_dir."""
    script_name = "build.ps1" if platform.system() == "Windows" else "build.sh"
    direct = source_dir / script_name
    if direct.exists():
        return direct
    # Scan up to two levels deep in case the zipball wraps in another folder.
    for candidate in source_dir.rglob(script_name):
        depth = len(candidate.relative_to(source_dir).parts)
        if depth <= 3:
            return candidate
    return None


def run_build_script(
    script_path: Path, *, output_callback: Optional[Callable[[str], None]] = None
) -> Tuple[bool, str]:
    """Invoke the build script (which calls pyinstaller). Streams stdout/stderr."""
    if platform.system() == "Windows":
        shell_cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-File"]
    else:
        shell_cmd = ["bash"]
        try:
            os.chmod(script_path, 0o755)
        except OSError:
            pass

    output_lines: list[str] = []
    try:
        process = subprocess.Popen(
            shell_cmd + [str(script_path)],
            cwd=str(script_path.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip("\n\r")
            output_lines.append(line)
            if output_callback:
                output_callback(line)
        process.wait()
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Could not run build script: {exc}"

    joined = "\n".join(output_lines)
    if process.returncode == 0:
        return True, joined
    return False, f"Build failed with exit code {process.returncode}\n\n{joined}"


# --------- in-place install -------------------------------------------------


def _exe_filename() -> str:
    """Filename of the bundled executable for this platform."""
    return "meeting-notetaker.exe" if platform.system() == "Windows" else "meeting-notetaker"


def is_frozen() -> bool:
    """True iff this Python is running from a pyinstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def current_exe_path() -> Optional[Path]:
    """Path to the currently running bundled executable, or None.

    Returns None when running from source (sys.executable would point
    at the python interpreter), so callers can skip the in-place
    install and surface a "your dev install built a fresh dist/" message
    instead.
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


def find_built_exe(source_dir: Path) -> Optional[Path]:
    """Locate the freshly built executable under source_dir/dist/.

    Walks at most three levels deep so a zipball that wraps the project
    in an extra folder still resolves. Returns the first match by name.
    """
    target_name = _exe_filename()
    dist = source_dir / "dist"
    if dist.exists():
        direct = dist / target_name
        if direct.exists():
            return direct
    for candidate in source_dir.rglob(target_name):
        try:
            depth = len(candidate.relative_to(source_dir).parts)
        except ValueError:
            continue
        if depth <= 4 and candidate.is_file():
            return candidate
    return None


def old_exe_path(target: Path) -> Path:
    """Sibling path the running .exe gets renamed to during an install."""
    return target.with_suffix(target.suffix + ".old")


def install_in_place(new_exe: Path, target: Path) -> Tuple[bool, str]:
    """Replace `target` with `new_exe` in place.

    On Windows, the running .exe cannot be deleted while in use, but
    NTFS does allow renaming it. The standard pattern is:

      1. Move any leftover <target>.old out of the way.
      2. Rename current target -> target.old (frees the canonical path).
      3. Copy new_exe over target.
      4. On next launch, cleanup_old_exe removes <target>.old.

    Returns (ok, message). On failure, attempts to undo step 2 so the
    running install isn't left in a half-replaced state.
    """
    new_exe = Path(new_exe)
    target = Path(target)
    if not new_exe.exists() or not new_exe.is_file():
        return False, f"New executable not found at {new_exe}."
    if not target.exists():
        return False, f"Target executable not found at {target}."

    backup = old_exe_path(target)
    if backup.exists():
        try:
            backup.unlink()
        except OSError:
            # The previous .old is still locked (rare); leave it for
            # the next launch's cleanup. Rename below will overwrite
            # only if Python's os.rename allows it on this OS; otherwise
            # we fall through and the rename fails cleanly.
            pass

    try:
        os.rename(target, backup)
    except OSError as exc:
        return False, f"Could not rename current executable: {exc}"

    try:
        shutil.copy2(new_exe, target)
    except OSError as exc:
        # Try to roll back so the user is left with a working install.
        try:
            if not target.exists():
                os.rename(backup, target)
        except OSError:
            pass
        return False, f"Could not copy new executable into place: {exc}"

    return True, f"Installed {new_exe.name} in place at {target}."


def cleanup_old_exe(target: Optional[Path] = None) -> bool:
    """Delete the .old sibling left by a previous install_in_place().

    Returns True if a stale .old existed and was removed; False if
    there was nothing to clean or the removal failed (we just retry on
    the next launch).
    """
    if target is None:
        current = current_exe_path()
        if current is None:
            return False
        target = current
    backup = old_exe_path(target)
    if not backup.exists():
        return False
    try:
        backup.unlink()
        return True
    except OSError:
        log.debug("could not remove stale %s; will retry on next launch", backup)
        return False


def upgrade(
    *,
    owner: str = DEFAULT_GITHUB_OWNER,
    repo: str = DEFAULT_GITHUB_REPO,
    progress_callback: Optional[Callable[[str, str], None]] = None,
) -> Tuple[bool, str]:
    """End-to-end upgrade: fetch -> verify -> download -> extract -> build -> install.

    progress_callback receives (stage, message) tuples; stages are:
      'fetch', 'download', 'extract', 'build', 'build_output',
      'install', 'done'.
    The 'build_output' stage receives a line of stdout/stderr per call
    so the UI can stream it into a text view; everything else is a
    status update for a label.

    When running from a pyinstaller bundle, the freshly built exe is
    installed over the running one via install_in_place. When running
    from source, the dist/ path is reported and the caller is expected
    to copy the build out manually.
    """
    def notify(stage: str, message: str) -> None:
        if progress_callback:
            progress_callback(stage, message)

    notify("fetch", "Checking GitHub for the latest release...")
    release = get_latest_release(owner=owner, repo=repo)
    if release is None:
        return False, "Could not fetch release information (check network / private repo)."

    remote_version = release["tag_name"]
    if not is_newer_version(remote_version, __version__):
        return True, f"Already on the latest version ({__version__})."

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        zip_path = tmp_path / "release.zip"
        extract_dir = tmp_path / "src"
        extract_dir.mkdir()

        notify("download", f"Downloading release {remote_version}...")
        if not download_release(release["zipball_url"], zip_path):
            return False, "Failed to download release zipball."

        notify("extract", "Extracting archive...")
        source_dir = extract_archive(zip_path, extract_dir)
        if source_dir is None:
            return False, "Failed to extract release archive."

        script = find_build_script(source_dir)
        if script is None:
            return False, "Could not find build.ps1 / build.sh in the release archive."

        notify("build", f"Running {script.name}...")
        ok, build_output = run_build_script(script, output_callback=lambda line: notify("build_output", line))
        if not ok:
            return False, f"Build failed:\n{build_output}"

        # Locate the freshly built executable. The build script writes
        # to <source_dir>/dist/<name>{,.exe}; if the zipball wrapped
        # the project in an extra folder, find_built_exe walks down.
        built = find_built_exe(source_dir)
        if built is None:
            return False, (
                "Build succeeded but the new executable could not be found "
                f"under {source_dir / 'dist'}."
            )

        target = current_exe_path()
        if target is None:
            # Running from source -- there's no installed exe to replace.
            # Report where the new build is so the user can move it
            # into place manually.
            return True, (
                f"Built {built.name} for {remote_version}, but this Python "
                "process is running from source -- there is no installed "
                "executable to replace.\n\nNew build: " + str(built)
            )

        notify("install", f"Installing {built.name} in place...")
        ok_install, install_msg = install_in_place(built, target)
        if not ok_install:
            return False, install_msg + f"\n\nNew build is at: {built}"

    notify("done", f"Upgraded {__version__} -> {remote_version}.")
    return True, (
        f"Upgraded to {remote_version} in place at {target}.\n\n"
        "Restart the app to load the new build."
    )
