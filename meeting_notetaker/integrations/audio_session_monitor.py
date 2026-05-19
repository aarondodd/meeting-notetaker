"""Ad-hoc meeting auto-detect via Windows audio sessions (pycaw + COM).

When a known meeting app (Teams, Zoom, Slack, WebEx, GoToMeeting, ...) is
actively playing audio for long enough to look like a real call rather than
a notification chirp, the monitor surfaces a tray toast: "Teams call
detected -- start recording?" The user clicks to open New Session;
recording never auto-starts.

Detection uses IAudioSessionManager2 (per-render-endpoint enumeration of
active sessions, owning process IDs, peak meter values) via pycaw. No
audio is captured; the OS already tracks which processes have non-silent
sessions and the cost is two short COM calls per tick.

The QObject + signal layer is in AudioSessionMonitor at the bottom. Pure
helpers (_RunState, _CooldownStore, _classify_processes) are testable on
Linux without pycaw or psutil installed.
"""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional


log = logging.getLogger(__name__)


# Default allowlist of process names that indicate a meeting is in progress.
# Each entry is matched case-insensitively against the owning process's
# image name. Browser-based meetings (Meet in Chrome) are intentionally
# excluded: chrome.exe also plays YouTube, music, etc. and the false-positive
# rate is too high to be useful as a one-shot allowlist signal.
DEFAULT_APP_ALLOWLIST: tuple[str, ...] = (
    "Teams.exe",
    "ms-teams.exe",
    "Zoom.exe",
    "ZoomPhone.exe",
    "slack.exe",
    "WebexMta.exe",
    "atmgr.exe",  # WebEx Meeting Manager
    "GoToMeetingWinStore.exe",
    "Discord.exe",
)


# Friendly display names for known executables. Falls back to the bare
# process name (sans .exe) when not listed.
_APP_DISPLAY_NAMES: dict[str, str] = {
    "teams.exe": "Microsoft Teams",
    "ms-teams.exe": "Microsoft Teams",
    "zoom.exe": "Zoom",
    "zoomphone.exe": "Zoom Phone",
    "slack.exe": "Slack",
    "webexmta.exe": "Cisco WebEx",
    "atmgr.exe": "Cisco WebEx",
    "gotomeetingwinstore.exe": "GoToMeeting",
    "discord.exe": "Discord",
}


def display_name_for(process_name: str) -> str:
    """Return a friendly app label for a process executable name."""
    return _APP_DISPLAY_NAMES.get(process_name.lower(), process_name.rsplit(".", 1)[0])


@dataclass
class MeetingAudioInfo:
    """Carried by meeting_audio_detected; the click handler uses it to
    pre-fill the session title and the tray balloon body."""

    process_name: str
    app_label: str
    first_detected_at: datetime
    sustained_seconds: float


def is_available() -> bool:
    """True if pycaw + psutil are importable on this host (Windows only)."""
    if not sys.platform.startswith("win"):
        return False
    try:
        import pycaw.pycaw  # noqa: F401
        import psutil  # noqa: F401
        return True
    except ImportError as exc:
        log.info("pycaw / psutil not importable: %s", exc)
        return False
    except Exception as exc:
        log.warning("pycaw import raised unexpectedly: %s", exc)
        return False


# ---- pure helpers (testable without pycaw) ---------------------------------


@dataclass
class _SessionSnapshot:
    """A single audio session as observed in one poll tick. The
    enumerator side produces these; everything downstream is pure logic."""

    process_name: str
    peak: float  # 0.0 to 1.0


@dataclass
class _RunState:
    """Tracks how long a given allowlisted process has been continuously
    above the silence floor. Resets on a silent tick."""

    process_name: str
    started_at: datetime
    last_active_at: datetime
    fired: bool = False  # already emitted for this run; suppress until reset


def _filter_allowlisted(
    snapshots: list[_SessionSnapshot],
    allowlist: list[str],
    silence_floor: float,
) -> list[_SessionSnapshot]:
    """Return only sessions whose process name matches the allowlist
    (case-insensitive) and whose peak is above the silence floor."""
    norm = {a.lower() for a in allowlist}
    return [
        s for s in snapshots
        if s.process_name.lower() in norm and s.peak >= silence_floor
    ]


