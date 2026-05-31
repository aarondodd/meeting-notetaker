"""Assertions against the captured OWA payload shape.

These tests pin the probe parser's expectations to the *redacted*
fixtures in tests/fixtures/. When OWA's internal API rotates (Aaron
hits a "the probe stopped returning events" report), the diff
between a fresh capture and a fixture surfaces the breaking change.

Run from the project root::

    python -m pytest experiments/owa-calendar-probe/tests/ -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Allow `python -m pytest experiments/owa-calendar-probe/tests/` without
# packaging the probe.
_PROBE_ROOT = Path(__file__).resolve().parent.parent
if str(_PROBE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROBE_ROOT))

from relay import capture, parser  # noqa: E402


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---- calendarview ---------------------------------------------------------


def test_calendarview_returns_two_events():
    payload = _load("calendarview-redacted.json")
    meetings = parser.parse_calendarview(payload["body"])
    assert len(meetings) == 2


def test_calendarview_first_event_basics():
    payload = _load("calendarview-redacted.json")
    meetings = parser.parse_calendarview(payload["body"])
    m = meetings[0]
    assert m.subject == "Team standup"
    assert m.event_id.startswith("AAMkAGI2")
    assert m.location == "Teams"
    assert m.is_online_meeting is True
    assert m.online_meeting_url.startswith("https://teams.microsoft.com/")
    assert m.has_attachments is False
    assert m.start_utc is not None
    assert m.end_utc is not None
    assert m.start_utc < m.end_utc


def test_calendarview_attendees_preserved():
    payload = _load("calendarview-redacted.json")
    meetings = parser.parse_calendarview(payload["body"])
    standup = meetings[0]
    assert len(standup.attendees) == 2
    # Names survive redaction; only email local parts get scrubbed.
    names = {a.name for a in standup.attendees}
    assert "Sample User" in names


def test_calendarview_has_attachments_flag_propagates():
    payload = _load("calendarview-redacted.json")
    meetings = parser.parse_calendarview(payload["body"])
    arch = meetings[1]
    assert arch.subject == "Architecture sync"
    assert arch.has_attachments is True


def test_calendarview_body_html_is_stripped_for_text():
    payload = _load("calendarview-redacted.json")
    meetings = parser.parse_calendarview(payload["body"])
    standup = meetings[0]
    assert standup.body_html == "<p>Standup agenda</p>"
    assert standup.body_text == "Standup agenda"


def test_calendarview_organizer_split_into_name_and_email():
    payload = _load("calendarview-redacted.json")
    meetings = parser.parse_calendarview(payload["body"])
    m = meetings[0]
    assert m.organizer_name == "Sample Organizer"
    assert "@" in m.organizer_email


def test_calendarview_empty_body_returns_empty_list():
    assert parser.parse_calendarview({}) == []
    assert parser.parse_calendarview({"value": []}) == []
    # Non-dict input must not crash.
    assert parser.parse_calendarview([]) == []  # type: ignore[arg-type]


# ---- people.lookup --------------------------------------------------------


def test_people_lookup_extracts_enrichment_fields():
    payload = _load("people-lookup-redacted.json")
    people = parser.parse_people_lookup(payload["body"])
    assert len(people) == 1
    p = people[0]
    assert p["display_name"] == "Sample Person"
    assert p["job_title"] == "Principal Engineer"
    assert p["company_name"] == "Acme Corp"
    assert p["department"] == "Platform"
    assert p["person_type"] == "OrganizationUser"
    assert "@" in p["email"]


def test_people_lookup_missing_body_returns_empty():
    assert parser.parse_people_lookup({}) == []
    assert parser.parse_people_lookup({"value": []}) == []


# ---- attachments.list -----------------------------------------------------


def test_attachments_list_extracts_metadata():
    payload = _load("attachments-list-redacted.json")
    atts = parser.parse_attachments_list(payload["body"])
    assert len(atts) == 2
    pdf = atts[0]
    assert pdf.name == "pre-read.pdf"
    assert pdf.content_type == "application/pdf"
    assert pdf.size == 142331
    assert pdf.is_inline is False
    assert pdf.attachment_id == "attid-fixture-1"


def test_attachments_list_handles_missing_value_key():
    assert parser.parse_attachments_list({}) == []


# ---- Outlook REST v2.0 PascalCase shape (real-shape from probe) -----------
#
# These test against the JSON shape outlook.office365.com/api/v2.0
# actually returns. The 2026-05-31 probe validated that this is the
# path that works on the cloud.microsoft Outlook host without an
# Entra app registration. The Graph-style camelCase fixture above is
# kept because the prod merge will eventually try Graph too (or as a
# fallback) and the parser must accept both shapes.


def test_outlook_rest_returns_two_events():
    payload = _load("calendarview-outlook-rest-redacted.json")
    meetings = parser.parse_calendarview(payload)
    assert len(meetings) == 2


def test_outlook_rest_pascal_case_event_basics():
    payload = _load("calendarview-outlook-rest-redacted.json")
    m = parser.parse_calendarview(payload)[0]
    assert m.subject == "Test 1"
    assert m.event_id.startswith("AAMkADlk")
    assert m.location == "Microsoft Teams Meeting"
    assert m.is_online_meeting is True
    assert m.online_meeting_url.startswith("https://teams.microsoft.com/l/meetup-join/")
    assert m.has_attachments is False
    assert m.start_utc is not None
    assert m.end_utc is not None
    assert m.start_utc < m.end_utc
    # Web link round-trip.
    assert m.web_link.startswith("https://outlook.office365.com/")


def test_outlook_rest_organizer_split():
    payload = _load("calendarview-outlook-rest-redacted.json")
    m = parser.parse_calendarview(payload)[0]
    assert m.organizer_name == "Sample User"
    assert "@" in m.organizer_email
    # Redaction stripped the local part but the domain survives so
    # downstream tenant routing can still infer it.
    assert m.organizer_email.endswith("@example-user.com")


def test_outlook_rest_attendees_with_external_invitee():
    """Cross-tenant attendee case: the meeting's owner is on tenant A
    (example-user.com); the invited attendee is on tenant B
    (example-org.com). The parser must preserve both name and email
    even when redaction has stripped local parts."""
    payload = _load("calendarview-outlook-rest-redacted.json")
    m = parser.parse_calendarview(payload)[0]
    assert len(m.attendees) == 1
    att = m.attendees[0]
    assert att.name == "Sample User"
    assert att.email.endswith("@example-org.com")
    assert att.attendee_type == "Required"


def test_outlook_rest_body_html_extracted_and_stripped():
    payload = _load("calendarview-outlook-rest-redacted.json")
    m = parser.parse_calendarview(payload)[0]
    assert "<html>" in m.body_html
    # body_text must be HTML-free.
    assert "<" not in m.body_text
    assert "Microsoft Teams meeting" in m.body_text


def test_outlook_rest_handles_empty_attendees_and_location():
    payload = _load("calendarview-outlook-rest-redacted.json")
    m = parser.parse_calendarview(payload)[1]
    assert m.subject == "Test 2"
    assert m.attendees == []
    assert m.location == ""
    assert m.is_online_meeting is False


def test_people_outlook_rest_returns_two_entries():
    payload = _load("people-outlook-rest-redacted.json")
    people = parser.parse_people_lookup(payload)
    assert len(people) == 2


def test_people_outlook_rest_organization_user_enrichment():
    """Tenant-resolved person -- PersonType.Subclass = OrganizationUser.
    On a personal-grade tenant, JobTitle / Company / Department come
    back null because no admin populates those fields. Enterprise
    tenants typically populate them via Active Directory sync. The
    parser preserves nulls as empty strings so downstream code
    doesn't have to special-case None."""
    payload = _load("people-outlook-rest-redacted.json")
    person = parser.parse_people_lookup(payload)[0]
    assert person["display_name"] == "Sample User"
    assert person["given_name"] == "Sample"
    assert person["surname"] == "User"
    assert person["person_type"] == "OrganizationUser"
    assert person["email"].endswith("@example-user.com")
    # Null enrichment fields collapse to empty strings.
    assert person["job_title"] == ""
    assert person["company_name"] == ""
    assert person["department"] == ""


