"""Pure-Python helpers from the export-flow module (#79).

The dialog + worker side of integrations_export_flow lives behind Qt
imports we don't unit-test here; this file covers the deterministic
helpers (recents push, title build) that are stateless.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from meeting_notetaker.integrations.integrations_export_flow import (  # noqa: E402
    _push_recent,
)


def test_push_recent_inserts_at_front_and_dedups_by_id():
    existing = [
        {"id": "a", "title": "A", "used_at": "2026-06-01T10:00:00"},
        {"id": "b", "title": "B", "used_at": "2026-06-01T09:00:00"},
    ]
    out = _push_recent(existing, "c", "C", {})
    assert [e["id"] for e in out] == ["c", "a", "b"]


def test_push_recent_dedupes_when_id_already_present():
    existing = [
        {"id": "a", "title": "A old", "used_at": "2026-06-01T10:00:00"},
        {"id": "b", "title": "B", "used_at": "2026-06-01T09:00:00"},
    ]
    out = _push_recent(existing, "a", "A new", {})
    assert [e["id"] for e in out] == ["a", "b"]
    # New entry's title supersedes the old one.
    assert out[0]["title"] == "A new"


def test_push_recent_caps_at_five():
    existing = [
        {"id": f"r{i}", "title": f"R{i}", "used_at": "2026-06-01T10:00:00"}
        for i in range(5)
    ]
    out = _push_recent(existing, "new", "New", {})
    assert len(out) == 5
    assert out[0]["id"] == "new"
    # Oldest dropped (was r4 at the tail).
    assert "r4" not in {e["id"] for e in out}


def test_push_recent_stamps_used_at():
    out = _push_recent([], "a", "A", {})
    assert "used_at" in out[0]
    # Roughly ISO-formatted; not asserting exact value (clock-dependent).
    assert "T" in out[0]["used_at"]


def test_push_recent_preserves_extra_payload():
    out = _push_recent([], "a", "A", {"space_id": "100"})
    assert out[0]["extra"] == {"space_id": "100"}


def test_push_recent_omits_extra_when_empty():
    out = _push_recent([], "a", "A", {})
    assert "extra" not in out[0]


# ---- default page title + series lookup --------------------------------


def test_default_page_title_uses_yyyy_mm_dd_hh_mm_dash_title():
    """Default for the picker's title field: 'YYYY-MM-DD HH:MM - <title>'
    (Aaron's 2026-06-03 ask). Date+time is in the user's local
    timezone so it matches the rest of the app's display."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from meeting_notetaker.integrations.integrations_export_flow import (
        _default_page_title,
    )

    session = SimpleNamespace(
        id="s-1",
        title="Weekly Sync",
        # Stored as UTC ISO; the helper converts to local for display.
        created_at=datetime(2026, 6, 3, 21, 30, 0, tzinfo=timezone.utc).isoformat(),
    )

    class _Store:
        def get_session(self, sid):
            return session if sid == "s-1" else None

    main_app = SimpleNamespace(store=_Store())
    title = _default_page_title(main_app, "s-1")
    # The local-time slot varies by host TZ; we just assert the shape:
    # YYYY-MM-DD HH:MM - Weekly Sync.
    assert " - Weekly Sync" in title
    assert title.split(" - ")[0].count("-") == 2  # YYYY-MM-DD


def test_default_page_title_falls_back_to_title_alone_when_no_timestamp():
    from types import SimpleNamespace

    from meeting_notetaker.integrations.integrations_export_flow import (
        _default_page_title,
    )

    session = SimpleNamespace(id="s", title="Untitled", created_at="")

    class _Store:
        def get_session(self, sid):
            return session

    main_app = SimpleNamespace(store=_Store())
    assert _default_page_title(main_app, "s") == "Untitled"


def test_default_page_title_handles_unknown_session():
    """Robust against the session id not resolving (race during
    delete, etc.) -- never raises."""
    from types import SimpleNamespace

    from meeting_notetaker.integrations.integrations_export_flow import (
        _default_page_title,
    )

    class _Store:
        def get_session(self, sid):
            return None

    main_app = SimpleNamespace(store=_Store())
    # Falls back to the session id itself rather than raising.
    assert _default_page_title(main_app, "ghost") == "ghost"


def test_session_series_name_returns_empty_when_no_store():
    from types import SimpleNamespace

    from meeting_notetaker.integrations.integrations_export_flow import (
        _session_series_name,
    )

    main_app = SimpleNamespace(classification=None)
    assert _session_series_name(main_app, "s") == ""


def test_session_series_name_returns_series_name_when_set():
    from types import SimpleNamespace

    from meeting_notetaker.integrations.integrations_export_flow import (
        _session_series_name,
    )

    series = SimpleNamespace(name="Weekly Engineering Sync")

    class _Store:
        def series_for_session(self, sid):
            return series

    main_app = SimpleNamespace(classification=_Store())
    assert _session_series_name(main_app, "s") == "Weekly Engineering Sync"


def test_session_series_name_swallows_exceptions():
    from types import SimpleNamespace

    from meeting_notetaker.integrations.integrations_export_flow import (
        _session_series_name,
    )

    class _Broken:
        def series_for_session(self, sid):
            raise RuntimeError("db gone")

    main_app = SimpleNamespace(classification=_Broken())
    # Empty fallback rather than bubbling -- export is best-effort
    # informational here.
    assert _session_series_name(main_app, "s") == ""