def _advance_runs(
    runs: dict[str, _RunState],
    active: list[_SessionSnapshot],
    now: datetime,
) -> list[str]:
    """Update run tracking against this tick's active sessions; drop any
    runs that went silent. Returns the list of process names that just
    went silent so the caller can also clear their cooldown reservations
    if it chooses (we do not -- a fresh run starts a fresh window)."""
    active_names = {s.process_name for s in active}
    expired = [name for name in runs.keys() if name not in active_names]
    for name in expired:
        del runs[name]
    for snap in active:
        run = runs.get(snap.process_name)
        if run is None:
            runs[snap.process_name] = _RunState(
                process_name=snap.process_name,
                started_at=now,
                last_active_at=now,
            )
        else:
            run.last_active_at = now
    return expired


def _runs_ready_to_fire(
    runs: dict[str, _RunState],
    cooldowns: dict[str, datetime],
    min_duration_sec: float,
    now: datetime,
) -> list[_RunState]:
    """Yield runs that have sustained past min_duration, haven't already
    fired in this run, and are not currently in a per-process cooldown."""
    ready: list[_RunState] = []
    for run in runs.values():
        if run.fired:
            continue
        until = cooldowns.get(run.process_name)
        if until is not None and until > now:
            continue
        duration = (run.last_active_at - run.started_at).total_seconds()
        if duration >= min_duration_sec:
            ready.append(run)
    return ready


# ---- cooldown persistence -------------------------------------------------


class _CooldownStore:
    """Tracks per-process cooldown expirations so a dismissed prompt
    doesn't re-fire for the same app for `cooldown_seconds`.

    Persisted to JSON so an app restart doesn't immediately re-prompt for
    audio that was still playing when the user dismissed the toast.
    Entries older than `now` are pruned on every read."""

    def __init__(self, path: Path, *, now: Optional[Callable[[], datetime]] = None) -> None:
        self.path = Path(path)
        self._now_fn = now if now is not None else datetime.now

    def _load(self) -> dict[str, datetime]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        out: dict[str, datetime] = {}
        for k, v in data.items():
            try:
                out[str(k)] = datetime.fromisoformat(str(v))
            except (TypeError, ValueError):
                continue
        return out

    def _save(self, data: dict[str, datetime]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: v.isoformat() for k, v in data.items()}
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def active(self, now: Optional[datetime] = None) -> dict[str, datetime]:
        when = now if now is not None else self._now_fn()
        data = self._load()
        live = {k: v for k, v in data.items() if v > when}
        if len(live) != len(data):
            self._save(live)
        return live

    def set(self, process_name: str, until: datetime, *, now: Optional[datetime] = None) -> None:
        data = self.active(now=now)
        data[process_name] = until
        self._save(data)

    def clear(self, process_name: str, *, now: Optional[datetime] = None) -> None:
        data = self.active(now=now)
        if process_name in data:
            del data[process_name]
            self._save(data)


# ---- pycaw enumeration (live; not testable in CI) -------------------------


# IAudioSessionControl::GetState enum values
_SESSION_STATE_INACTIVE = 0
_SESSION_STATE_ACTIVE = 1
_SESSION_STATE_EXPIRED = 2


def enumerate_active_sessions() -> list[_SessionSnapshot]:
    """Return a snapshot of every audio session on the default render
    endpoint that is currently in the Active state. Returns [] on any
    failure path -- pycaw missing, COM error, no devices.

    A session is "Active" when its owning process holds the audio device
    open AND is producing samples. Inactive sessions (process exists but
    not playing) and Expired sessions (process is gone) are skipped.
    The peak field is populated from the slider's master volume rather
    than a live meter -- the state check alone is the load-bearing signal
    and a real meter requires a fragile QueryInterface dance that has
    moved across pycaw versions.
    """
    if not is_available():
        return []
    try:
        from pycaw.pycaw import AudioUtilities
    except ImportError:
        return []

    try:
        sessions = AudioUtilities.GetAllSessions()
    except Exception as exc:
        log.debug("pycaw GetAllSessions failed: %s", exc)
        return []

    out: list[_SessionSnapshot] = []
    for sess in sessions:
        try:
            proc = sess.Process
            if proc is None:
                # System-sounds pseudo-session has no Process; skip.
                continue
            name = str(proc.name())
        except Exception:
            continue
        try:
            state = int(sess.State)
        except Exception:
            continue
        if state != _SESSION_STATE_ACTIVE:
            continue
        try:
            # Slider position is a useful diagnostic (a muted session at
            # state Active still counts as "audio is being rendered" --
            # the system happily renders the unmuted parts of the stream
            # while suppressing them in the mixer). Default 1.0 if it's
            # not readable.
            peak = float(sess.SimpleAudioVolume.GetMasterVolume())
        except Exception:
            peak = 1.0
        out.append(_SessionSnapshot(process_name=name, peak=peak))
    return out


