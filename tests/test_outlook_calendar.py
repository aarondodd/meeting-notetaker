"""Pure-Python tests for Outlook integration helpers.

COM dispatch + Qt monitor are exercised on real Windows; these cover the
sanitize_body parser + the dedup store + the calendar-flavored live-notes
seed.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from meeting_notetaker.integrations.outlook_calendar import (
    _DedupStore,
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
