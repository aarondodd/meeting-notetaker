"""Highlights export plan + SRT remap.

The encoder paths (PyAV, PIL) are tested live on Windows in the
release pipeline; the load-bearing pure-Python piece is the timeline
planner + the transcript->concatenated-timeline remap. Those produce
the (segment kind, start, end) tuples every encoder downstream
depends on.
"""
from __future__ import annotations

import pytest

from meeting_notetaker.audio.highlights_export import (
    DEFAULT_JUMP_INTERSTITIAL_MS,
    DEFAULT_TITLE_INTERSTITIAL_MS,
    SEGMENT_HIGHLIGHT,
    SEGMENT_JUMP,
    SEGMENT_TITLE,
    format_srt,
    plan_highlight_timeline,
    remap_transcript_to_highlights,
    total_output_duration_ms,
)
from meeting_notetaker.models.highlights import Highlight


# ---- plan_highlight_timeline (video mode) -----------------------------


def test_plan_video_single_highlight_has_title_card_then_segment():
    plan = plan_highlight_timeline(
        [Highlight(10_000, 20_000, "Decision on MDM")],
        mode="video",
    )
    kinds = [s.kind for s in plan]
    assert kinds == [SEGMENT_TITLE, SEGMENT_HIGHLIGHT]
    assert plan[0].duration_ms == DEFAULT_TITLE_INTERSTITIAL_MS
    assert plan[0].label == "Decision on MDM"
    assert plan[1].duration_ms == 10_000


def test_plan_video_untitled_highlight_falls_back_to_index_label():
    plan = plan_highlight_timeline(
        [Highlight(0, 1000)],
        mode="video",
    )
    assert plan[0].label == "Highlight 1"


def test_plan_video_two_highlights_interleaves_title_jump_title():
    h1 = Highlight(10_000, 15_000, "First")
    h2 = Highlight(30_000, 32_000, "Second")
    plan = plan_highlight_timeline([h1, h2], mode="video")
    kinds = [s.kind for s in plan]
    assert kinds == [
        SEGMENT_TITLE,     # before highlight 1
        SEGMENT_HIGHLIGHT, # highlight 1
        SEGMENT_JUMP,      # cut to highlight 2
        SEGMENT_TITLE,     # before highlight 2
        SEGMENT_HIGHLIGHT, # highlight 2
    ]


def test_plan_video_jump_card_shows_destination_mmss():
    h1 = Highlight(0, 5_000, "A")
    h2 = Highlight(125_000, 130_000, "B")
    plan = plan_highlight_timeline([h1, h2], mode="video")
    jump_seg = [s for s in plan if s.kind == SEGMENT_JUMP][0]
    assert "02:05" in jump_seg.label


def test_plan_video_jump_card_shows_hours_for_long_sessions():
    h1 = Highlight(0, 5_000, "A")
    h2 = Highlight(3_725_000, 3_730_000, "B")  # 1:02:05
    plan = plan_highlight_timeline([h1, h2], mode="video")
    jump_seg = [s for s in plan if s.kind == SEGMENT_JUMP][0]
    assert "01:02:05" in jump_seg.label


def test_plan_video_output_timeline_monotonic():
    h1 = Highlight(0, 5_000, "A")
    h2 = Highlight(20_000, 25_000, "B")
    h3 = Highlight(60_000, 65_000, "C")
    plan = plan_highlight_timeline([h1, h2, h3], mode="video")
    # Each segment starts exactly where the previous one ended.
    for prev, curr in zip(plan, plan[1:]):
        assert curr.output_start_ms == prev.output_end_ms


def test_plan_handles_unsorted_input():
    """Issue #26's bar widget keeps highlights in user-insertion
    order; the planner has to time-sort them itself."""
    later = Highlight(50_000, 55_000, "Later")
    earlier = Highlight(10_000, 15_000, "Earlier")
    plan = plan_highlight_timeline([later, earlier], mode="video")
    highlight_segments = [s for s in plan if s.kind == SEGMENT_HIGHLIGHT]
    assert highlight_segments[0].source_start_ms < highlight_segments[1].source_start_ms


def test_plan_empty_highlights_returns_empty():
    assert plan_highlight_timeline([], mode="video") == []
    assert plan_highlight_timeline([], mode="audio") == []


# ---- plan_highlight_timeline (audio mode) -----------------------------


