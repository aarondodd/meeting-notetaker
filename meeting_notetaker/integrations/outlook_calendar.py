"""Outlook calendar integration via pywin32 COM.

Reads the local Outlook profile through MAPI -- no network calls, no
admin rights, no third-party API. The COM Dispatch is the same primitive
that drives every other Office automation script on a corporate Windows
laptop; IT virtually never blocks it because too much existing tooling
depends on it.

Behavior:
- Poll every ~60s (driven by the parent app's QTimer).
- For each meeting whose Start falls inside the window (default +-5 min
  of now), emit MeetingInfo so the app can post a tray notification.
- Deduplicate by Outlook EntryID per calendar day. A daily expiry means
  a recurring meeting re-fires on its next-day instance; missed days do
  not re-notify.
- All Outlook failure modes (Outlook not running, COM error, restricted
  enumerator empty) are non-fatal: log and return [].
- No auto-start of recording. The signal triggers the UI; the user
  explicitly clicks "Create Session" on the toast.

The QObject + signal layer is in OutlookCalendarMonitor at the bottom.
Pure-function pieces (sanitize_body, _DedupStore) are testable on Linux
without pywin32 or Outlook installed.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


log = logging.getLogger(__name__)


@dataclass
class CalendarAttendee:
    name: str = ""
    email: str = ""

    @property
    def display(self) -> str:
        return (self.name or self.email or "(unknown)").strip()


@dataclass
class MeetingInfo:
    entry_id: str
    subject: str
    start_time: datetime
    end_time: datetime
    attendees: list[CalendarAttendee] = field(default_factory=list)
    body: str = ""
    location: str = ""


# ---- pure helpers (testable without pywin32) -------------------------------


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_NL_RE = re.compile(r"\n{3,}")


def sanitize_body(body: str, *, max_chars: int = 4000) -> str:
    """Strip HTML tags + normalize whitespace + clip to max_chars.

    Outlook bodies are usually plain text but can be HTML for invites that
    came from a non-Outlook origin (Google Calendar, web meeting tools).
    The strip is intentionally lossy -- the goal is "readable agenda for a
    human glance", not faithful reproduction.
    """
    if not body:
        return ""
    out = _HTML_TAG_RE.sub(" ", body)
    out = out.replace("\r\n", "\n").replace("\r", "\n")
    out = _WS_RE.sub(" ", out)
    out = "\n".join(line.rstrip() for line in out.split("\n"))
    out = _NL_RE.sub("\n\n", out)
    out = out.strip()
    if len(out) > max_chars:
        out = out[: max_chars - 3].rstrip() + "..."
    return out


def is_available() -> bool:
    """True if pywin32 is importable on this host."""
    if not sys.platform.startswith("win"):
        return False
    try:
        import win32com.client  # noqa: F401 -- import-only check
        return True
    except ImportError:
        return False


# ---- Outlook fetch (live; not testable in CI) ------------------------------


def fetch_imminent_meetings(window_minutes: int = 5) -> list[MeetingInfo]:
    """Return calendar items whose start is within +- window_minutes of now.

    Returns [] silently on any failure path (Outlook not running, COM error,
    pywin32 missing). The caller polls again on the next tick.
    """
    if not is_available():
        return []
    try:
        import win32com.client  # type: ignore
    except ImportError:
        return []

    try:
        outlook = win32com.client.Dispatch("Outlook.Application")
    except Exception as exc:
        log.debug("Outlook Dispatch failed (Outlook may not be running): %s", exc)
        return []

    try:
        ns = outlook.GetNamespace("MAPI")
        cal = ns.GetDefaultFolder(9)  # olFolderCalendar
        items = cal.Items
        items.IncludeRecurrences = True
        items.Sort("[Start]")

        now = datetime.now()
        lo = now - timedelta(minutes=window_minutes)
        hi = now + timedelta(minutes=window_minutes)
        # Outlook DASL restriction needs a US-locale-ish timestamp string.
        restriction = (
            f"[Start] >= '{lo.strftime('%m/%d/%Y %I:%M %p')}' AND "
            f"[Start] <= '{hi.strftime('%m/%d/%Y %I:%M %p')}'"
        )
        restricted = items.Restrict(restriction)

        out: list[MeetingInfo] = []
        for item in restricted:
            try:
                out.append(_item_to_info(item))
            except Exception:
                log.exception("failed to parse calendar item")
        return out
    except Exception:
        log.exception("Outlook calendar fetch failed")
        return []


def _item_to_info(item) -> MeetingInfo:
    attendees: list[CalendarAttendee] = []
    try:
        for r in item.Recipients:
            attendees.append(
                CalendarAttendee(
                    name=str(getattr(r, "Name", "") or "").strip(),
                    email=str(getattr(r, "Address", "") or "").strip(),
                )
            )
    except Exception:
        log.debug("recipients parse failed", exc_info=True)

    body = sanitize_body(str(getattr(item, "Body", "") or ""))

    return MeetingInfo(
        entry_id=str(item.EntryID),
        subject=str(item.Subject or "(no subject)"),
        start_time=_pywintype_to_datetime(item.Start),
        end_time=_pywintype_to_datetime(item.End),
        attendees=attendees,
        body=body,
        location=str(getattr(item, "Location", "") or "").strip(),
    )


def _pywintype_to_datetime(pwt) -> datetime:
    # pywintypes.datetime is datetime-compatible; this normalizes to naive
    # local datetime for the UI (we display "starts at 14:30 local").
    try:
        return datetime(
            pwt.year, pwt.month, pwt.day, pwt.hour, pwt.minute, pwt.second
        )
    except Exception:
        return datetime.fromisoformat(str(pwt)[:19])


# ---- dedup store (per-day) -------------------------------------------------


class _DedupStore:
    """Tracks which Outlook EntryIDs we've already surfaced today.

    Persisted to JSON so an app restart in the middle of the day doesn't
    re-fire today's already-notified meetings. The map is keyed by date
    string; only TODAY's entries are kept on each write, so recurring
    meetings re-fire on their next-day instance and the file stays small.
    """

    def __init__(self, path: Path, *, now: Optional[datetime] = None) -> None:
        self.path = Path(path)
        self._now_fn = (lambda: now) if now is not None else datetime.now

    def _today_key(self) -> str:
        return self._now_fn().strftime("%Y-%m-%d")

    def _load(self) -> dict[str, list[str]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): list(v) for k, v in data.items() if isinstance(v, list)}
            return {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict[str, list[str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def is_seen(self, entry_id: str) -> bool:
        return entry_id in self._load().get(self._today_key(), [])

    def mark_seen(self, entry_id: str) -> None:
        today = self._today_key()
        # Drop stale dates so the file doesn't accumulate across days.
        data: dict[str, list[str]] = {today: self._load().get(today, [])}
        if entry_id not in data[today]:
            data[today].append(entry_id)
        self._save(data)


# ---- QObject monitor (Qt-only) ---------------------------------------------


try:
    from PyQt6.QtCore import QObject, QTimer, pyqtSignal

    class OutlookCalendarMonitor(QObject):
        """Polls Outlook every poll_interval_sec; emits meeting_imminent per new meeting."""

        meeting_imminent = pyqtSignal(object)  # MeetingInfo

        def __init__(
            self,
            state_path: Path,
            *,
            window_minutes: int = 5,
            poll_interval_sec: int = 60,
            parent: Optional[QObject] = None,
        ) -> None:
            super().__init__(parent)
            self._dedup = _DedupStore(state_path)
            self._window_minutes = window_minutes
            self._timer = QTimer(self)
            self._timer.setInterval(max(5, int(poll_interval_sec)) * 1000)
            self._timer.timeout.connect(self._tick)

        def start(self) -> None:
            log.info(
                "OutlookCalendarMonitor starting (window=%d min, poll=%dms)",
                self._window_minutes, self._timer.interval(),
            )
            self._tick()
            self._timer.start()

        def stop(self) -> None:
            self._timer.stop()
            log.info("OutlookCalendarMonitor stopped")

        def is_running(self) -> bool:
            return self._timer.isActive()

        @property
        def window_minutes(self) -> int:
            return self._window_minutes

        def _tick(self) -> None:
            try:
                meetings = fetch_imminent_meetings(self._window_minutes)
            except Exception:
                log.exception("calendar poll failed")
                return
            for m in meetings:
                if self._dedup.is_seen(m.entry_id):
                    continue
                self._dedup.mark_seen(m.entry_id)
                self.meeting_imminent.emit(m)
except ImportError:  # pragma: no cover -- pure-Python test envs without PyQt6
    OutlookCalendarMonitor = None  # type: ignore[assignment,misc]
