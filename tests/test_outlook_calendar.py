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
    MeetingInfo,
    _DedupStore,
    _RemainingTodayCache,
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

    def fake_range(start, end, *, light=False):
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

    def fake_range(start, end, *, light=False):
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


# ---- PropertyAccessor + Resolve paths (2026-05-28 followup) ----------------


class _FakePropertyAccessor:
    """Stand-in for Recipient.PropertyAccessor.

    Maps proptag URLs -> returned values. Missing keys raise so the
    real path's exception swallow is exercised.
    """

    def __init__(self, mapping=None):
        self._mapping = mapping or {}

    def GetProperty(self, proptag):
        if proptag not in self._mapping:
            raise RuntimeError("property not available")
        return self._mapping[proptag]


class _FakeRecipientWithResolve:
    """Recipient stand-in that records whether Resolve() was called.

    Test hook for the step-1 path that resolves unresolved recipients
    against the GAL before any AddressEntry access.
    """

    def __init__(self, *, name="", address_entry=None, accessor=None):
        self.Name = name
        self.AddressEntry = address_entry
        self.PropertyAccessor = accessor
        self.resolve_calls = 0

    def Resolve(self):
        self.resolve_calls += 1


def test_resolve_calls_recipient_resolve_first():
    """Recipient.Resolve() runs before AddressEntry access so an
    unresolved attendee gets a chance to be matched against the GAL."""
    ae = _FakeAddressEntry(
        exchange_user=_FakeExchangeUser(
            primary_smtp="bob@fhb.com", job_title="VP",
            company="FHB", department="EDA",
        ),
    )
    rec = _FakeRecipientWithResolve(name="Bob", address_entry=ae)
    fields = outlook_calendar._resolve_recipient_fields(rec)
    assert rec.resolve_calls == 1
    assert fields["email"] == "bob@fhb.com"
    assert fields["title"] == "VP"


def test_property_accessor_fills_when_exchange_user_returns_none():
    """The known-bad path: GetExchangeUser() returns None (cached
    Exchange / no offline GAL). PropertyAccessor fills the fields
    via MAPI proptags so the user still gets enrichment."""
    accessor = _FakePropertyAccessor({
        outlook_calendar._PR_SMTP_ADDRESS: "bob@fhb.com",
        outlook_calendar._PR_TITLE: "VP Engineering",
        outlook_calendar._PR_COMPANY_NAME: "First Hawaiian Bank",
        outlook_calendar._PR_DEPARTMENT_NAME: "EDA",
    })
    ae = _FakeAddressEntry(
        address="/o=ExchangeLabs/ou=g/cn=r/cn=bob",  # X.500 garbage
        exchange_user=None,  # the failure mode we're testing
    )
    rec = _FakeRecipientWithResolve(
        name="Bob", address_entry=ae, accessor=accessor,
    )
    fields = outlook_calendar._resolve_recipient_fields(rec)
    assert fields["email"] == "bob@fhb.com"
    assert "/o=" not in fields["email"]
    assert fields["title"] == "VP Engineering"
    assert fields["company"] == "First Hawaiian Bank"
    assert fields["department"] == "EDA"


def test_property_accessor_fills_missing_fields_only():
    """When ExchangeUser provided some fields but not all, the
    PropertyAccessor backs only the empties -- doesn't overwrite."""
    accessor = _FakePropertyAccessor({
        outlook_calendar._PR_TITLE: "PROPACCESSOR_TITLE",  # would overwrite
        outlook_calendar._PR_COMPANY_NAME: "PROPACCESSOR_COMPANY",
        outlook_calendar._PR_DEPARTMENT_NAME: "EDA",  # fills empty
    })
    ae = _FakeAddressEntry(
        exchange_user=_FakeExchangeUser(
            primary_smtp="bob@fhb.com",
            job_title="VP",  # already set, must NOT be overwritten
            company="FHB",
            department="",  # empty, PropertyAccessor fills
        ),
    )
    rec = _FakeRecipientWithResolve(
        name="Bob", address_entry=ae, accessor=accessor,
    )
    fields = outlook_calendar._resolve_recipient_fields(rec)
    assert fields["title"] == "VP"  # preserved
    assert fields["company"] == "FHB"  # preserved
    assert fields["department"] == "EDA"  # filled


