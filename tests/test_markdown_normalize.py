"""Tighten the loose-list markdown Claude.ai's Copy button writes.

Issue #42: the synthesis pulled into the app's Synthesis pane carries
extra blank lines between every bullet and 2+ blank lines between
sections. A user who manually selects + Ctrl+C's the same response
gets tight markdown. The post-clipboard normalizer in
``meeting_notetaker.automation.markdown_normalize`` collapses the
loose form into the tight form so the saved notes.md matches the
user's expectation.
"""
from __future__ import annotations

from meeting_notetaker.automation.markdown_normalize import (
    normalize_synthesis_markdown,
)


def test_collapses_aarons_reproduction():
    """The exact before/after Aaron pasted in the bug report (#42)."""
    loose = (
        "# Agenda\n"
        "\n"
        "- How to delete code locations in Dagster Cloud UI (possible permissions issue)\n"
        "\n"
        "- AWS Secrets setup -- AWS CLI installed, next steps unclear\n"
        "\n"
        "- Whether a dev branch merge is required before testing, or if local testing on PC is possible\n"
        "\n"
        "\n"
        "\n"
        "# TL;DR\n"
        "\n"
        "Ledward got Dagster installed and running locally.\n"
    )
    tight = (
        "# Agenda\n"
        "- How to delete code locations in Dagster Cloud UI (possible permissions issue)\n"
        "- AWS Secrets setup -- AWS CLI installed, next steps unclear\n"
        "- Whether a dev branch merge is required before testing, or if local testing on PC is possible\n"
        "\n"
        "# TL;DR\n"
        "\n"
        "Ledward got Dagster installed and running locally.\n"
    )
    assert normalize_synthesis_markdown(loose) == tight


def test_idempotent_on_already_tight_markdown():
    """Running the normalizer twice (or on a clean input) is a no-op."""
    tight = (
        "# Attendees\n"
        "- Ledward Kalani\n"
        "- Aaron Dodd\n"
        "\n"
        "# Agenda\n"
        "- one\n"
        "- two\n"
    )
    once = normalize_synthesis_markdown(tight)
    twice = normalize_synthesis_markdown(once)
    assert once == tight
    assert twice == tight


def test_preserves_blank_line_between_list_and_paragraph():
    """Don't collapse the blank line that separates a list from prose --
    that one IS meaningful (without it, CommonMark merges the next
    paragraph into the last list item)."""
    src = (
        "- a\n"
        "- b\n"
        "\n"
        "This is a paragraph after the list.\n"
    )
    assert normalize_synthesis_markdown(src) == src


def test_preserves_blank_line_between_paragraph_and_list():
    src = (
        "Some prose.\n"
        "\n"
        "- a\n"
        "- b\n"
    )
    assert normalize_synthesis_markdown(src) == src


def test_handles_asterisk_and_plus_bullets():
    """CommonMark accepts -, *, + interchangeably. All three should
    get the same tight-list treatment."""
    src = (
        "* one\n"
        "\n"
        "* two\n"
        "\n"
        "+ three\n"
        "\n"
        "+ four\n"
    )
    expected = (
        "* one\n"
        "* two\n"
        "+ three\n"
        "+ four\n"
    )
    assert normalize_synthesis_markdown(src) == expected


def test_does_not_touch_mid_paragraph_dashes():
    """Em-dash-style separators inside prose are not list items and
    must not be collapsed against surrounding blank lines."""
    src = (
        "Aaron - aka the user.\n"
        "\n"
        "Another paragraph.\n"
    )
    assert normalize_synthesis_markdown(src) == src


def test_collapses_three_or_more_blank_lines_between_sections():
    src = (
        "# A\n"
        "Text.\n"
        "\n"
        "\n"
        "\n"
        "# B\n"
        "More text.\n"
    )
    expected = (
        "# A\n"
        "Text.\n"
        "\n"
        "# B\n"
        "More text.\n"
    )
    assert normalize_synthesis_markdown(src) == expected


def test_empty_input_returns_empty():
    assert normalize_synthesis_markdown("") == ""


def test_preserves_trailing_newline():
    """splitlines drops a trailing newline; the normalizer must put
    it back so round-trips don't strip the final \\n that downstream
    file writers expect."""
    assert normalize_synthesis_markdown("# A\n") == "# A\n"
    # And conversely, no trailing newline in -> none out.
    assert normalize_synthesis_markdown("# A") == "# A"


def test_handles_short_input_that_could_index_out_of_bounds():
    """Two-line input where line[i+2] would be out of bounds.
    Regression guard for the lookahead in the tight-list pass."""
    assert normalize_synthesis_markdown("- a\n- b") == "- a\n- b"
    assert normalize_synthesis_markdown("- a\n\n- b") == "- a\n- b"


def test_consecutive_bullet_runs_with_section_break():
    """Multiple bullet runs separated by a section header. The blank
    lines around the header survive; the blanks between bullets
    within each run get tightened."""
    src = (
        "# First\n"
        "- a\n"
        "\n"
        "- b\n"
        "\n"
        "# Second\n"
        "- c\n"
        "\n"
        "- d\n"
    )
    expected = (
        "# First\n"
        "- a\n"
        "- b\n"
        "\n"
        "# Second\n"
        "- c\n"
        "- d\n"
    )
    assert normalize_synthesis_markdown(src) == expected
