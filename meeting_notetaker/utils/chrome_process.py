"""Chrome process detection + launch helpers for synthesis automation.

Three-state model the SessionView + status bar render from:

  * NOT_RUNNING        -- no chrome.exe in process table
  * RUNNING_CONNECTED  -- chrome.exe exists AND the bridge has a peer
  * RUNNING_DISCONNECTED -- chrome.exe exists but the bridge has no peer
                            (extension's service worker is asleep or
                            the user disabled the extension)

The first two states allow Send (button enabled); the third disables
Send because clicking would just fail.



The synthesis bridge can only see "is the extension talking to me?"
which conflates two distinct states: "Chrome isn't running" vs
"Chrome is running but the extension's service worker has been killed
and hasn't reconnected." The first is a normal state (user just hasn't
opened Chrome yet); the second is a recoverable failure.

We disambiguate by checking the OS process table for chrome.exe. The
SessionView's Send button gating + status-bar indicator use the
combined (bridge_connected, chrome_running) state to show three
distinct states:

  * Chrome not running        -> Send enabled (will launch Chrome)
  * Chrome running, connected -> Send enabled (normal flow)
  * Chrome running, disconnected -> Send disabled (broken extension)
"""
from __future__ import annotations

import enum
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional


log = logging.getLogger(__name__)


class SynthesisConnectionState(enum.Enum):
    """Combined (chrome_running, bridge_connected) state. Three values:

      * NOT_RUNNING        -- Chrome process not detected. Send is
                              enabled (will launch Chrome on click).
      * RUNNING_CONNECTED  -- Chrome up + bridge has a peer. Normal
                              flow.
      * RUNNING_DISCONNECTED -- Chrome up but no peer. Send disabled;
                              waiting for the alarm-based retry to
                              reconnect.
    """

    NOT_RUNNING = "not_running"
    RUNNING_CONNECTED = "running_connected"
    RUNNING_DISCONNECTED = "running_disconnected"

    @classmethod
    def derive(cls, *, chrome_running: bool, bridge_connected: bool) -> "SynthesisConnectionState":
        if not chrome_running:
            return cls.NOT_RUNNING
        if bridge_connected:
            return cls.RUNNING_CONNECTED
        return cls.RUNNING_DISCONNECTED

    def status_label(self) -> str:
        """Human-readable label rendered in the status bar."""
        return {
            SynthesisConnectionState.NOT_RUNNING: "Synthesis: Chrome not running",
            SynthesisConnectionState.RUNNING_CONNECTED: "Synthesis: Chrome running, connected",
            SynthesisConnectionState.RUNNING_DISCONNECTED: "Synthesis: Chrome running, disconnected",
        }[self]

    def status_tooltip(self) -> str:
        """Tooltip explaining what each state means."""
        return {
            SynthesisConnectionState.NOT_RUNNING:
                "Chrome isn't running. Clicking Send to Claude.ai will "
                "launch Chrome and open the synthesis tab automatically.",
            SynthesisConnectionState.RUNNING_CONNECTED:
                "The Meeting Notetaker extension is connected. Send is "
                "ready to use.",
            SynthesisConnectionState.RUNNING_DISCONNECTED:
                "Chrome is running but the extension hasn't connected "
                "back to the app. The extension service worker may be "
                "asleep; it should reconnect within ~60s. If it "
                "persists, open the extension popup and click "
                "'Reconnect to app'.",
        }[self]

    def dot_color(self) -> str:
        """Status-bar dot color name for this state.

        green = ready to send, yellow = Chrome cold (Send will warm it),
        red = Chrome up but the extension isn't talking to the app.
        """
        return {
            SynthesisConnectionState.NOT_RUNNING: "yellow",
            SynthesisConnectionState.RUNNING_CONNECTED: "green",
            SynthesisConnectionState.RUNNING_DISCONNECTED: "red",
        }[self]

    def send_button_enabled(self) -> bool:
        """Whether the Send-to-LLM button should be clickable in this
        state. Only RUNNING_DISCONNECTED disables -- the other two
        states either work as normal (RUNNING_CONNECTED) or trigger a
        launch-and-then-send sequence (NOT_RUNNING)."""
        return self is not SynthesisConnectionState.RUNNING_DISCONNECTED


