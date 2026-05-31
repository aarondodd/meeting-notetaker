"""Map OWA's internal JSON shape onto a MeetingInfo-shaped dataclass.

This is the probe's "is the OWA payload sufficient?" answer. The
dataclass deliberately mirrors meeting_notetaker.integrations.
outlook_calendar.MeetingInfo so a port to the prod tree is mostly
import-renames.

Kept pure-Python so tests can exercise it without Qt, PortAudio, or
network access.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


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
    """OWA emits ``{"dateTime": "2026-05-31T17:00:00.0000000",
    "timeZone": "Pacific/Honolulu"}``. The string is wall-clock time
    in the named zone. We return a UTC-anchored ``datetime`` + the
    original tz name (for the caller to render local-time labels).

    Returns (None, "") on any malformed input -- a missing start_time
    is the caller's problem, not ours."""
    if not isinstance(raw, dict):
        return None, ""
    dt_str = raw.get("dateTime") or ""
    tz_name = raw.get("timeZone") or "UTC"
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
    """Top level: ``{"value": [event, ...]}``. Empty / missing
    ``value`` -> empty list rather than raising; the probe view
    treats an empty calendar as a valid state."""
    events = []
    if not isinstance(body, dict):
        return events
    raw_events = body.get("value")
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
    start_utc, start_tz = _parse_owa_datetime(ev.get("start"))
    end_utc, _ = _parse_owa_datetime(ev.get("end"))

    organizer = ev.get("organizer") or {}
    organizer_email_obj = (organizer.get("emailAddress") or {})

    body_obj = ev.get("body") or {}
    body_content = body_obj.get("content") or ""
    body_type = (body_obj.get("contentType") or "").lower()
    if body_type == "html":
        body_html, body_text = body_content, _strip_html(body_content)
    else:
        body_html, body_text = "", body_content

    online = ev.get("onlineMeeting") or {}
    online_url = ev.get("onlineMeetingUrl") or online.get("joinUrl") or ""

    return ProbeMeeting(
        event_id=str(ev.get("id") or ""),
        ical_uid=str(ev.get("iCalUId") or ""),
        subject=str(ev.get("subject") or ""),
        start_utc=start_utc,
        end_utc=end_utc,
        start_tz=start_tz,
        location=((ev.get("location") or {}).get("displayName") or ""),
        organizer_name=organizer_email_obj.get("name") or "",
        organizer_email=organizer_email_obj.get("address") or "",
        body_html=body_html,
        body_text=body_text,
        is_online_meeting=bool(ev.get("isOnlineMeeting", False)),
        online_meeting_url=online_url,
        has_attachments=bool(ev.get("hasAttachments", False)),
        web_link=str(ev.get("webLink") or ""),
        attendees=[_parse_attendee(a) for a in (ev.get("attendees") or [])],
    )


def _parse_attendee(att: Any) -> ProbeAttendee:
    if not isinstance(att, dict):
        return ProbeAttendee()
    ea = att.get("emailAddress") or {}
    status = (att.get("status") or {}).get("response") or ""
    return ProbeAttendee(
        name=str(ea.get("name") or ""),
        email=str(ea.get("address") or ""),
        attendee_type=str(att.get("type") or ""),
        response_status=str(status),
    )


def parse_people_lookup(body: dict) -> list[dict]:
    """``/people`` returns ``{"value": [person, ...]}`` with title /
    company / department for tenant-resolved entries. We return a flat
    list of dicts -- the caller decides whether to fold them onto a
    specific ProbeAttendee."""
    out: list[dict] = []
    if not isinstance(body, dict):
        return out
    for entry in (body.get("value") or []):
        if not isinstance(entry, dict):
            continue
        scored = entry.get("scoredEmailAddresses") or []
        primary_email = (scored[0].get("address") if scored else "") or ""
        out.append({
            "display_name": entry.get("displayName") or "",
            "given_name": entry.get("givenName") or "",
            "surname": entry.get("surname") or "",
            "job_title": entry.get("jobTitle") or "",
            "company_name": entry.get("companyName") or "",
            "department": entry.get("department") or "",
            "office_location": entry.get("officeLocation") or "",
            "email": primary_email,
            "person_type": (entry.get("personType") or {}).get("subclass") or "",
        })
    return out


def parse_attachments_list(body: dict) -> list[ProbeAttachment]:
    out: list[ProbeAttachment] = []
    if not isinstance(body, dict):
        return out
    for att in (body.get("value") or []):
        if not isinstance(att, dict):
            continue
        out.append(ProbeAttachment(
            attachment_id=str(att.get("id") or ""),
            name=str(att.get("name") or ""),
            content_type=str(att.get("contentType") or ""),
            size=int(att.get("size") or 0),
            is_inline=bool(att.get("isInline", False)),
        ))
    return out