def test_people_outlook_rest_implicit_contact():
    """External invitee -- on a different tenant from the OWA user.
    PersonType.Subclass = ImplicitContact. No GivenName / Surname,
    no UPN, no IM address; just the email + DisplayName + a high
    RelevanceScore proving the user has corresponded with this
    address before."""
    payload = _load("people-outlook-rest-redacted.json")
    person = parser.parse_people_lookup(payload)[1]
    assert person["display_name"] == "Sample User"
    assert person["person_type"] == "ImplicitContact"
    assert person["email"].endswith("@example-org.com")
    assert person["given_name"] == ""
    assert person["surname"] == ""
    # Same null-as-empty contract for enrichment fields.
    assert person["job_title"] == ""
    assert person["company_name"] == ""


def test_parser_accepts_both_casings_in_one_payload():
    """Mixed-shape payloads shouldn't happen in practice but the
    parser should not crash if some events are PascalCase and
    others camelCase (e.g. a hypothetical layered fallback that
    merged responses from both endpoints)."""
    mixed = {
        "value": [
            {  # PascalCase
                "Id": "evt-pascal",
                "Subject": "Pascal one",
                "Start": {"DateTime": "2026-06-01T10:00:00.0000000", "TimeZone": "UTC"},
                "End":   {"DateTime": "2026-06-01T10:30:00.0000000", "TimeZone": "UTC"},
                "Attendees": [],
                "Organizer": {"EmailAddress": {"Name": "P", "Address": "p@x.com"}},
            },
            {  # camelCase
                "id": "evt-camel",
                "subject": "Camel one",
                "start": {"dateTime": "2026-06-01T11:00:00.0000000", "timeZone": "UTC"},
                "end":   {"dateTime": "2026-06-01T11:30:00.0000000", "timeZone": "UTC"},
                "attendees": [],
                "organizer": {"emailAddress": {"name": "C", "address": "c@x.com"}},
            },
        ],
    }
    out = parser.parse_calendarview(mixed)
    assert len(out) == 2
    assert {m.subject for m in out} == {"Pascal one", "Camel one"}
    assert {m.event_id for m in out} == {"evt-pascal", "evt-camel"}