# Process names that count as "Chrome is running." We exclude
# chromedriver and other related binaries; only the user-facing
# browser counts. Edge would be "msedge.exe" -- not handled here
# because the extension shipping target is Chrome.
_CHROME_PROCESS_NAMES = frozenset({
    "chrome.exe",
    "chrome",
    "Google Chrome",
})


def is_chrome_running() -> bool:
    """True if at least one Chrome browser process is alive in the OS
    process table.

    Uses psutil (already a runtime dep via the ad-hoc meeting
    detector). Returns False on any failure -- we treat "we don't
    know" as "not running" so the launch path is taken on Send, which
    is safe (launching when Chrome is already up just opens a new
    tab in the existing instance).
    """
    try:
        import psutil  # noqa: PLC0415
    except ImportError:
        log.warning("psutil not available; can't detect Chrome process")
        return False
    try:
        for proc in psutil.process_iter(["name"]):
            try:
                name = proc.info.get("name") or ""
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if name in _CHROME_PROCESS_NAMES:
                return True
    except (psutil.Error, OSError) as exc:
        log.debug("psutil process_iter failed: %s", exc)
        return False
    return False


def locate_chrome_exe() -> Optional[Path]:
    """Best-effort lookup for the Chrome executable. Tries:

      1. App Paths registry (HKLM/HKCU\\...\\App Paths\\chrome.exe)
         -- canonical Windows mechanism for finding installed apps.
      2. Default install paths under Program Files.
      3. Per-user install path under %LOCALAPPDATA%.
      4. ``shutil.which`` for PATH lookups.

    Returns None if none resolves. Lifted out of automation_install_dialog
    so both the install wizard and the launch-on-Send path share the
    same logic.
    """
    if sys.platform.startswith("win"):
        try:
            import winreg  # type: ignore[import-not-found]  # noqa: PLC0415

            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(
                        hive,
                        r"Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
                    ) as k:
                        value, _ = winreg.QueryValueEx(k, None)
                        if value:
                            p = Path(value)
                            if p.exists():
                                return p
                except FileNotFoundError:
                    continue
        except ImportError:
            pass

        candidates = [
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        ]
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            candidates.append(
                Path(local_app) / "Google" / "Chrome" / "Application" / "chrome.exe"
            )
        for c in candidates:
            if c.exists():
                return c

    import shutil  # noqa: PLC0415

    for name in ("chrome", "google-chrome", "chromium"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def launch_chrome(url: Optional[str] = None) -> bool:
    """Spawn Chrome (with an optional URL) and return immediately.

    Returns True on successful spawn, False if Chrome can't be located
    or the subprocess call fails. The caller is expected to follow up
    by waiting for the bridge to report connected.

    Note: launching Chrome via the command line opens a NEW WINDOW
    only if no other Chrome instance is running; otherwise the URL
    opens as a new tab in the existing instance. Either way, the
    user's session state (signed-in cookies, restored tabs) is
    preserved by Chrome.
    """
    chrome_exe = locate_chrome_exe()
    if chrome_exe is None:
        log.warning("Chrome executable not found on this system")
        return False
    args: list[str] = [str(chrome_exe)]
    if url:
        args.append(url)
    try:
        subprocess.Popen(  # noqa: S603 -- argv is a list, no shell
            args,
            close_fds=True,
        )
        log.info("launched Chrome via %s%s", chrome_exe, f" -> {url}" if url else "")
        return True
    except OSError as exc:
        log.warning("Chrome launch failed: %s", exc)
        return False
