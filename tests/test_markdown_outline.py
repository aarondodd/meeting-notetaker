"""Tests for the markdown outline transforms (#92).

Pure-Python; no Qt. The transforms produce text-out from text-in, so
the tests just compare strings.
"""
from __future__ import annotations

from meeting_notetaker.utils.markdown_outline import (
    DEFAULT_TOC_MAX_DEPTH,
    TOC_HEADING,
    apply_outline,
    generate_toc,
    inject_toc,
    iter_headings,
    number_headings,
    slugify,
)


# ---- slugify ------------------------------------------------------------

def test_slugify_basic():
    assert slugify("Project Overview") == "project-overview"


def test_slugify_special_chars_collapse_to_dashes():
    assert slugify("Q3 / Q4 Planning") == "q3-q4-planning"


def test_slugify_strips_leading_and_trailing_dashes():
    assert slugify("--Foo Bar--") == "foo-bar"


def test_slugify_collapses_runs():
    assert slugify("foo   ---   bar") == "foo-bar"


def test_slugify_handles_parens():
    assert slugify("API Reference (v2)") == "api-reference-v2"


def test_slugify_numeric_prefix_kept():
    """Numbered headings should slug cleanly so the TOC + the
    anchor target line up after numbering is applied."""
    assert slugify("1.2.3 Some Heading") == "1-2-3-some-heading"


def test_slugify_empty():
    assert slugify("") == ""
    assert slugify("---") == ""


# ---- iter_headings ------------------------------------------------------

def test_iter_headings_yields_each_heading():
    src = "# A\n\n## B\n\n### C\n"
    out = list(iter_headings(src))
    assert out == [(0, 1, "A"), (2, 2, "B"), (4, 3, "C")]


def test_iter_headings_skips_fenced_code():
    src = "# Real heading\n\n```\n# Not a heading\n```\n\n## After fence\n"
    out = [(level, body) for _, level, body in iter_headings(src)]
    assert out == [(1, "Real heading"), (2, "After fence")]


def test_iter_headings_skips_tilde_fences():
    src = "# Real\n\n~~~bash\n# Code comment\n~~~\n\n# Also real\n"
    out = [(level, body) for _, level, body in iter_headings(src)]
    assert out == [(1, "Real"), (1, "Also real")]


def test_iter_headings_ignores_non_heading_lines():
    src = "regular text\n# Heading\n\nmore text\n"
    out = list(iter_headings(src))
    assert out == [(1, 1, "Heading")]


def test_iter_headings_requires_whitespace_after_hashes():
    """`#NotAHeading` is not a heading per common markdown grammar."""
    src = "#NotAHeading\n# RealHeading\n"
    out = list(iter_headings(src))
    assert out == [(1, 1, "RealHeading")]


# ---- number_headings ----------------------------------------------------

def test_number_headings_single_h1():
    assert number_headings("# Foo\n") == "# 1 Foo\n"


def test_number_headings_h1_then_h2():
    src = "# A\n## B\n## C\n"
    expected = "# 1 A\n## 1.1 B\n## 1.2 C\n"
    assert number_headings(src) == expected


def test_number_headings_resets_deeper_levels():
    src = "# A\n## A1\n### A1a\n# B\n## B1\n"
    expected = "# 1 A\n## 1.1 A1\n### 1.1.1 A1a\n# 2 B\n## 2.1 B1\n"
    assert number_headings(src) == expected


def test_number_headings_skipped_level_collapses():
    """H1 -> H3 without H2: prefix shouldn't have an awkward `1.0.1`."""
    src = "# A\n### Deep\n"
    out = number_headings(src)
    # The collapse drops missing levels: "1 A" then "1.1 Deep" (H3 still
    # increments its own slot but the suppressed zero from H2 is removed).
    assert "# 1 A" in out
    # The H3 prefix doesn't contain `0`.
    assert "0" not in out.split("Deep")[0]


def test_number_headings_skips_fenced_code():
    src = "# Real\n\n```\n# Not a heading\n```\n\n# Also real\n"
    out = number_headings(src)
    assert "# 1 Real" in out
    assert "# 2 Also real" in out
    # The fence body is unmodified.
    assert "# Not a heading" in out


def test_number_headings_preserves_blank_lines():
    src = "# A\n\n\n## B\n"
    out = number_headings(src)
    assert out == "# 1 A\n\n\n## 1.1 B\n"


def test_number_headings_preserves_crlf():
    src = "# A\r\n## B\r\n"
    out = number_headings(src)
    assert "\r\n" in out


