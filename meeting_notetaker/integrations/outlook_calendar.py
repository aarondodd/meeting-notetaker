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
    # v0.7.2 (issue #51): Outlook's AddressEntry exposes these for
    # attendees resolved against the GAL (Global Address List) or
    # the user's Outlook contacts. External invitees (vendor email
    # addresses with no AddressEntry) get empty strings. Threaded
    # through to resolve_attendees_batch + applied via
    # update_contact_fields(fill_empty_only=True) so existing
    # Contact values are never overwritten.
    title: str = ""
    company: str = ""
    department: str = ""

    @property
    def display(self) -> str:
        return (self.name or self.email or "(unknown)").strip()


@dataclass
class CalendarAttachment:
    """An attachment carried on the calendar invite (issue #29).

    `display_name` is the friendly name from Outlook (usually the
    source filename when the inviter dropped a file in).
    `local_path` is set after `pull_attachments_to_temp` saves it
    out; None for the metadata-only read path.
    """
    display_name: str
    size: int = 0
    local_path: Optional[Path] = None


@dataclass
class MeetingInfo:
    entry_id: str
    subject: str
    start_time: datetime
    end_time: datetime
    attendees: list[CalendarAttendee] = field(default_factory=list)
    body: str = ""
    location: str = ""
    attachments: list[CalendarAttachment] = field(default_factory=list)


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
    except ImportError as exc:
        # Logged at INFO so the cause is visible in the log viewer without
        # being chatty in normal runtime. A bare ImportError here usually
        # means pywin32 isn't installed in the active venv; check the log
        # for the exception message.
        log.info("pywin32 not importable: %s", exc)
        return False
    except Exception as exc:
        log.warning("pywin32 import raised unexpectedly: %s", exc)
        return False


@dataclass
class DiagnosticResult:
    """Summary of an Outlook reachability check, suitable for a UI dialog."""

    platform_ok: bool
    pywin32_ok: bool
    pywin32_error: str = ""
    dispatch_ok: bool = False
    dispatch_error: str = ""
    namespace_ok: bool = False
    namespace_error: str = ""
    calendar_count: int = 0

    @property
    def overall_ok(self) -> bool:
        return (
            self.platform_ok
            and self.pywin32_ok
            and self.dispatch_ok
            and self.namespace_ok
        )

    def summary(self) -> str:
        if not self.platform_ok:
            return (
                "Outlook integration is Windows-only. "
                "Calendar features stay off on this platform."
            )
        if not self.pywin32_ok:
            return (
                "pywin32 (win32com.client) could not be imported.\n\n"
                f"Details: {self.pywin32_error}\n\n"
                "Fix: with the app's virtual environment active, run\n"
                "    pip install pywin32\n"
                "    python Scripts/pywin32_postinstall.py -install\n"
                "Then restart the app."
            )
        if not self.dispatch_ok:
            return (
                "pywin32 is installed but Dispatch(\"Outlook.Application\") "
                "failed.\n\n"
                f"Details: {self.dispatch_error}\n\n"
                "Common causes: Outlook is not installed, is currently signing "
                "in, is being launched as Administrator while this app is not "
                "(or vice versa), or the COM server is disabled by policy."
            )
        if not self.namespace_ok:
            return (
                "Connected to Outlook but the MAPI namespace was unreachable.\n\n"
                f"Details: {self.namespace_error}\n\n"
                "Often a transient state during Outlook startup; retry shortly."
            )
        return (
            f"Outlook reachable. {self.calendar_count} calendar item(s) "
            "visible in today's window."
        )


def diagnose() -> DiagnosticResult:
    """Run a one-shot Outlook reachability check and return structured results.

    Each step is independent so the result tells the user where the chain
    breaks: platform, pywin32 import, COM Dispatch, MAPI namespace, then a
    sanity count of today's calendar items.
    """
    result = DiagnosticResult(
        platform_ok=sys.platform.startswith("win"),
        pywin32_ok=False,
    )
    if not result.platform_ok:
        return result

    try:
        import win32com.client  # type: ignore
        result.pywin32_ok = True
    except ImportError as exc:
        result.pywin32_error = f"ImportError: {exc}"
        return result
    except Exception as exc:
        result.pywin32_error = f"{type(exc).__name__}: {exc}"
        return result

    try:
        outlook = win32com.client.Dispatch("Outlook.Application")
        result.dispatch_ok = True
    except Exception as exc:
        result.dispatch_error = f"{type(exc).__name__}: {exc}"
        return result

    try:
        ns = outlook.GetNamespace("MAPI")
        cal = ns.GetDefaultFolder(9)  # olFolderCalendar
        items = cal.Items
        items.IncludeRecurrences = True
        result.namespace_ok = True
        try:
            result.calendar_count = int(getattr(items, "Count", 0))
        except Exception:
            result.calendar_count = 0
    except Exception as exc:
        result.namespace_error = f"{type(exc).__name__}: {exc}"
    return result


