"""Map OWA's calendar JSON shape onto a MeetingInfo-shaped dataclass.

OWA exposes two compatible response shapes:

1. **Outlook REST v2.0** (PascalCase: Subject/Start/Attendees).
   This is what outlook.office365.com/api/v2.0/me/calendarview
   returns and is the path the 2026-05-31 probe validated against
   Aaron's tenant.

2. **Microsoft Graph** (camelCase: subject/start/attendees).
   graph.microsoft.com/v1.0/me/calendarview returns this shape but
   requires a token audience'd for graph.microsoft.com -- on the
   tenants tested, OWA doesn't mint Graph-audience tokens by
   default unless the user navigates to a Graph-using feature.

The parser sniffs the casing on each event and dispatches. The
dataclass output is identical regardless of source shape so
downstream code never has to care.

Kept pure-Python so tests can exercise it without Qt, PortAudio, or
network access.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def _pick(d: dict, *keys: str) -> Any:
    """Return the first present value from d for any of the given
    keys. Used to handle the camelCase vs PascalCase split: pass both
    spellings, take whichever the response uses."""
    for k in keys:
        if k in d:
            return d[k]
    return None


@dataclass
class ProbeAttendee:
    name: str = ""
    email: str = ""
    title: str = ""
    company: str = ""
    department: str = ""
    response_status: str = ""
    attendee_type: str = ""  # required | optional | resource


@dataclass
class ProbeAttachment:
    name: str = ""
    content_type: str = ""
    size: int = 0
    is_inline: bool = False
    attachment_id: str = ""


@dataclass
class ProbeMeeting:
    event_id: str = ""
    ical_uid: str = ""
    subject: str = ""
    start_utc: Optional[datetime] = None
    end_utc: Optional[datetime] = None
    start_tz: str = ""
    location: str = ""
    organizer_name: str = ""
    organizer_email: str = ""
    body_html: str = ""
    body_text: str = ""
    is_online_meeting: bool = False
    online_meeting_url: str = ""
    has_attachments: bool = False
    web_link: str = ""
    attendees: list[ProbeAttendee] = field(default_factory=list)


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(value: str) -> str:
    if not value:
        return ""
    txt = _HTML_TAG_RE.sub(" ", value)
    return re.sub(r"\s+", " ", txt).strip()


def _parse_owa_datetime(raw: Any) -> tuple[Optional[datetime], str]:
    """OWA emits ``{"DateTime": "2026-05-31T17:00:00.0000000",
    "TimeZone": "Pacific/Honolulu"}`` (Outlook REST v2.0) or the
    camelCase Graph equivalent. The string is wall-clock time in
    the named zone. We return a UTC-anchored ``datetime`` + the
    original tz name (for the caller to render local-time labels).

    Returns (None, "") on any malformed input -- a missing start_time
    is the caller's problem, not ours."""
    if not isinstance(raw, dict):
        return None, ""
    dt_str = _pick(raw, "DateTime", "dateTime") or ""
    tz_name = _pick(raw, "TimeZone", "timeZone") or "UTC"
    if not dt_str:
        return None, tz_name
    # OWA's fractional seconds run to 7 digits; Python only takes 6.
    cleaned = dt_str.split(".")[0]
    try:
        naive = datetime.fromisoformat(cleaned)
    except ValueError:
        return None, tz_name
    # Best-effort tz attachment. Fallback to UTC if zoneinfo doesn't
    # know the zone (Windows tz names like "Pacific Standard Time"
    # would need a separate map; OWA usually emits IANA names for
    # personal calendars but Windows for shared mailboxes).
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            aware = naive.replace(tzinfo=ZoneInfo(tz_name))
        except ZoneInfoNotFoundError:
            aware = naive.replace(tzinfo=timezone.utc)
    except ImportError:  # pragma: no cover - py<3.9 not supported
        aware = naive.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc), tz_name


def parse_calendarview(body: dict) -> list[ProbeMeeting]:
    """Top level: ``{"value": [event, ...]}`` (Graph + Outlook REST)
    or rarely ``{"Value": [...]}``. Empty / missing -> empty list
    rather than raising; the probe view treats an empty calendar
    as a valid state."""
    events = []
    if not isinstance(body, dict):
        return events
    raw_events = _pick(body, "value", "Value")
    if not isinstance(raw_events, list):
        return events
    for ev in raw_events:
        meeting = _parse_event(ev)
        if meeting is not None:
            events.append(meeting)
    return events


