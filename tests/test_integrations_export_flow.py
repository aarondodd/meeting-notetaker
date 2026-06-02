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