def test_property_accessor_skips_legacy_dn_email():
    """If the PropertyAccessor returns a legacy DN (shouldn't happen
    in practice, but defensive), the guard drops it rather than
    accepting a useless string."""
    accessor = _FakePropertyAccessor({
        outlook_calendar._PR_SMTP_ADDRESS: "/o=ExchangeLabs/ou=g/cn=r/cn=bob",
    })
    ae = _FakeAddressEntry(exchange_user=None)
    rec = _FakeRecipientWithResolve(
        name="Bob", address_entry=ae, accessor=accessor,
    )
    fields = outlook_calendar._resolve_recipient_fields(rec)
    assert fields["email"] == ""


def test_resolve_handles_resolve_call_raising():
    """Recipient.Resolve() raises (offline, GAL unavailable). The
    downstream paths still run and may still recover fields."""
    class _RaisingRecipient:
        Name = "Bob"
        def __init__(self):
            self.AddressEntry = _FakeAddressEntry(
                exchange_user=_FakeExchangeUser(
                    primary_smtp="bob@fhb.com", job_title="VP",
                ),
            )
        def Resolve(self):
            raise RuntimeError("offline")

    fields = outlook_calendar._resolve_recipient_fields(_RaisingRecipient())
    assert fields["email"] == "bob@fhb.com"
    assert fields["title"] == "VP"


def test_address_entry_property_accessor_fills_when_others_empty():
    """When GetExchangeUser() returns an object with empty fields AND
    Recipient.PropertyAccessor also comes back blank, the
    AddressEntry.PropertyAccessor is the last automatic fallback.

    This is the FHB Exchange Online + cached-mode failure mode Aaron
    hit on 2026-05-28 -- ExchangeUser came back but with empty
    title/company, Recipient.PA also empty, ae_pa fills in."""
    # ExchangeUser exists but every field is empty/None.
    eu = _FakeExchangeUser(
        primary_smtp="", job_title="", company="", department="",
    )
    ae_accessor = _FakePropertyAccessor({
        outlook_calendar._PR_SMTP_ADDRESS: "bob@fhb.com",
        outlook_calendar._PR_TITLE: "VP Engineering",
        outlook_calendar._PR_COMPANY_NAME: "First Hawaiian Bank",
        outlook_calendar._PR_DEPARTMENT_NAME: "EDA",
    })

    class _AEWithPA(_FakeAddressEntry):
        def __init__(self, *args, ae_accessor=None, **kwargs):
            super().__init__(*args, **kwargs)
            self.PropertyAccessor = ae_accessor

    ae = _AEWithPA(exchange_user=eu, ae_accessor=ae_accessor)
    # Recipient PA returns nothing -- the gap the ae_pa fills.
    empty_accessor = _FakePropertyAccessor({})
    rec = _FakeRecipientWithResolve(
        name="Bob", address_entry=ae, accessor=empty_accessor,
    )
    fields = outlook_calendar._resolve_recipient_fields(rec)
    assert fields["email"] == "bob@fhb.com"
    assert fields["title"] == "VP Engineering"
    assert fields["company"] == "First Hawaiian Bank"
    assert fields["department"] == "EDA"


# ---- _RemainingTodayCache (#102 bug 4) -----------------------------------


def _meeting(eid, start_h, end_h, *, now_year=2026, now_month=6, now_day=10):
    return MeetingInfo(
        entry_id=eid,
        subject=f"M-{eid}",
        start_time=datetime(now_year, now_month, now_day, start_h, 0),
        end_time=datetime(now_year, now_month, now_day, end_h, 0),
    )


def test_cache_initial_state_is_stale():
    cache = _RemainingTodayCache(ttl_seconds=60)
    assert cache.is_fresh(datetime(2026, 6, 10, 9, 0)) is False


