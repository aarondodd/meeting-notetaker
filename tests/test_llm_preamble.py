"""Tests for the LLM preamble stripper (#102 bug 8)."""
from __future__ import annotations

from meeting_notetaker.utils.llm_preamble import strip_preamble


def test_strips_let_me_research_preamble():
    src = (
        "Let me research the transcript and put together a summary.\n\n"
        "## Summary\n\n"
        "Meeting body here.\n"
    )
    out = strip_preamble(src)
    assert out.startswith("## Summary")
    assert "Let me research" not in out


def test_strips_multi_sentence_preamble():
    src = (
        "Let me analyze this. I'll look at the action items first, "
        "then walk through the decisions.\n\n"
        "## Action Items\n\n- Build it.\n"
    )
    out = strip_preamble(src)
    assert out.startswith("## Action Items")
    assert "Let me analyze" not in out


def test_strips_here_is_preamble():
    src = "Here's the synthesis:\n\n## Summary\nText.\n"
    assert strip_preamble(src).startswith("## Summary")


def test_strips_after_reviewing_preamble():
    src = "After reviewing the transcript, here's what I found:\n\n## Notes\nx\n"
    assert strip_preamble(src).startswith("## Notes")


def test_leaves_response_starting_with_heading_unchanged():
    src = "## Summary\n\nReal content.\n"
    assert strip_preamble(src) == src


def test_leaves_response_with_no_heading_unchanged():
    """Conservative: a heading-less synthesis (rare but valid) is
    kept verbatim so we don't accidentally swallow real content."""
    src = (
        "Let me work through this. The meeting decided to ship "
        "the feature on Friday and Alice owns the rollout."
    )
    assert strip_preamble(src) == src


def test_leaves_legitimate_prose_lead_unchanged():
    """A response that opens with synthesis content (no
    conversational opener) but doesn't lead with a heading should
    survive untouched."""
    src = (
        "The meeting agreed on three deliverables for Q3.\n\n"
        "## Deliverables\n\n- Foo\n"
    )
    assert strip_preamble(src) == src


def test_empty_input_returns_empty():
    assert strip_preamble("") == ""
    assert strip_preamble("   \n  \n  ") == "   \n  \n  "


def test_none_safe_via_falsy():
    # The function returns the input on falsy; pass empty string.
    assert strip_preamble("") == ""


def test_case_insensitive_preamble_match():
    src = "LET ME REVIEW the transcript.\n\n## Summary\n"
    assert strip_preamble(src).startswith("## Summary")


def test_strips_leading_whitespace_then_preamble():
    src = "\n\n   Let me look at this.\n\n## Notes\nx\n"
    out = strip_preamble(src)
    assert out.startswith("## Notes")


def test_preamble_with_inline_code_doesnt_break_stripper():
    """Backticks inside preamble shouldn't trip the heading-detection
    regex (which requires line-start hashes)."""
    src = (
        "Looking at the `Plan` section, I'll synthesize accordingly.\n\n"
        "## Plan\n\nx\n"
    )
    assert strip_preamble(src).startswith("## Plan")


def test_first_keyword_starter():
    src = "First, let me walk through the decisions.\n\n## Decisions\nx\n"
    assert strip_preamble(src).startswith("## Decisions")


def test_unrecognized_lead_preserved():
    """A lead phrase we don't recognize stays put; better to under-
    strip than to corrupt legitimate prose."""
    src = "Unexpectedly conversational opener that we don't know.\n\n## A\nx\n"
    assert strip_preamble(src) == src
