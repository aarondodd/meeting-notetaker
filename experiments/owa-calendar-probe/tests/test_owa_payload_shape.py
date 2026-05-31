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
    assert "Aaron Dodd" in names


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