def _parse_event(ev: Any) -> Optional[ProbeMeeting]:
    if not isinstance(ev, dict):
        return None
    start_utc, start_tz = _parse_owa_datetime(_pick(ev, "Start", "start"))
    end_utc, _ = _parse_owa_datetime(_pick(ev, "End", "end"))

    organizer = _pick(ev, "Organizer", "organizer") or {}
    organizer_email_obj = _pick(organizer, "EmailAddress", "emailAddress") or {}

    body_obj = _pick(ev, "Body", "body") or {}
    body_content = _pick(body_obj, "Content", "content") or ""
    body_type = (_pick(body_obj, "ContentType", "contentType") or "").lower()
    if body_type == "html":
        body_html, body_text = body_content, _strip_html(body_content)
    else:
        body_html, body_text = "", body_content

    online = _pick(ev, "OnlineMeeting", "onlineMeeting") or {}
    online_url = (
        _pick(ev, "OnlineMeetingUrl", "onlineMeetingUrl")
        or _pick(online, "JoinUrl", "joinUrl")
        or ""
    )

    location = _pick(ev, "Location", "location") or {}
    location_name = _pick(location, "DisplayName", "displayName") or ""

    return ProbeMeeting(
        event_id=str(_pick(ev, "Id", "id") or ""),
        ical_uid=str(_pick(ev, "iCalUId", "iCalUid") or ""),
        subject=str(_pick(ev, "Subject", "subject") or ""),
        start_utc=start_utc,
        end_utc=end_utc,
        start_tz=start_tz,
        location=location_name,
        organizer_name=_pick(organizer_email_obj, "Name", "name") or "",
        organizer_email=_pick(organizer_email_obj, "Address", "address") or "",
        body_html=body_html,
        body_text=body_text,
        is_online_meeting=bool(_pick(ev, "IsOnlineMeeting", "isOnlineMeeting") or False),
        online_meeting_url=online_url,
        has_attachments=bool(_pick(ev, "HasAttachments", "hasAttachments") or False),
        web_link=str(_pick(ev, "WebLink", "webLink") or ""),
        attendees=[_parse_attendee(a) for a in (_pick(ev, "Attendees", "attendees") or [])],
    )


def _parse_attendee(att: Any) -> ProbeAttendee:
    if not isinstance(att, dict):
        return ProbeAttendee()
    ea = _pick(att, "EmailAddress", "emailAddress") or {}
    status_obj = _pick(att, "Status", "status") or {}
    status = _pick(status_obj, "Response", "response") or ""
    return ProbeAttendee(
        name=str(_pick(ea, "Name", "name") or ""),
        email=str(_pick(ea, "Address", "address") or ""),
        attendee_type=str(_pick(att, "Type", "type") or ""),
        response_status=str(status),
    )


def parse_people_lookup(body: dict) -> list[dict]:
    """``/people`` returns ``{"value": [person, ...]}`` (or ``Value``
    in PascalCase) with title / company / department for tenant-
    resolved entries. We return a flat list of dicts -- the caller
    decides whether to fold them onto a specific ProbeAttendee."""
    out: list[dict] = []
    if not isinstance(body, dict):
        return out
    entries = _pick(body, "value", "Value") or []
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        scored = _pick(entry, "ScoredEmailAddresses", "scoredEmailAddresses") or []
        primary_email = ""
        if scored and isinstance(scored[0], dict):
            primary_email = _pick(scored[0], "Address", "address") or ""
        person_type = _pick(entry, "PersonType", "personType") or {}
        out.append({
            "display_name": _pick(entry, "DisplayName", "displayName") or "",
            "given_name": _pick(entry, "GivenName", "givenName") or "",
            "surname": _pick(entry, "Surname", "surname") or "",
            "job_title": _pick(entry, "JobTitle", "jobTitle") or "",
            "company_name": _pick(entry, "CompanyName", "companyName") or "",
            "department": _pick(entry, "Department", "department") or "",
            "office_location": _pick(entry, "OfficeLocation", "officeLocation") or "",
            "email": primary_email,
            "person_type": _pick(person_type, "Subclass", "subclass") or "",
        })
    return out


def parse_attachments_list(body: dict) -> list[ProbeAttachment]:
    out: list[ProbeAttachment] = []
    if not isinstance(body, dict):
        return out
    entries = _pick(body, "value", "Value") or []
    if not isinstance(entries, list):
        return out
    for att in entries:
        if not isinstance(att, dict):
            continue
        out.append(ProbeAttachment(
            attachment_id=str(_pick(att, "Id", "id") or ""),
            name=str(_pick(att, "Name", "name") or ""),
            content_type=str(_pick(att, "ContentType", "contentType") or ""),
            size=int(_pick(att, "Size", "size") or 0),
            is_inline=bool(_pick(att, "IsInline", "isInline") or False),
        ))
    return out
