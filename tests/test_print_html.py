"""Tests for the print-html conversion (#94 fallback).

Pure-Python; mistune-based. Verifies that the HTML output mistune
produces carries the anchor structure Qt's setHtml -> PDF path
needs to emit clickable TOC links in the rendered PDF.
"""
from __future__ import annotations

from meeting_notetaker.utils.print_html import markdown_to_print_html


# ---- anchor injection ---------------------------------------------------

def test_heading_anchor_emitted_before_heading():
    out = markdown_to_print_html("# Project Overview\n")
    # Anchor sits immediately before the heading tag so Qt's PDF
    # writer carries it as a named destination at the heading's
    # position in the rendered document.
    assert out.startswith('<a name="project-overview"></a><h1>')


def test_heading_anchor_slug_matches_outline_slug():
    """The slug must match what `slugify` produces from the outline
    module, otherwise the TOC links don't resolve."""
    from meeting_notetaker.utils.markdown_outline import slugify
    out = markdown_to_print_html("# Q3 / Q4 Planning\n")
    expected_slug = slugify("Q3 / Q4 Planning")
    assert f'<a name="{expected_slug}"></a>' in out
    assert '<a name="q3-q4-planning"></a>' in out


def test_numbered_heading_anchor_uses_numbered_slug():
    """When `apply_outline` has numbered the heading, the slug
    includes the prefix. The print-html anchor must include it too."""
    out = markdown_to_print_html("# 1.2.3 Some Heading\n")
    assert '<a name="1-2-3-some-heading"></a>' in out


def test_multiple_heading_levels_each_get_anchors():
    out = markdown_to_print_html("# A\n\n## B\n\n### C\n")
    assert '<a name="a"></a><h1>A</h1>' in out
    assert '<a name="b"></a><h2>B</h2>' in out
    assert '<a name="c"></a><h3>C</h3>' in out


def test_anchor_injection_can_be_disabled():
    """The toggle exists for callers who explicitly want plain
    HTML (e.g. a non-PDF preview path) but the PDF render path
    always passes the default True."""
    out = markdown_to_print_html("# A\n", anchor_headings=False)
    assert '<a name="a"></a>' not in out
    assert '<h1>A</h1>' in out


# ---- TOC link round-trip -----------------------------------------------

def test_toc_internal_link_carries_through_as_href():
    """The TOC's `[text](#slug)` markdown link must end up as an
    `<a href="#slug">text</a>` in the HTML so Qt's PDF writer
    creates a clickable link annotation pointing at the named
    destination."""
    src = (
        "## Contents\n\n"
        "- [1 Intro](#1-intro)\n"
        "- [2 Body](#2-body)\n\n"
        "---\n\n"
        "# 1 Intro\n\nIntro body.\n\n"
        "# 2 Body\n\nBody text.\n"
    )
    out = markdown_to_print_html(src)
    # Link in TOC list.
    assert '<a href="#1-intro">1 Intro</a>' in out
    # Anchor at heading.
    assert '<a name="1-intro"></a><h1>1 Intro</h1>' in out


# ---- markdown surface compatibility -------------------------------------

def test_basic_paragraph_renders():
    out = markdown_to_print_html("plain paragraph\n")
    assert "<p>plain paragraph</p>" in out


def test_bullet_list_renders():
    out = markdown_to_print_html("- one\n- two\n- three\n")
    assert "<ul>" in out
    assert "<li>one</li>" in out


def test_code_fence_renders_as_pre_code():
    out = markdown_to_print_html("```python\nprint('hi')\n```\n")
    assert "<pre>" in out
    assert "<code" in out


def test_image_tag_renders():
    out = markdown_to_print_html("![alt](images/foo.png)\n")
    assert '<img src="images/foo.png"' in out
    assert 'alt="alt"' in out


def test_table_renders():
    out = markdown_to_print_html(
        "| Col1 | Col2 |\n|------|------|\n| a | b |\n",
    )
    assert "<table>" in out
    assert "<th>Col1</th>" in out


def test_strikethrough_renders():
    out = markdown_to_print_html("~~gone~~\n")
    assert "<del>gone</del>" in out


def test_empty_input_returns_empty():
    assert markdown_to_print_html("") == ""


# ---- heading text with inline formatting -------------------------------

def test_slug_strips_inline_html_from_heading_text():
    """A heading with bold text should slugify the bold-stripped
    text, not the rendered HTML."""
    out = markdown_to_print_html("# **Bold** Heading\n")
    # Slug from "Bold Heading", not from the HTML-rendered form.
    assert '<a name="bold-heading"></a>' in out
