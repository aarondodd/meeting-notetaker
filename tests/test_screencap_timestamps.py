"""Screenshot timestamp parsing + bisect-by-position lookups.

These helpers are the seam between the on-disk screenshot files and
the UI surfaces that anchor them (transcript rail + playback layout
top image). Pin the filename grammar, the recording-relative offset
math, the closest-block anchor rule, and the sticky-image lookup.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from meeting_notetaker.screencap.timestamps import (
    current_screenshot_for_position,
    match_screenshots_to_blocks,
    offset_ms,
    parse_started_at,
    screenshot_offsets,
    screenshot_taken_at,
)


def test_screenshot_taken_at_parses_standard_name(tmp_path):
    p = tmp_path / "0001-20260524T143200Z.png"
    p.touch()
    dt = screenshot_taken_at(p)
    assert dt is not None
    assert dt == datetime(2026, 5, 24, 14, 32, 0, tzinfo=timezone.utc)


def test_screenshot_taken_at_returns_none_for_bad_names(tmp_path):
    for name in ("foo.png", "0001-not-a-timestamp.png", "0001-20260524.png"):
        p = tmp_path / name
        p.touch()
        assert screenshot_taken_at(p) is None


def test_parse_started_at_handles_z_suffix():
    dt = parse_started_at("2026-05-24T14:30:00Z")
    assert dt is not None
    assert dt == datetime(2026, 5, 24, 14, 30, 0, tzinfo=timezone.utc)


def test_parse_started_at_returns_none_on_garbage():
    assert parse_started_at(None) is None
    assert parse_started_at("") is None
    assert parse_started_at("not-a-date") is None


def test_offset_ms_returns_expected_delta():
    taken = datetime(2026, 5, 24, 14, 35, 20, tzinfo=timezone.utc)
    started = datetime(2026, 5, 24, 14, 30, 0, tzinfo=timezone.utc)
    # 5 min 20 sec = 320 sec = 320_000 ms
    assert offset_ms(taken, started) == 320_000


def test_offset_ms_negative_for_pre_start_capture():
    """A screenshot taken before recording started has a negative
    offset. The caller (rail or playback layout) decides how to
    surface that case; the helper preserves the sign."""
    taken = datetime(2026, 5, 24, 14, 29, 0, tzinfo=timezone.utc)
    started = datetime(2026, 5, 24, 14, 30, 0, tzinfo=timezone.utc)
    assert offset_ms(taken, started) == -60_000


def test_screenshot_offsets_filters_and_sorts(tmp_path):
    """Mixed list of conforming + non-conforming names; sorted by offset."""
    started = "2026-05-24T14:30:00Z"
    p1 = tmp_path / "0001-20260524T143005Z.png"  # +5s
    p1.touch()
    p2 = tmp_path / "0002-20260524T143015Z.png"  # +15s
    p2.touch()
    bad = tmp_path / "stray.png"
    bad.touch()
    out = screenshot_offsets([p2, bad, p1], started)
    # Bad name dropped; conforming names sorted ascending by offset.
    assert [name.name for name, _ in out] == [p1.name, p2.name]
    assert [ms for _, ms in out] == [5_000, 15_000]


def test_screenshot_offsets_empty_when_started_at_missing(tmp_path):
    """Without a recording-start anchor, every offset is undefined --
    the whole list returns empty so the rail stays hidden."""
    p1 = tmp_path / "0001-20260524T143005Z.png"
    p1.touch()
    assert screenshot_offsets([p1], None) == []
    assert screenshot_offsets([p1], "garbage") == []


def test_match_screenshots_to_blocks_anchors_to_active_segment(tmp_path):
    """A screenshot at +12s with segments at 0s/11s/22s anchors to
    the 11s segment (it's the one being spoken when the screenshot
    was taken)."""
    p = tmp_path / "0001-20260524T143012Z.png"
    p.touch()
    screenshots = [(p, 12_000)]
    segments = [(0, 0), (11_000, 1), (22_000, 2)]
    out = match_screenshots_to_blocks(screenshots, segments)
    assert out == [(p, 1)]


def test_match_screenshots_to_blocks_pre_first_segment_anchors_to_zero(tmp_path):
    """Pre-first-segment screenshot (offset less than the first
    segment's start) anchors to block 0 so it sits at the top of
    the rail."""
    p = tmp_path / "0001-20260524T143002Z.png"
    p.touch()
    screenshots = [(p, 2_000)]
    segments = [(5_000, 7), (10_000, 9)]
    out = match_screenshots_to_blocks(screenshots, segments)
    assert out == [(p, 7)]


def test_match_screenshots_to_blocks_empty_segments_returns_empty(tmp_path):
    p = tmp_path / "0001.png"
    p.touch()
    assert match_screenshots_to_blocks([(p, 1_000)], []) == []


def test_current_screenshot_for_position_sticky_to_latest_le(tmp_path):
    """At t=20s with screenshots at 5s/15s/30s, the active one is the
    15s screenshot. At t=4s, no screenshot has been taken yet."""
    p5 = tmp_path / "0001-5s.png"
    p15 = tmp_path / "0002-15s.png"
    p30 = tmp_path / "0003-30s.png"
    for p in (p5, p15, p30):
        p.touch()
    screenshots = [(p5, 5_000), (p15, 15_000), (p30, 30_000)]
    assert current_screenshot_for_position(screenshots, 20_000) == p15
    assert current_screenshot_for_position(screenshots, 4_000) is None
    assert current_screenshot_for_position(screenshots, 5_000) == p5
    assert current_screenshot_for_position(screenshots, 999_999) == p30


def test_current_screenshot_for_position_empty_returns_none():
    assert current_screenshot_for_position([], 1_000) is None