# ---- Outlook fetch (live; not testable in CI) ------------------------------


def fetch_calendar_range(start: datetime, end: datetime) -> list[MeetingInfo]:
    """Return calendar items whose Start falls in [start, end].

    Returns [] silently on any failure path (Outlook not running, COM error,
    pywin32 missing). Used by both fetch_imminent_meetings (narrow +-window)
    and fetch_remaining_today (now -> end-of-day).
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

        restriction = (
            f"[Start] >= '{start.strftime('%m/%d/%Y %I:%M %p')}' AND "
            f"[Start] <= '{end.strftime('%m/%d/%Y %I:%M %p')}'"
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


def fetch_imminent_meetings(window_minutes: int = 5) -> list[MeetingInfo]:
    """Return calendar items whose start is within +- window_minutes of now.

    Returns [] silently on any failure path. The monitor polls again on the
    next tick.
    """
    now = datetime.now()
    return fetch_calendar_range(
        now - timedelta(minutes=window_minutes),
        now + timedelta(minutes=window_minutes),
    )


def fetch_remaining_today(now: Optional[datetime] = None) -> list[MeetingInfo]:
    """Return meetings that haven't yet ended through end-of-local-day.

    Used by the "Pick from Calendar..." button in New Session. Includes
    meetings already in progress (Aaron's common case: opening the app
    after a meeting has started) by widening the start of the search to
    a few hours back, then filtering to items whose end_time > now.

    Returns [] if Outlook is unreachable.
    """
    if now is None:
        now = datetime.now()
    # Look back far enough to catch any reasonable in-progress meeting
    # (Outlook restricts by Start, not End, so we need to fetch items
    # that started before now). 6h is well beyond a normal meeting; the
    # post-filter trims out anything actually done.
    lookback_start = now - timedelta(hours=6)
    end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0)
    if end_of_day < now:
        # Sanity: never happens (microsecond=0 doesn't move things backwards),
        # but guard anyway.
        end_of_day = now
    candidates = fetch_calendar_range(lookback_start, end_of_day)
    return [m for m in candidates if m.end_time > now]


def _looks_like_legacy_dn(value: str) -> bool:
    """True when a string looks like an X.500 legacyExchangeDN.

    Exchange recipients have an `Address` of the form
    '/o=ExchangeLabs/ou=Exchange Administrative Group (.../cn=Recipients/cn=...'
    -- not a useful email value to store. We detect + reject these so
    the fallback path can hunt for a real SMTP address.
    """
    if not value:
        return False
    v = value.strip().lower()
    return v.startswith("/o=") or v.startswith("/cn=")


# MAPI property tags used as a fallback when GetExchangeUser() returns
# None (cached Exchange mode without offline GAL, certain hybrid setups).
# The Recipient's PropertyAccessor surfaces these via the proptag URL.
# Reference: docs.microsoft.com/en-us/office/client-developer/outlook/
# pia/how-to-display-properties-not-shown-in-the-outlook-object-model
_PR_SMTP_ADDRESS = (
    "http://schemas.microsoft.com/mapi/proptag/0x39FE001F"
)
_PR_TITLE = "http://schemas.microsoft.com/mapi/proptag/0x3A17001F"
_PR_COMPANY_NAME = "http://schemas.microsoft.com/mapi/proptag/0x3A16001F"
_PR_DEPARTMENT_NAME = "http://schemas.microsoft.com/mapi/proptag/0x3A18001F"


def _proptag_get(accessor, proptag: str) -> str:
    try:
        value = accessor.GetProperty(proptag)
    except Exception:
        return ""
    return str(value or "").strip()


def _resolve_recipient_fields(recipient) -> dict:
    """Pull name / email / title / company / department from a Recipient.

    Three-tier resolution because Exchange COM is uncooperative:

      1. Call Recipient.Resolve() first. Unresolved recipients have an
         AddressEntry but GetExchangeUser() returns None until the
         recipient is resolved against the GAL. Resolve() is idempotent
         on already-resolved recipients.

      2. AddressEntry.GetExchangeUser() -- when it returns an object,
         it's authoritative for both the SMTP address and the
         JobTitle/CompanyName/Department triplet. AddressEntry.Address
         for an Exchange recipient is the X.500 legacyExchangeDN
         (/o=ExchangeLabs/.../cn=Recipients/cn=...), useless as an
         email value.

      3. PropertyAccessor fallback. Cached Exchange mode without an
         offline GAL (or some hybrid setups) makes GetExchangeUser()
         return None even for resolved Exchange users. The MAPI
         PropertyAccessor on the Recipient itself exposes PR_SMTP_ADDRESS
         + PR_TITLE + PR_COMPANY_NAME + PR_DEPARTMENT_NAME without
         needing the ExchangeUser object.

      4. AddressEntry direct fields as the final fallback for SMTP
         external attendees who have a direct .Address email.

    Final guard: any email value that still looks like an X.500 DN
    gets dropped (better to record no email than a useless one).

    Each step is wrapped so a flaky COM call never crashes the fetch.
    Logs each recipient's resolution path at debug level so users
    troubleshooting empty-fields can pinpoint where their environment
    diverges.
    """
    name = str(getattr(recipient, "Name", "") or "").strip()
    email = ""
    title = ""
    company = ""
    department = ""
    # Track which resolution paths fired for debug logging.
    path_taken: list[str] = []

    # Step 1: Resolve. Best-effort; if Resolve fails we still try
    # the downstream paths since AddressEntry may already be populated.
    try:
        recipient.Resolve()
    except Exception:
        path_taken.append("resolve_failed")

    try:
        ae = recipient.AddressEntry
    except Exception:
        ae = None
    if ae is None:
        path_taken.append("no_address_entry")
        log.debug(
            "recipient resolve [%s]: %s", name, ",".join(path_taken) or "none",
        )
        return {
            "name": name, "email": email,
            "title": title, "company": company, "department": department,
        }

    # Step 2: GetExchangeUser() -- authoritative when it works.
    exchange_user = None
    try:
        exchange_user = ae.GetExchangeUser()
    except Exception:
        exchange_user = None
    if exchange_user is not None:
        path_taken.append("exchange_user")
        try:
            email = str(
                getattr(exchange_user, "PrimarySmtpAddress", "") or ""
            ).strip()
        except Exception:
            email = ""
        try:
            title = str(getattr(exchange_user, "JobTitle", "") or "").strip()
        except Exception:
            title = ""
        try:
            company = str(getattr(exchange_user, "CompanyName", "") or "").strip()
        except Exception:
            company = ""
        try:
            department = str(
                getattr(exchange_user, "Department", "") or ""
            ).strip()
        except Exception:
            department = ""

    # Step 3: PropertyAccessor fallback for any field still empty.
    # Works even when GetExchangeUser() returned None.
    missing = not (email and title and company and department)
    if missing:
        accessor = None
        try:
            accessor = recipient.PropertyAccessor
        except Exception:
            accessor = None
        if accessor is not None:
            path_taken.append("property_accessor")
            if not email:
                v = _proptag_get(accessor, _PR_SMTP_ADDRESS)
                if v and not _looks_like_legacy_dn(v):
                    email = v
            if not title:
                title = _proptag_get(accessor, _PR_TITLE)
            if not company:
                company = _proptag_get(accessor, _PR_COMPANY_NAME)
            if not department:
                department = _proptag_get(accessor, _PR_DEPARTMENT_NAME)

    # Step 4: direct AddressEntry fields. Last-resort for SMTP external
    # attendees who never had an ExchangeUser to begin with.
    if not email:
        try:
            raw_email = str(getattr(ae, "Address", "") or "").strip()
        except Exception:
            raw_email = ""
        if raw_email and not _looks_like_legacy_dn(raw_email):
            email = raw_email
            path_taken.append("ae_address")
    if not title:
        try:
            title = str(getattr(ae, "JobTitle", "") or "").strip()
        except Exception:
            title = ""
    if not company:
        try:
            company = str(getattr(ae, "CompanyName", "") or "").strip()
        except Exception:
            company = ""
    if not department:
        try:
            department = str(getattr(ae, "Department", "") or "").strip()
        except Exception:
            department = ""

    # Final guard: never let an X.500 DN escape as an email value.
    if _looks_like_legacy_dn(email):
        email = ""

    log.debug(
        "recipient resolve [%s]: paths=%s email=%r title=%r company=%r dept=%r",
        name, ",".join(path_taken) or "none",
        email or "", title or "", company or "", department or "",
    )
    return {
        "name": name, "email": email,
        "title": title, "company": company, "department": department,
    }


def _item_to_info(item) -> MeetingInfo:
    attendees: list[CalendarAttendee] = []
    try:
        for r in item.Recipients:
            fields = _resolve_recipient_fields(r)
            attendees.append(CalendarAttendee(**fields))
    except Exception:
        log.debug("recipients parse failed", exc_info=True)

    body = sanitize_body(str(getattr(item, "Body", "") or ""))

    # Metadata-only walk of attachments. We don't save them to disk
    # here -- that's expensive (file copies) and only useful when the
    # caller actually picks this meeting. pull_attachments_to_temp
    # handles the save step at session-creation time.
    attachments: list[CalendarAttachment] = []
    try:
        for a in item.Attachments:
            # Type 1 = olByValue (a file we can SaveAsFile).
            # Other types (olByReference, olEmbeddeditem, olOLE) are
            # not directly extractable, so we skip them here.
            atype = int(getattr(a, "Type", 0) or 0)
            if atype != 1:
                continue
            attachments.append(
                CalendarAttachment(
                    display_name=str(
                        getattr(a, "DisplayName", "") or
                        getattr(a, "FileName", "") or
                        "attachment"
                    ).strip(),
                    size=int(getattr(a, "Size", 0) or 0),
                )
            )
    except Exception:
        log.debug("attachments parse failed", exc_info=True)

    return MeetingInfo(
        entry_id=str(item.EntryID),
        subject=str(item.Subject or "(no subject)"),
        start_time=_pywintype_to_datetime(item.Start),
        end_time=_pywintype_to_datetime(item.End),
        attendees=attendees,
        body=body,
        location=str(getattr(item, "Location", "") or "").strip(),
        attachments=attachments,
    )


def pull_attachments_to_temp(entry_id: str) -> list[Path]:
    """Save every file-type attachment on the given MeetingItem to
    a temp directory; return the resulting paths.

    Used after the user picks a calendar entry to create a session
    from -- the caller (`_seed_live_notes_from_meeting`) hands these
    paths to AttachmentsStore.add_file. Files in the temp dir are
    NOT cleaned up here; AttachmentsStore.add_file copies them into
    the session dir and the tempdir's contents become orphaned but
    harmless (they'll get swept on next boot).

    Returns [] on any failure (Outlook unreachable, EntryID
    missing, no file-type attachments).
    """
    if not is_available():
        return []
    try:
        import tempfile
        import win32com.client
    except ImportError:
        return []
    out: list[Path] = []
    try:
        outlook = win32com.client.Dispatch("Outlook.Application")
        namespace = outlook.GetNamespace("MAPI")
        item = namespace.GetItemFromID(entry_id)
        if item is None:
            return []
        temp_dir = Path(tempfile.mkdtemp(prefix="mtn-cal-"))
        for a in item.Attachments:
            try:
                atype = int(getattr(a, "Type", 0) or 0)
            except Exception:
                atype = 0
            if atype != 1:
                continue
            name = str(
                getattr(a, "FileName", "") or
                getattr(a, "DisplayName", "") or
                "attachment"
            ).strip()
            if not name:
                continue
            safe = "".join(c for c in name if c not in "\\/:*?\"<>|\r\n\t").strip()
            if not safe:
                continue
            dst = temp_dir / safe
            try:
                a.SaveAsFile(str(dst))
            except Exception:
                log.exception("Outlook SaveAsFile failed for %s", name)
                continue
            if dst.exists():
                out.append(dst)
    except Exception:
        log.exception("pull_attachments_to_temp failed for %s", entry_id)
    return out


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
    from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

    class _OutlookFetchWorker(QThread):
        """Run the Outlook COM call on a background thread (issue #48).

        Outlook calendar resolution goes through win32com's lazy
        attribute lookup, which is synchronous + can take 700-1000 ms
        per tick. Running it on the main thread froze the UI for
        ~900 ms every 60 s (the QTimer cadence). The worker calls
        ``fetch_imminent_meetings`` then emits ``done(meetings)`` on
        the main thread for the dedup + signal-emit step.

        COM apartment: ``pythoncom.CoInitialize`` must be called once
        per thread that touches win32com. The worker initializes on
        entry + uninitializes in a finally so a raise during the COM
        call doesn't leak the apartment.
        """

        done = pyqtSignal(object)  # list[MeetingInfo]

        def __init__(self, window_minutes: int) -> None:
            super().__init__(None)
            self._window_minutes = window_minutes

        def run(self) -> None:
            # Local import keeps the pure-Python test env from needing
            # pywin32 just to import the module.
            try:
                import pythoncom  # type: ignore[import-not-found]
            except ImportError:
                pythoncom = None
            if pythoncom is not None:
                try:
                    pythoncom.CoInitialize()
                except Exception:
                    log.exception("OutlookFetchWorker: CoInitialize failed")
                    self.done.emit([])
                    return
            try:
                try:
                    meetings = fetch_imminent_meetings(self._window_minutes)
                except Exception:
                    log.exception("calendar poll failed (worker)")
                    meetings = []
                self.done.emit(meetings)
            finally:
                if pythoncom is not None:
                    try:
                        pythoncom.CoUninitialize()
                    except Exception:
                        log.exception(
                            "OutlookFetchWorker: CoUninitialize failed"
                        )

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
            # In-flight worker (issue #48). At most one tick runs at a
            # time; if the QTimer fires while a prior tick is still
            # crunching, the new tick is skipped rather than queued
            # behind it.
            self._worker: Optional[_OutlookFetchWorker] = None

        def start(self) -> None:
            log.info(
                "OutlookCalendarMonitor starting (window=%d min, poll=%dms)",
                self._window_minutes, self._timer.interval(),
            )
            # First tick still goes through the worker now (issue #48
            # subsumes #43); QTimer.singleShot(0) just ensures the
            # window paints before we even dispatch the worker.
            QTimer.singleShot(0, self._tick)
            self._timer.start()

        def stop(self) -> None:
            self._timer.stop()
            # Don't wait for the in-flight worker -- it's a one-shot
            # COM call and the finished signal handler retires it
            # via _safe_cleanup. wait() at stop would block the
            # caller (potentially the app's aboutToQuit handler).
            log.info("OutlookCalendarMonitor stopped")

        def is_running(self) -> bool:
            return self._timer.isActive()

        @property
        def window_minutes(self) -> int:
            return self._window_minutes

        def _tick(self) -> None:
            if self._worker is not None and self._worker.isRunning():
                # Prior poll still resolving COM -- skip this tick
                # rather than overlapping (Outlook COM access from
                # two threads is asking for trouble).
                log.debug("OutlookCalendarMonitor: prior tick still running, skipping")
                return
            worker = _OutlookFetchWorker(self._window_minutes)
            worker.done.connect(self._on_fetch_done)
            worker.finished.connect(lambda w=worker: self._retire_worker(w))
            self._worker = worker
            worker.start()

        def _on_fetch_done(self, meetings) -> None:
            """Slot for worker.done. Runs on the main thread; safe to
            touch _dedup + emit Qt signals."""
            for m in meetings:
                if self._dedup.is_seen(m.entry_id):
                    continue
                self._dedup.mark_seen(m.entry_id)
                self.meeting_imminent.emit(m)

        def _retire_worker(self, worker) -> None:
            try:
                worker.wait()
            except Exception:
                log.exception("Outlook worker wait failed")
            try:
                worker.deleteLater()
            except Exception:
                log.exception("Outlook worker deleteLater failed")
            if self._worker is worker:
                self._worker = None
except ImportError:  # pragma: no cover -- pure-Python test envs without PyQt6
    OutlookCalendarMonitor = None  # type: ignore[assignment,misc]