def test_plan_audio_skips_title_cards():
    """Audio-only has no surface to render text on, so just clip +
    silent gaps."""
    plan = plan_highlight_timeline(
        [Highlight(0, 1000, "A"), Highlight(2000, 3000, "B")],
        mode="audio",
        audio_gap_ms=500,
    )
    kinds = [s.kind for s in plan]
    assert kinds == [SEGMENT_HIGHLIGHT, SEGMENT_JUMP, SEGMENT_HIGHLIGHT]
    # Total output = h1 + gap + h2 = 1000 + 500 + 1000.
    assert total_output_duration_ms(plan) == 2500


def test_plan_audio_no_leading_gap():
    """First highlight starts at output_ms=0; no silent prefix."""
    plan = plan_highlight_timeline(
        [Highlight(0, 1000)], mode="audio",
    )
    assert plan[0].output_start_ms == 0
    assert plan[0].kind == SEGMENT_HIGHLIGHT


def test_plan_unknown_mode_raises():
    with pytest.raises(ValueError):
        plan_highlight_timeline([Highlight(0, 1000)], mode="foo")


# ---- total_output_duration_ms -----------------------------------------


def test_total_output_duration_sums_segments():
    plan = plan_highlight_timeline(
        [Highlight(0, 5_000, "A"), Highlight(10_000, 12_000, "B")],
        mode="video",
        title_interstitial_ms=2000,
        jump_interstitial_ms=2000,
    )
    # title(2) + h1(5) + jump(2) + title(2) + h2(2) = 13s
    assert total_output_duration_ms(plan) == 13_000


def test_total_output_duration_empty_plan():
    assert total_output_duration_ms([]) == 0


# ---- transcript remap -------------------------------------------------


def _video_plan(highlights):
    return plan_highlight_timeline(
        highlights,
        mode="video",
        title_interstitial_ms=2000,
        jump_interstitial_ms=2000,
    )


def test_remap_transcript_keeps_in_window_cues():
    transcript = (
        "[00:00:05] Alice: introduction.\n"
        "[00:00:12] Bob: response.\n"
        "[00:01:00] Alice: discussion.\n"
    )
    # Highlight covers 00:00:10 - 00:00:20 of source -- only Bob's line
    # falls inside.
    plan = _video_plan([Highlight(10_000, 20_000, "")])
    cues = remap_transcript_to_highlights(transcript, plan)
    assert len(cues) == 1
    assert cues[0][2] == "Bob: response."


def test_remap_transcript_offsets_into_output_timeline():
    """A transcript line at source 00:00:15 inside a highlight
    starting at source 00:00:10 should land at
    (highlight's output start + 5s) in the new timeline. With the
    2s title card preceding the highlight, that's title(2000) +
    5000 = 7000."""
    transcript = "[00:00:15] Bob: hi.\n"
    plan = _video_plan([Highlight(10_000, 20_000, "")])
    cues = remap_transcript_to_highlights(transcript, plan)
    # title interstitial = 2000ms; offset inside highlight = 5000ms.
    assert cues[0][0] == 2000 + 5000


def test_remap_transcript_drops_out_of_window_cues():
    transcript = (
        "[00:00:05] Alice: before.\n"
        "[00:01:00] Alice: between.\n"
        "[00:05:00] Alice: after.\n"
    )
    plan = _video_plan([
        Highlight(15_000, 25_000, "A"),
        Highlight(120_000, 130_000, "B"),
    ])
    cues = remap_transcript_to_highlights(transcript, plan)
    # None of the three lines falls inside either highlight.
    assert cues == []


def test_remap_transcript_empty_inputs():
    assert remap_transcript_to_highlights("", []) == []
    assert remap_transcript_to_highlights("[00:00:05] x", []) == []


# ---- format_srt -------------------------------------------------------


def test_format_srt_basic():
    cues = [(0, 2000, "first cue"), (3000, 5000, "second cue")]
    out = format_srt(cues)
    assert "1" in out
    assert "00:00:00,000 --> 00:00:02,000" in out
    assert "first cue" in out
    assert "00:00:03,000 --> 00:00:05,000" in out
    assert "second cue" in out


def test_format_srt_empty_input_returns_empty_string():
    assert format_srt([]) == ""


def test_format_srt_handles_long_durations():
    """Hours-mm-ss formatting works past the 1-hour boundary."""
    cues = [(3_661_500, 3_665_000, "after an hour")]
    out = format_srt(cues)
    assert "01:01:01,500 --> 01:01:05,000" in out