def test_number_headings_empty_body_skipped():
    """A bare `##` with no body text doesn't get a number prefix."""
    src = "# Real\n##\n"
    out = number_headings(src)
    # First heading numbered; the empty one is left untouched.
    assert out.startswith("# 1 Real\n")


def test_number_headings_empty_input():
    assert number_headings("") == ""


def test_number_headings_no_headings():
    src = "Just some text\n\nMore text.\n"
    assert number_headings(src) == src


# ---- generate_toc -------------------------------------------------------

def test_generate_toc_basic():
    src = "# A\n## B\n### C\n"
    out = generate_toc(src)
    assert TOC_HEADING in out
    assert "- [A](#a)" in out
    assert "  - [B](#b)" in out
    assert "    - [C](#c)" in out


def test_generate_toc_returns_empty_when_no_headings():
    assert generate_toc("Just text\n") == ""


def test_generate_toc_respects_max_depth():
    src = "# A\n## B\n### C\n#### D\n"
    out = generate_toc(src, max_depth=2)
    assert "[A]" in out
    assert "[B]" in out
    assert "[C]" not in out
    assert "[D]" not in out


def test_generate_toc_default_max_depth_is_three():
    """Pinned so a doc author can rely on default behavior."""
    assert DEFAULT_TOC_MAX_DEPTH == 3


def test_generate_toc_skips_fenced_code():
    src = "# Real\n\n```\n# Not real\n```\n"
    out = generate_toc(src)
    assert "[Real]" in out
    assert "[Not real]" not in out


def test_generate_toc_indents_by_level():
    src = "## H2\n### H3\n"
    out = generate_toc(src)
    # H2 starts the outline at indent=1*2 spaces.
    assert "  - [H2](#h2)" in out
    assert "    - [H3](#h3)" in out


def test_generate_toc_includes_separator():
    out = generate_toc("# A\n")
    assert "---" in out


# ---- inject_toc ---------------------------------------------------------

def test_inject_toc_prepends_block():
    src = "# Body\n"
    out = inject_toc(src)
    assert out.startswith(TOC_HEADING)
    assert out.endswith(src)


def test_inject_toc_is_no_op_when_no_headings():
    src = "Just text\n"
    assert inject_toc(src) == src


# ---- apply_outline ------------------------------------------------------

def test_apply_outline_both_off_is_identity():
    src = "# Foo\n## Bar\n"
    assert apply_outline(src, number=False, toc=False) == src


def test_apply_outline_numbering_only():
    src = "# Foo\n## Bar\n"
    out = apply_outline(src, number=True, toc=False)
    assert "# 1 Foo" in out
    assert TOC_HEADING not in out


def test_apply_outline_toc_only():
    src = "# Foo\n## Bar\n"
    out = apply_outline(src, number=False, toc=True)
    assert TOC_HEADING in out
    # Body headings unnumbered.
    assert "# Foo" in out and "# 1 Foo" not in out


def test_apply_outline_both_numbers_first_so_toc_carries_numbers():
    """Numbering must run before TOC so the TOC's link text already
    carries the prefix (matters for visual consistency + so the
    anchor's slug matches the numbered heading's auto-anchor)."""
    src = "# Foo\n## Bar\n"
    out = apply_outline(src, number=True, toc=True)
    # The TOC entry text carries the number.
    assert "- [1 Foo]" in out
    assert "  - [1.1 Bar]" in out


def test_apply_outline_toc_link_matches_numbered_anchor_slug():
    """The TOC entry's anchor must slugify to the same string the
    rendered numbered heading produces; otherwise click-to-navigate
    breaks across all consumers (Preview, PDF, Confluence)."""
    src = "# Project Overview\n## Goals\n"
    out = apply_outline(src, number=True, toc=True)
    assert "[1 Project Overview](#1-project-overview)" in out
    assert "[1.1 Goals](#1-1-goals)" in out


# ---- combined regression ------------------------------------------------

# ---- skip_h1 (numbering) ------------------------------------------------

def test_number_headings_skip_h1_leaves_h1_alone():
    """With skip_h1, H1 stays unnumbered and the H2 below becomes
    the top-level counter slot."""
    src = "# Doc Title\n## Goals\n## Non-Goals\n"
    out = number_headings(src, skip_h1=True)
    assert "# Doc Title" in out
    assert "## 1 Goals" in out
    assert "## 2 Non-Goals" in out


def test_number_headings_skip_h1_h2_becomes_top_level():
    """H2 with skip_h1 = top-level counter; H3 becomes "N.M"."""
    src = "## Goals\n### Phase 1\n### Phase 2\n## Non-Goals\n"
    out = number_headings(src, skip_h1=True)
    assert "## 1 Goals" in out
    assert "### 1.1 Phase 1" in out
    assert "### 1.2 Phase 2" in out
    assert "## 2 Non-Goals" in out


