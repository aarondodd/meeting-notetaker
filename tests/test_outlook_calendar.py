"""Pure-Python tests for Outlook integration helpers.

COM dispatch + Qt monitor are exercised on real Windows; these cover the
sanitize_body parser + the dedup store + the calendar-flavored live-notes
seed + the no-COM fallback paths of the range/remaining-today fetchers.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from meeting_notetaker.integrations import outlook_calendar
from meeting_notetaker.integrations.outlook_calendar import (
    _DedupStore,
    fetch_calendar_range,
    fetch_remaining_today,
    is_available,
    sanitize_body,
)
from meeting_notetaker.utils.live_notes import seed_body_with_calendar


# ---- sanitize_body ---------------------------------------------------------


def test_sanitize_strips_html_tags():
    raw = "<p>Hello <b>world</b></p>"
    assert sanitize_body(raw) == "Hello world"


def test_sanitize_collapses_whitespace():
    raw = "Line one\r\n\r\n\r\n\r\nLine two   with     spaces"
    out = sanitize_body(raw)
    assert "Line one" in out
    assert "Line two with spaces" in out
    # No runs of 3+ blank lines.
    assert "\n\n\n" not in out


def test_sanitize_handles_empty_and_none():
    assert sanitize_body("") == ""
    assert sanitize_body("   \n   \r\n   ") == ""


def test_sanitize_clips_long_bodies():
    raw = "x" * 5000
    out = sanitize_body(raw, max_chars=100)
    assert len(out) == 100
    assert out.endswith("...")


def test_sanitize_does_not_clip_short_bodies():
    raw = "Short agenda"
    out = sanitize_body(raw, max_chars=100)
    assert out == "Short agenda"


def test_sanitize_does_not_eat_normal_paragraph_breaks():
    raw = "Topic 1\n\nTopic 2\n\nTopic 3"
    out = sanitize_body(raw)
    assert out.count("\n\n") == 2


# ---- _DedupStore -----------------------------------------------------------


def test_dedup_marks_and_recalls(tmp_path):
    store = _DedupStore(tmp_path / "calendar_state.json")
    assert store.is_seen("abc") is False
    store.mark_seen("abc")
    assert store.is_seen("abc") is True


def test_dedup_persists_across_instances(tmp_path):
    path = tmp_path / "calendar_state.json"
    _DedupStore(path).mark_seen("abc")
    assert _DedupStore(path).is_seen("abc") is True


def test_dedup_drops_stale_dates_on_write(tmp_path):
    path = tmp_path / "calendar_state.json"
    # Seed yesterday's marker by hand; today's mark should drop it.
    yesterday = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    path.write_text(json.dumps({yesterday: ["old-id"]}), encoding="utf-8")
    store = _DedupStore(path)
    store.mark_seen("new-id")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert yesterday not in saved
    assert "new-id" in saved[datetime.now().strftime("%Y-%m-%d")]


def test_dedup_handles_corrupt_file(tmp_path):
    path = tmp_path / "calendar_state.json"
    path.write_text("{not valid json", encoding="utf-8")
    store = _DedupStore(path)
    assert store.is_seen("anything") is False
    store.mark_seen("foo")  # should not raise
    assert store.is_seen("foo") is True


def test_dedup_clears_after_day_rolls(tmp_path):
    path = tmp_path / "calendar_state.json"
    # Use the injectable clock to simulate a day change.
    base = datetime(2026, 5, 17, 10, 0)
    later = datetime(2026, 5, 18, 9, 0)

    day_one = _DedupStore(path, now=base)
    day_one.mark_seen("entry-1")
    assert day_one.is_seen("entry-1") is True

    day_two = _DedupStore(path, now=later)
    assert day_two.is_seen("entry-1") is False


# ---- live_notes seed with calendar -----------------------------------------


def test_seed_with_calendar_attendees_only():
    body = seed_body_with_calendar(attendees=["Alice", "Bob"], agenda="")
    assert "# Attendees" in body
    assert "- Alice" in body
    assert "- Bob" in body
    assert "# Agenda" in body
    assert "# Notes" in body
    assert "# Action Items" in body


def test_seed_with_calendar_agenda_only():
    body = seed_body_with_calendar(attendees=[], agenda="Review Q3 progress")
    assert "Review Q3 progress" in body
    # No bullets when attendees list is empty.
    assert "- " in body  # the placeholder dash from the template


def test_seed_with_calendar_dedupes_blank_names():
    body = seed_body_with_calendar(attendees=["", "Alice", ""], agenda="")
    # Blank names are dropped.
    lines = [
        line for line in body.splitlines()
        if line.startswith("- ") and line != "- "
    ]
    assert lines == ["- Alice"]


def test_seed_with_calendar_strips_agenda_whitespace():
    body = seed_body_with_calendar(attendees=[], agenda="\n\n  text  \n\n")
    assert "# Agenda\ntext\n" in body


# ---- fetch_* fallback paths ------------------------------------------------


def test_is_available_false_on_linux():
    # Linux dev runtime: pywin32 isn't installable, so the integration must
    # advertise itself as unavailable and the fetchers return [] cleanly.
    assert is_available() is False


def test_fetch_calendar_range_returns_empty_when_unavailable():
    # When is_available() is False, no exception should be raised; we just
    # get an empty list. (Forced by the early-return in fetch_calendar_range.)
    start = datetime(2026, 5, 17, 9, 0)
    end = datetime(2026, 5, 17, 18, 0)
    assert fetch_calendar_range(start, end) == []


def test_fetch_remaining_today_returns_empty_when_unavailable():
    assert fetch_remaining_today() == []
    # And with an injected `now` -- still no Outlook, still empty.
    assert fetch_remaining_today(now=datetime(2026, 5, 17, 14, 30)) == []


def test_fetch_remaining_today_looks_back_for_in_progress_meetings(monkeypatch):
    """v0.6.5: include meetings that started before now but haven't ended.

    The Outlook restrict filter is on Start; we widen the fetch
    backwards a few hours, then post-filter by end_time > now. Confirm
    the widening + the filter both work.
    """
    from meeting_notetaker.integrations.outlook_calendar import MeetingInfo

    now = datetime(2026, 5, 17, 14, 30)
    captured: dict = {}

    def fake_range(start, end):
        captured["start"] = start
        captured["end"] = end
        # Three candidates:
        #   - started 4h ago, ended an hour ago (DONE, must be dropped)
        #   - started 30m ago, still running (IN-PROGRESS, must surface)
        #   - starts in 2h (UPCOMING, must surface)
        return [
            MeetingInfo(
                entry_id="done", subject="Done",
                start_time=datetime(2026, 5, 17, 10, 30),
                end_time=datetime(2026, 5, 17, 13, 30),
            ),
            MeetingInfo(
                entry_id="now", subject="In Progress",
                start_time=datetime(2026, 5, 17, 14, 0),
                end_time=datetime(2026, 5, 17, 15, 0),
            ),
            MeetingInfo(
                entry_id="later", subject="Upcoming",
                start_time=datetime(2026, 5, 17, 16, 30),
                end_time=datetime(2026, 5, 17, 17, 30),
            ),
        ]

    monkeypatch.setattr(outlook_calendar, "fetch_calendar_range", fake_range)
    result = fetch_remaining_today(now=now)
    # The lookback start sits before now; the filter drops the
    # already-ended meeting.
    assert captured["start"] < now
    subjects = [m.subject for m in result]
    assert "In Progress" in subjects
    assert "Upcoming" in subjects
    assert "Done" not in subjects


def test_fetch_remaining_today_end_of_day_still_safe(monkeypatch):
    """The 23:59:59 edge case still doesn't raise and still walks the
    lookback window."""
    captured: dict = {}

    def fake_range(start, end):
        captured["start"] = start
        captured["end"] = end
        return []

    monkeypatch.setattr(outlook_calendar, "fetch_calendar_range", fake_range)
    fetch_remaining_today(now=datetime(2026, 5, 17, 23, 59, 30))
    assert captured["end"].hour == 23
    assert captured["end"] >= captured["start"]


# ---- _resolve_recipient_fields (2026-05-28) --------------------------------
#
# COM Recipient/AddressEntry/ExchangeUser are duck-typed via simple
# Fake* classes here. The resolver is the only place we touch those
# attributes, so end-to-end coverage at this layer is sufficient.


class _FakeExchangeUser:
    def __init__(
        self, *,
        primary_smtp="", job_title="", company="", department="",
    ):
        self.PrimarySmtpAddress = primary_smtp
        self.JobTitle = job_title
        self.CompanyName = company
        self.Department = department


class _FakeAddressEntry:
    """AddressEntry stand-in.

    `exchange_user` is the object returned by GetExchangeUser(); pass
    None to simulate an SMTP/external entry where the call returns
    None (in real COM it would raise or return None).
    """

    def __init__(
        self, *,
        address="", title="", company="", department="",
        exchange_user=None, raise_on_get_exchange=False,
    ):
        self.Address = address
        self.JobTitle = title
        self.CompanyName = company
        self.Department = department
        self._exchange_user = exchange_user
        self._raise_on_get_exchange = raise_on_get_exchange

    def GetExchangeUser(self):
        if self._raise_on_get_exchange:
            raise RuntimeError("GetExchangeUser failed")
        return self._exchange_user


class _FakeRecipient:
    def __init__(self, *, name="", address_entry=None):
        self.Name = name
        self.AddressEntry = address_entry


def test_resolve_exchange_user_returns_primary_smtp_not_legacy_dn():
    """For an Exchange recipient, the resolver pulls PrimarySmtpAddress
    from GetExchangeUser() and ignores the X.500 AddressEntry.Address."""
    legacy_dn = (
        "/o=ExchangeLabs/ou=Exchange Administrative Group "
        "(FYDIBOHF23SPDLT)/cn=Recipients/cn=user-guid"
    )
    rec = _FakeRecipient(
        name="Bob Smith",
        address_entry=_FakeAddressEntry(
            address=legacy_dn,
            exchange_user=_FakeExchangeUser(
                primary_smtp="bob@fhb.com",
                job_title="VP Engineering",
                company="First Hawaiian Bank",
                department="EDA",
            ),
        ),
    )
    fields = outlook_calendar._resolve_recipient_fields(rec)
    assert fields["name"] == "Bob Smith"
    assert fields["email"] == "bob@fhb.com"
    assert "/o=" not in fields["email"]
    assert fields["title"] == "VP Engineering"
    assert fields["company"] == "First Hawaiian Bank"
    assert fields["department"] == "EDA"


def test_resolve_smtp_external_uses_address_entry_address():
    """External SMTP attendees: no ExchangeUser, but AddressEntry.Address
    holds a real email. The resolver returns it verbatim."""
    rec = _FakeRecipient(
        name="External Vendor",
        address_entry=_FakeAddressEntry(
            address="vendor@external.com",
            exchange_user=None,
        ),
    )
    fields = outlook_calendar._resolve_recipient_fields(rec)
    assert fields["email"] == "vendor@external.com"
    assert fields["title"] == ""
    assert fields["company"] == ""


def test_resolve_get_exchange_user_failure_falls_back():
    """If GetExchangeUser raises, the resolver falls back to direct
    AddressEntry fields. Email guard still drops X.500 DN values."""
    rec = _FakeRecipient(
        name="Bob",
        address_entry=_FakeAddressEntry(
            address="bob@bobco.com",  # SMTP-style, accepted
            title="CEO",
            company="Bobco",
            raise_on_get_exchange=True,
        ),
    )
    fields = outlook_calendar._resolve_recipient_fields(rec)
    assert fields["email"] == "bob@bobco.com"
    assert fields["title"] == "CEO"
    assert fields["company"] == "Bobco"


def test_resolve_drops_legacy_dn_even_in_fallback():
    """If GetExchangeUser fails AND AddressEntry.Address is a legacy
    DN, the resolver returns empty email rather than a useless DN."""
    legacy_dn = "/o=ExchangeLabs/ou=Group/cn=Recipients/cn=bob"
    rec = _FakeRecipient(
        name="Bob",
        address_entry=_FakeAddressEntry(
            address=legacy_dn,
            raise_on_get_exchange=True,
        ),
    )
    fields = outlook_calendar._resolve_recipient_fields(rec)
    assert fields["email"] == ""
    assert fields["name"] == "Bob"  # name still recovered


def test_resolve_handles_no_address_entry():
    """Recipient with AddressEntry=None returns name-only fields."""
    rec = _FakeRecipient(name="Bob", address_entry=None)
    fields = outlook_calendar._resolve_recipient_fields(rec)
    assert fields == {
        "name": "Bob", "email": "",
        "title": "", "company": "", "department": "",
    }


def test_looks_like_legacy_dn_recognizes_known_prefixes():
    """The detector matches /o=, /O=, /cn=, /CN= prefixes; rejects
    plain SMTP addresses and empty strings."""
    f = outlook_calendar._looks_like_legacy_dn
    assert f("/o=ExchangeLabs/ou=Group/cn=Recipients/cn=user") is True
    assert f("/O=ExchangeLabs/...") is True
    assert f("/cn=user") is True
    assert f("/CN=user") is True
    assert f("bob@fhb.com") is False
    assert f("") is False
    assert f(None) is False
