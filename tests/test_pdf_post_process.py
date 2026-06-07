"""Tests for the PDF post-processor (#94 robust fix).

The body-side helpers are unit-tested directly. End-to-end PDF
generation + post-processing is exercised by integration tests that
need PyQt + the print path, gated to skip cleanly when those aren't
available.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

from meeting_notetaker.utils.pdf_post_process import (
    _bbox_for_text,
    _collect_body_headings,
    _collect_toc_entries,
    _normalize_for_match,
)


# ---- markdown parsing ---------------------------------------------------

def test_collect_body_headings_ordered():
    src = "# A\n\n## B\n\n# C\n"
    out = _collect_body_headings(src, max_depth=3)
    assert out == ["A", "B", "C"]


def test_collect_body_headings_skips_contents_block():
    """The ## Contents heading + the auto-generated TOC list don't
    belong in the body-heading outline (they ARE the TOC)."""
    src = (
        "## Contents\n\n"
        "- [A](#a)\n"
        "- [B](#b)\n\n"
        "---\n\n"
        "# A\n\nbody\n\n# B\nbody\n"
    )
    out = _collect_body_headings(src, max_depth=3)
    assert "Contents" not in out
    assert out == ["A", "B"]


def test_collect_body_headings_respects_max_depth():
    src = "# A\n## B\n### C\n#### D\n"
    out = _collect_body_headings(src, max_depth=2)
    assert out == ["A", "B"]


def test_collect_toc_entries_from_anchor_links():
    """Any `[text](#slug)` link in the source qualifies. In practice
    only the TOC list contains these patterns."""
    src = (
        "## Contents\n\n"
        "- [1 Intro](#1-intro)\n"
        "- [2 Body](#2-body)\n"
    )
    out = _collect_toc_entries(src, max_depth=3)
    assert out == ["1 Intro", "2 Body"]


def test_collect_toc_entries_dedupes():
    """If the same anchor link text appears twice, only the first
    is kept -- the second would overlap-link to the same destination
    and adds no value."""
    src = "[A](#a) ... [A](#a)\n"
    out = _collect_toc_entries(src, max_depth=3)
    assert out == ["A"]


def test_collect_toc_entries_empty_when_no_links():
    assert _collect_toc_entries("# Title\n\nbody\n", max_depth=3) == []


# ---- _normalize_for_match -----------------------------------------------

def test_normalize_collapses_whitespace():
    """PDF text extraction frequently emits tabs and newlines
    between words. Match-side normalization handles them."""
    assert _normalize_for_match("1\tIntroduction") == "1 introduction"
    assert _normalize_for_match("  1   Introduction  ") == "1 introduction"


def test_normalize_casefolds():
    assert _normalize_for_match("INTRO") == _normalize_for_match("intro")


def test_normalize_empty():
    assert _normalize_for_match("") == ""
    assert _normalize_for_match(None) == ""  # type: ignore[arg-type]


# ---- _bbox_for_text -----------------------------------------------------

def test_bbox_single_run_exact_match():
    """A single text run whose normalized content contains the
    target yields a bbox based on that run's position."""
    runs = [("1\tIntroduction", 100.0, 200.0, 50.0, 12.0)]
    bbox = _bbox_for_text(runs, "1 Introduction")
    assert bbox == (100.0, 200.0, 150.0, 200.0 + 12.0 * 1.2)


def test_bbox_match_across_runs_on_same_line():
    """Two adjacent runs on the same y position concatenate to form
    the match. Bbox covers their combined extent."""
    runs = [
        ("Hello ", 100.0, 200.0, 30.0, 12.0),
        ("World", 130.0, 200.0, 25.0, 12.0),
    ]
    bbox = _bbox_for_text(runs, "Hello World")
    assert bbox is not None
    x1, y1, x2, y2 = bbox
    assert x1 == 100.0
    assert x2 == pytest.approx(155.0)


def test_bbox_returns_none_when_no_match():
    runs = [("Goodbye", 0.0, 0.0, 10.0, 12.0)]
    assert _bbox_for_text(runs, "Hello") is None


def test_bbox_returns_none_for_empty_inputs():
    assert _bbox_for_text([], "anything") is None
    assert _bbox_for_text([("foo", 0, 0, 1, 1)], "") is None


def test_bbox_normalizes_whitespace_in_target():
    """Source target like '1 Intro' matches a PDF run rendered as
    '1\\tIntro' (tab between words)."""
    runs = [("1\tIntro", 100.0, 200.0, 50.0, 12.0)]
    assert _bbox_for_text(runs, "1 Intro") is not None


# ---- end-to-end (requires Qt for PDF generation) ------------------------

# Skip if Qt isn't available. Even when it is, the PDF write requires
# a QApplication so the test pins that explicitly.

pytest_qt6 = pytest.importorskip("PyQt6")


def _has_qprinter() -> bool:
    try:
        from PyQt6.QtPrintSupport import QPrinter  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _has_qprinter(), reason="QPrinter not available")
def test_end_to_end_adds_outline_and_links():
    """Render a small PDF via Qt, post-process, verify pypdf reports
    outline items + link annotations."""
    from PyQt6.QtPrintSupport import QPrinter
    from PyQt6.QtWidgets import QApplication

    import pypdf

    from meeting_notetaker.ui.print_document import PrintTextDocument
    from meeting_notetaker.utils.pdf_post_process import add_pdf_navigation
    from meeting_notetaker.utils.print_html import markdown_to_print_html

    app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841

    src = (
        "## Contents\n\n"
        "- [1 Introduction](#1-introduction)\n"
        "- [2 Architecture](#2-architecture)\n\n"
        "---\n\n"
        "# 1 Introduction\n\n"
        + ("Intro body. " * 80)
        + "\n\n"
        "# 2 Architecture\n\n"
        + ("Arch body. " * 80)
        + "\n"
    )

    with tempfile.TemporaryDirectory() as td:
        out_path = Path(td) / "test.pdf"
        doc = PrintTextDocument(Path(td))
        doc.setHtml(markdown_to_print_html(src))
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer.setOutputFileName(str(out_path))
        doc.print(printer)
        stats = add_pdf_navigation(out_path, src)
        assert stats["error"] is None
        # Two body headings -> two outline items.
        assert stats["outline_added"] == 2
        # Two TOC entries -> two link annotations.
        assert stats["links_added"] == 2

        # Verify the post-processed PDF actually carries the new
        # structures.
        reader = pypdf.PdfReader(str(out_path))
        outline_titles = [
            getattr(item, "title", "") for item in reader.outline
        ]
        assert "1 Introduction" in outline_titles
        assert "2 Architecture" in outline_titles

        # Page 0 should carry the new Link annotations (in addition
        # to whatever Qt wrote).
        page0 = reader.pages[0]
        annots = page0.get("/Annots") or []
        link_count = 0
        for ref in annots:
            obj = ref.get_object()
            if obj.get("/Subtype") == "/Link":
                link_count += 1
        # Qt writes its own (non-navigable) link annotations for the
        # markdown link text; we add navigable Link annotations on
        # top. Either way, at least 2 should exist after the
        # post-process (one per TOC entry we added).
        assert link_count >= 2