def test_number_headings_skip_h1_continues_across_multiple_h1():
    """Multiple H1s with skip_h1: deeper-level counters keep
    continuing (H1 boundaries are decorative, not section
    separators). Users who want per-section restart shouldn't
    skip H1."""
    src = "# Section A\n## Foo\n# Section B\n## Bar\n"
    out = number_headings(src, skip_h1=True)
    assert "# Section A" in out
    assert "# Section B" in out
    assert "## 1 Foo" in out
    # Bar continues at 2, not back to 1 -- decorative H1s.
    assert "## 2 Bar" in out


# ---- skip_h1 (TOC) -------------------------------------------------------

def test_generate_toc_skip_h1_omits_h1():
    src = "# Title\n## Goals\n### Phase 1\n"
    out = generate_toc(src, skip_h1=True)
    assert "[Title]" not in out
    assert "[Goals]" in out
    assert "[Phase 1]" in out


def test_generate_toc_skip_h1_indent_starts_at_h2():
    """With skip_h1, the H2 entry has no indent (it's the top level
    of the visible outline). H3 has one level of indent."""
    src = "# Title\n## Goals\n### Phase\n"
    out = generate_toc(src, skip_h1=True)
    assert "- [Goals]" in out  # no leading whitespace
    assert "  - [Phase]" in out  # one level of indent


def test_generate_toc_max_depth_with_skip_h1():
    """max_depth applies to the post-skip level so max_depth=2 with
    skip_h1 means H2 + H3."""
    src = "# Title\n## A\n### B\n#### C\n"
    out = generate_toc(src, max_depth=2, skip_h1=True)
    assert "[A]" in out
    assert "[B]" in out
    assert "[C]" not in out


# ---- max_depth -----------------------------------------------------------

def test_generate_toc_max_depth_one_includes_only_top_level():
    src = "# A\n## B\n### C\n"
    out = generate_toc(src, max_depth=1)
    assert "[A]" in out
    assert "[B]" not in out
    assert "[C]" not in out


def test_generate_toc_max_depth_six_includes_everything():
    src = "# H1\n## H2\n### H3\n#### H4\n##### H5\n###### H6\n"
    out = generate_toc(src, max_depth=6)
    for tag in ("H1", "H2", "H3", "H4", "H5", "H6"):
        assert f"[{tag}]" in out


def test_generate_toc_max_depth_zero_returns_empty():
    out = generate_toc("# A\n## B\n", max_depth=0)
    assert out == ""


# ---- apply_outline forwards both new kwargs -----------------------------

def test_apply_outline_skip_h1_routes_through_to_both_transforms():
    src = "# Title\n## Goals\n## Non-Goals\n"
    out = apply_outline(src, number=True, toc=True, skip_h1=True)
    # H1 untouched.
    assert "# Title" in out
    # Numbered H2 entries.
    assert "## 1 Goals" in out
    # TOC entry for Goals exists.
    assert "[1 Goals]" in out
    # No TOC entry for the title.
    assert "[Title]" not in out


def test_apply_outline_max_depth_threads_through():
    src = "# A\n## B\n### C\n"
    out = apply_outline(src, toc=True, max_depth=2)
    assert "[A]" in out
    assert "[B]" in out
    assert "[C]" not in out


def test_outline_round_trip_with_complex_doc():
    src = (
        "# Project Overview\n"
        "\n"
        "## Goals\n"
        "\n"
        "## Non-Goals\n"
        "\n"
        "# Architecture\n"
        "\n"
        "## Storage\n"
        "\n"
        "### SQLite\n"
        "\n"
        "### Files\n"
        "\n"
        "## Compute\n"
        "\n"
        "```python\n"
        "# This is code, not a heading\n"
        "def foo(): pass\n"
        "```\n"
        "\n"
        "# Conclusion\n"
    )
    out = apply_outline(src, number=True, toc=True)
    # Numbering applied to real headings.
    assert "# 1 Project Overview" in out
    assert "## 1.1 Goals" in out
    assert "## 1.2 Non-Goals" in out
    assert "# 2 Architecture" in out
    assert "## 2.1 Storage" in out
    assert "### 2.1.1 SQLite" in out
    assert "### 2.1.2 Files" in out
    assert "## 2.2 Compute" in out
    assert "# 3 Conclusion" in out
    # Fence body untouched.
    assert "# This is code, not a heading" in out
    # TOC at top + separator.
    assert out.find(TOC_HEADING) < out.find("# 1 Project Overview")
    assert "---" in out