def test_cache_get_populates_and_returns_filtered(monkeypatch):
    """get() runs fetch_remaining_today + post-filters by end_time > now
    so already-ended meetings vanish on the cached read."""
    cache = _RemainingTodayCache(ttl_seconds=60)
    fake_payload = [
        _meeting("done", 8, 9),
        _meeting("now", 9, 11),
        _meeting("later", 14, 15),
    ]

    def fake_fetch(now=None, *, light=False):
        assert light is True  # cache must request the light variant
        return list(fake_payload)

    monkeypatch.setattr(outlook_calendar, "fetch_remaining_today", fake_fetch)
    result = cache.get(now=datetime(2026, 6, 10, 9, 30))
    subjects = sorted(m.subject for m in result)
    assert subjects == ["M-later", "M-now"]


def test_cache_returns_fresh_without_refetching(monkeypatch):
    # Generous TTL so the 15-minute span of queries below stays inside
    # the fresh window. The picker's real-world rhythm (open dialog ->
    # close -> open again) is seconds, well under the 60s default.
    cache = _RemainingTodayCache(ttl_seconds=3600)
    calls = {"n": 0}

    def fake_fetch(now=None, *, light=False):
        calls["n"] += 1
        return [_meeting("x", 10, 11)]

    monkeypatch.setattr(outlook_calendar, "fetch_remaining_today", fake_fetch)
    cache.get(now=datetime(2026, 6, 10, 9, 30))
    cache.get(now=datetime(2026, 6, 10, 9, 35))
    cache.get(now=datetime(2026, 6, 10, 9, 45))
    assert calls["n"] == 1  # only the first call refetched


def test_cache_refetches_after_ttl(monkeypatch):
    cache = _RemainingTodayCache(ttl_seconds=30)
    calls = {"n": 0}

    def fake_fetch(now=None, *, light=False):
        calls["n"] += 1
        return [_meeting("x", 10, 11)]

    monkeypatch.setattr(outlook_calendar, "fetch_remaining_today", fake_fetch)
    cache.get(now=datetime(2026, 6, 10, 9, 30))
    # 31 seconds later -> TTL expired
    cache.get(now=datetime(2026, 6, 10, 9, 30, 31))
    assert calls["n"] == 2


def test_cache_refetches_on_day_rollover(monkeypatch):
    """Even within the TTL window, a date change invalidates the
    cached payload -- the lookback start moves to a different day."""
    cache = _RemainingTodayCache(ttl_seconds=60 * 60 * 6)  # generous TTL
    calls = {"n": 0}

    def fake_fetch(now=None, *, light=False):
        calls["n"] += 1
        return [_meeting("x", 10, 11)]

    monkeypatch.setattr(outlook_calendar, "fetch_remaining_today", fake_fetch)
    cache.get(now=datetime(2026, 6, 10, 23, 50))
    cache.get(now=datetime(2026, 6, 11, 0, 1))
    assert calls["n"] == 2


def test_cache_invalidate_drops_state(monkeypatch):
    cache = _RemainingTodayCache(ttl_seconds=60)

    def fake_fetch(now=None, *, light=False):
        return [_meeting("x", 10, 11)]

    monkeypatch.setattr(outlook_calendar, "fetch_remaining_today", fake_fetch)
    cache.get(now=datetime(2026, 6, 10, 9, 0))
    assert cache.is_fresh(datetime(2026, 6, 10, 9, 1))
    cache.invalidate()
    assert not cache.is_fresh(datetime(2026, 6, 10, 9, 1))


def test_cache_refresh_forces_refetch(monkeypatch):
    cache = _RemainingTodayCache(ttl_seconds=60)
    calls = {"n": 0}

    def fake_fetch(now=None, *, light=False):
        calls["n"] += 1
        return [_meeting("x", 10, 11)]

    monkeypatch.setattr(outlook_calendar, "fetch_remaining_today", fake_fetch)
    cache.get(now=datetime(2026, 6, 10, 9, 0))
    cache.refresh(now=datetime(2026, 6, 10, 9, 0))
    assert calls["n"] == 2


