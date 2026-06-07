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


def _has_pdfium() -> bool:
    try:
        import pypdfium2  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(
    not (_has_qprinter() and _has_pdfium()),
    reason="QPrinter or pypdfium2 not available",
)
def test_end_to_end_links_actually_navigate_to_target_headings():
    """The acceptance test: render a PDF, post-process, then for each
    TOC link follow its destination and verify the text immediately
    AT the destination y on the destination page is the target
    heading.

    Earlier iterations only checked that ``FPDFLink_GetDest`` returned
    a non-null destination -- which it did, even when the destination
    pointed at "page 0 top" (the TOC itself), producing a "link
    clickable but click does nothing" symptom in real viewers. This
    test follows the link the same way Chrome / Edge / Acrobat do
    and asserts the destination lands on the target heading text.
    """
    import ctypes

    from PyQt6.QtPrintSupport import QPrinter
    from PyQt6.QtWidgets import QApplication
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_raw

    from meeting_notetaker.ui.print_document import PrintTextDocument
    from meeting_notetaker.utils.pdf_post_process import add_pdf_navigation
    from meeting_notetaker.utils.print_html import markdown_to_print_html

    app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841

    # Three headings spread over enough body to force a multi-page
    # layout. Verifies cross-page navigation AND same-page mid-page
    # navigation (the first heading sits right after the TOC, so
    # its destination is page 0 but well below the TOC).
    src = (
        "## Contents\n\n"
        "- [1 Introduction](#1-introduction)\n"
        "- [2 Architecture](#2-architecture)\n"
        "- [3 Results](#3-results)\n\n"
        "---\n\n"
        "# 1 Introduction\n\n"
        + ("Intro body paragraph text. " * 300)
        + "\n\n"
        "# 2 Architecture\n\n"
        + ("Architecture body paragraph text. " * 300)
        + "\n\n"
        "# 3 Results\n\n"
        + ("Results body paragraph text. " * 200)
        + "\n"
    )
    # Source TOC link slug -> heading text the destination should
    # land on.
    expected: dict[str, str] = {
        "1-introduction": "1 Introduction",
        "2-architecture": "2 Architecture",
        "3-results": "3 Results",
    }

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
        assert stats["links_rewritten"] == 3, stats
        assert stats["outline_added"] == 3, stats

        # Use pypdf to find which slug each link points at; this
        # gives us a deterministic source-slug -> dest-page+y map
        # to validate against `expected`.
        import pypdf
        reader = pypdf.PdfReader(str(out_path))
        # First scan the unaltered link annots for their pre-post-
        # process slug text so we know what each Link "asked for"
        # before the rewrite -- pypdf walks objects, so even after
        # the rewrite we can still read the rewritten /Dest array
        # and identify the slug via its source rect position in the
        # TOC. Simpler: re-read the source PDF before post-process.
        # ...but that requires running the post-process twice. Use a
        # second pass over a re-print to get the slug map.
        second_pdf = Path(td) / "raw.pdf"
        printer2 = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer2.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer2.setOutputFileName(str(second_pdf))
        doc.print(printer2)
        raw_reader = pypdf.PdfReader(str(second_pdf))
        rect_to_slug: dict[tuple, str] = {}
        for page in raw_reader.pages:
            for annot_ref in page.get("/Annots", []) or []:
                obj = annot_ref.get_object()
                if obj.get("/Subtype") != "/Link":
                    continue
                dest = obj.get("/Dest")
                rect = obj.get("/Rect")
                if isinstance(dest, str) and rect is not None:
                    rect_to_slug[tuple(round(float(x), 2) for x in rect)] = dest

        pdf = pdfium.PdfDocument(str(out_path))
        try:
            for page_idx in range(len(pdf)):
                page = pdf[page_idx]
                start_pos = ctypes.c_int(0)
                link_handle = pdfium_raw.FPDF_LINK()
                while pdfium_raw.FPDFLink_Enumerate(
                    page.raw, ctypes.byref(start_pos),
                    ctypes.byref(link_handle),
                ):
                    rect_f = pdfium_raw.FS_RECTF()
                    pdfium_raw.FPDFLink_GetAnnotRect(
                        link_handle, ctypes.byref(rect_f),
                    )
                    src_rect = (
                        round(rect_f.left, 2),
                        round(rect_f.bottom, 2),
                        round(rect_f.right, 2),
                        round(rect_f.top, 2),
                    )
                    slug = rect_to_slug.get(src_rect)
                    if slug not in expected:
                        continue
                    dest = pdfium_raw.FPDFLink_GetDest(pdf.raw, link_handle)
                    assert dest, f"link for slug {slug!r} has no destination"
                    dest_page = pdfium_raw.FPDFDest_GetDestPageIndex(
                        pdf.raw, dest,
                    )
                    assert dest_page >= 0, (
                        f"slug {slug!r} has unresolved destination page"
                    )
                    # Read the destination y from the view params.
                    nparams = ctypes.c_ulong(0)
                    params = (ctypes.c_float * 4)()
                    view_type = pdfium_raw.FPDFDest_GetView(
                        dest, ctypes.byref(nparams), params,
                    )
                    # Qt writes /XYZ (view_type 1) with params (x, y, zoom).
                    # The post-process preserves Qt's destination form.
                    assert view_type == 1, (
                        f"slug {slug!r}: expected /XYZ destination "
                        f"(view_type=1), got {view_type}"
                    )
                    dest_y = params[1]
                    # Now extract the text at and below the
                    # destination y on the destination page. The
                    # target heading must appear in that slice; if
                    # the destination pointed at the TOC or page top
                    # (the "click does nothing" bug), the slice
                    # would contain body text or be empty.
                    dest_page_obj = pdf[dest_page]
                    page_h = float(dest_page_obj.get_size()[1])
                    tp = dest_page_obj.get_textpage()
                    try:
                        near = tp.get_text_bounded(
                            left=0, bottom=max(0.0, dest_y - 40.0),
                            right=2000.0,
                            top=min(page_h, dest_y + 10.0),
                        ) or ""
                    finally:
                        tp.close()
                    expected_heading = expected[slug]
                    assert expected_heading in near, (
                        f"slug {slug!r}: destination at "
                        f"page={dest_page} y={dest_y:.1f} should "
                        f"render heading {expected_heading!r}; got "
                        f"text {near!r}"
                    )
        finally:
            pdf.close()
