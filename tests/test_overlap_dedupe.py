"""dedupe_overlap edge cases."""
from __future__ import annotations

from meeting_notetaker.audio.chunk_buffer import dedupe_overlap


def test_exact_tail_prefix_match_is_trimmed():
    assert (
        dedupe_overlap(
            "hello world from the meeting",
            "from the meeting we should ship by friday",
        )
        == "we should ship by friday"
    )


def test_partial_overlap_picks_longest_match():
    # "the meeting" matches; "from the meeting" doesn't (only 3 prev tokens overlap).
    assert (
        dedupe_overlap(
            "we talked about the meeting",
            "the meeting was helpful",
        )
        == "was helpful"
    )


def test_no_match_returns_current_unchanged():
    assert (
        dedupe_overlap(
            "completely unrelated previous chunk",
            "fresh text without any overlap",
        )
        == "fresh text without any overlap"
    )


def test_case_insensitive_match():
    assert (
        dedupe_overlap(
            "we will SHIP this WEEK",
            "ship this week and call it done",
        )
        == "and call it done"
    )


def test_below_min_match_falls_through():
    # Only 1 token overlap, default min_match=2 -> no trim
    assert (
        dedupe_overlap("first sentence", "sentence two extends it")
        == "sentence two extends it"
    )


def test_empty_inputs_safe():
    assert dedupe_overlap("", "hello") == "hello"
    assert dedupe_overlap("hello", "") == ""