def test_fetch_remaining_today_passes_light_through(monkeypatch):
    """The light=True kwarg must reach fetch_calendar_range so the
    underlying _item_to_info_light path is exercised."""
    captured = {}

    def fake_range(start, end, *, light=False):
        captured["light"] = light
        return []

    monkeypatch.setattr(outlook_calendar, "fetch_calendar_range", fake_range)
    fetch_remaining_today(now=datetime(2026, 6, 10, 9, 0), light=True)
    assert captured["light"] is True


# ---- _apply_instance_times (#102 follow-up: recurring master/occurrence) -


def test_apply_instance_times_passes_through_when_both_none():
    base = MeetingInfo(
        entry_id="x",
        subject="Recurring",
        start_time=datetime(2026, 1, 5, 9, 0),  # master start
        end_time=datetime(2026, 1, 5, 10, 0),
    )
    out = outlook_calendar._apply_instance_times(
        base, start=None, end=None,
    )
    # No override -> same dataclass identity (or at least same fields).
    assert out.start_time == base.start_time
    assert out.end_time == base.end_time


def test_apply_instance_times_overrides_start_only():
    base = MeetingInfo(
        entry_id="x",
        subject="Recurring",
        start_time=datetime(2026, 1, 5, 9, 0),  # master
        end_time=datetime(2026, 1, 5, 10, 0),
    )
    out = outlook_calendar._apply_instance_times(
        base,
        start=datetime(2026, 6, 11, 9, 0),  # today's occurrence
        end=None,
    )
    assert out.start_time == datetime(2026, 6, 11, 9, 0)
    assert out.end_time == base.end_time


def test_apply_instance_times_overrides_both():
    """The Aaron's-2026-06-11-bug shape: the master Start/End from
    GetItemFromID must be replaced with the occurrence times the
    light cache captured so the downstream session created_at lines
    up with the picked day's instance, not the series's first
    instance."""
    master = MeetingInfo(
        entry_id="x",
        subject="Weekly sync",
        start_time=datetime(2025, 1, 7, 9, 0),  # series start
        end_time=datetime(2025, 1, 7, 9, 30),
        attendees=[],
        body="",
        location="",
        attachments=[],
    )
    occurrence_start = datetime(2026, 6, 11, 9, 0)
    occurrence_end = datetime(2026, 6, 11, 9, 30)
    out = outlook_calendar._apply_instance_times(
        master,
        start=occurrence_start,
        end=occurrence_end,
    )
    assert out.start_time == occurrence_start
    assert out.end_time == occurrence_end
    # Non-time fields are unchanged.
    assert out.subject == master.subject
    assert out.entry_id == master.entry_id


def test_apply_instance_times_preserves_master_when_partial_override():
    """Defensive: caller passes only `end` (rare). Start keeps master,
    end takes the override -- no field gets lost."""
    base = MeetingInfo(
        entry_id="x",
        subject="x",
        start_time=datetime(2025, 1, 7, 9, 0),
        end_time=datetime(2025, 1, 7, 9, 30),
    )
    out = outlook_calendar._apply_instance_times(
        base, start=None, end=datetime(2026, 6, 11, 9, 30),
    )
    assert out.start_time == base.start_time
    assert out.end_time == datetime(2026, 6, 11, 9, 30)


def test_fetch_meeting_by_entry_id_no_outlook_ignores_instance_times():
    """Without Outlook on the host, the function returns None
    regardless of whether instance_start/end are passed."""
    from meeting_notetaker.integrations.outlook_calendar import (
        fetch_meeting_by_entry_id,
    )
    assert fetch_meeting_by_entry_id("anything") is None
    assert fetch_meeting_by_entry_id(
        "anything",
        instance_start=datetime(2026, 6, 11, 9, 0),
        instance_end=datetime(2026, 6, 11, 9, 30),
    ) is None