# ---- QObject monitor (Qt-only) --------------------------------------------


try:
    from PyQt6.QtCore import QObject, QTimer, pyqtSignal

    class AudioSessionMonitor(QObject):
        """Polls Windows audio sessions every poll_interval_sec; emits
        meeting_audio_detected when an allowlisted app has been playing
        continuously past min_duration_sec and is not in cooldown."""

        meeting_audio_detected = pyqtSignal(object)  # MeetingAudioInfo

        def __init__(
            self,
            state_path: Path,
            *,
            allowlist: list[str],
            min_duration_sec: int = 25,
            cooldown_minutes: int = 10,
            silence_floor: float = 0.01,
            poll_interval_sec: int = 2,
            is_recording: Optional[Callable[[], bool]] = None,
            parent: Optional[QObject] = None,
        ) -> None:
            super().__init__(parent)
            self._allowlist = list(allowlist)
            self._min_duration_sec = int(min_duration_sec)
            self._cooldown_minutes = int(cooldown_minutes)
            self._silence_floor = float(silence_floor)
            self._is_recording = is_recording or (lambda: False)
            self._runs: dict[str, _RunState] = {}
            self._cooldowns = _CooldownStore(state_path)
            self._timer = QTimer(self)
            self._timer.setInterval(max(1, int(poll_interval_sec)) * 1000)
            self._timer.timeout.connect(self._tick)

        # ---- configuration accessors used by app.py reconciliation -----

        @property
        def min_duration_sec(self) -> int:
            return self._min_duration_sec

        @property
        def cooldown_minutes(self) -> int:
            return self._cooldown_minutes

        @property
        def allowlist(self) -> list[str]:
            return list(self._allowlist)

        # ---- lifecycle -----------------------------------------------------

        def start(self) -> None:
            log.info(
                "AudioSessionMonitor starting (allowlist=%d apps, "
                "min_duration=%ds, cooldown=%dm, poll=%dms)",
                len(self._allowlist), self._min_duration_sec,
                self._cooldown_minutes, self._timer.interval(),
            )
            self._tick()
            self._timer.start()

        def stop(self) -> None:
            self._timer.stop()
            self._runs.clear()
            log.info("AudioSessionMonitor stopped")

        def is_running(self) -> bool:
            return self._timer.isActive()

        # ---- the tick body, factored for testability ----------------------

        def _tick(self) -> None:
            # If a recording is already in progress, the user is in a
            # meeting we already know about; don't pile a "start recording?"
            # prompt on top.
            if self._is_recording():
                return
            try:
                snapshots = enumerate_active_sessions()
            except Exception:
                log.exception("audio session enumeration failed")
                return
            self._process_snapshots(snapshots, datetime.now())

        def _process_snapshots(
            self, snapshots: list[_SessionSnapshot], now: datetime
        ) -> None:
            """Pure-logic core: takes a snapshot list, updates runs +
            cooldowns, and emits the signal for any ready runs. Separated
            so unit tests can drive the state machine without pycaw."""
            active = _filter_allowlisted(
                snapshots, self._allowlist, self._silence_floor
            )
            _advance_runs(self._runs, active, now)
            cooldowns = self._cooldowns.active(now=now)
            ready = _runs_ready_to_fire(
                self._runs, cooldowns, self._min_duration_sec, now
            )
            for run in ready:
                info = MeetingAudioInfo(
                    process_name=run.process_name,
                    app_label=display_name_for(run.process_name),
                    first_detected_at=run.started_at,
                    sustained_seconds=(run.last_active_at - run.started_at).total_seconds(),
                )
                run.fired = True
                self._cooldowns.set(
                    run.process_name,
                    now + timedelta(minutes=self._cooldown_minutes),
                    now=now,
                )
                log.info(
                    "ad-hoc meeting detected: %s (sustained %.1fs)",
                    info.app_label, info.sustained_seconds,
                )
                self.meeting_audio_detected.emit(info)
except ImportError:  # pragma: no cover -- pure-Python test envs without PyQt6
    AudioSessionMonitor = None  # type: ignore[assignment,misc]