# ---- capture redaction ----------------------------------------------------


def test_redact_emails_replaces_local_part_only():
    raw = "contact aaron.dodd@example.com or someone+filter@example.org"
    out = capture.redact_emails(raw)
    assert "aaron.dodd" not in out
    assert "someone+filter" not in out
    assert "***@example.com" in out
    assert "***@example.org" in out


def test_redact_emails_is_idempotent():
    once = capture.redact_emails("aaron@example.com")
    twice = capture.redact_emails(once)
    assert once == twice == "***@example.com"


def test_redact_emails_walks_nested_structures():
    payload = {
        "value": [
            {"emailAddress": {"address": "aaron@example.com"}},
            {"emailAddress": {"address": "second@example.org"}},
        ],
        "url": "https://x/?$search=%22aaron@example.com%22",
    }
    out = capture.redact_emails(payload)
    assert out["value"][0]["emailAddress"]["address"] == "***@example.com"
    assert out["value"][1]["emailAddress"]["address"] == "***@example.org"
    assert "aaron@" not in out["url"]
    # Original payload must not be mutated.
    assert payload["value"][0]["emailAddress"]["address"] == "aaron@example.com"


def test_redact_emails_passes_non_strings_through():
    assert capture.redact_emails(42) == 42
    assert capture.redact_emails(None) is None
    assert capture.redact_emails(True) is True
