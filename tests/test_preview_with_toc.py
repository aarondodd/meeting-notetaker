"""PreviewWithToc -- heading extraction + TOC sidebar wiring.

extract_headings is pure-Python; the widget tests need Qt (offscreen
platform). The wrapper widget mirrors MarkdownPreview's
setMarkdown / setSearchPaths / setPlaceholderText API so it can
drop into LiveNotesWidget's QStackedWidget without the caller
noticing.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.ui.preview_with_toc import (  # noqa: E402
    PreviewWithToc,
    extract_headings,
)


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


# ---- extract_headings (no Qt needed) ---------------------------------


def test_extract_simple_atx_headings():
    body = "# A\n## B\n### C\n"
    assert extract_headings(body) == [(1, "A"), (2, "B"), (3, "C")]


def test_extract_returns_empty_for_empty_or_plain():
    assert extract_headings("") == []
    assert extract_headings(None) == []
    assert extract_headings("Just a paragraph.\nNo headings here.") == []


def test_extract_strips_trailing_closing_hashes():
    """The optional `## Section ##` closing form trims correctly."""
    body = "## Closing form ##\n# Plain\n"
    out = extract_headings(body)
    assert (2, "Closing form") in out
    assert (1, "Plain") in out


def test_extract_skips_headings_inside_fenced_code_blocks():
    """A `# heading` inside a ``` fence is code, not a heading."""
    body = (
        "# Real\n"
        "```\n"
        "# Not a heading -- this is code\n"
        "```\n"
        "## Another real\n"
    )
    out = extract_headings(body)
    levels = [lvl for lvl, _t in out]
    texts = [t for _lvl, t in out]
    assert levels == [1, 2]
    assert "Real" in texts
    assert "Another real" in texts
    assert "Not a heading -- this is code" not in texts


def test_extract_handles_tilde_fences():
    body = "# Outside\n~~~\n# Inside\n~~~\n"
    out = extract_headings(body)
    assert out == [(1, "Outside")]


def test_extract_requires_space_after_hash_marker():
    """'#header' (no space) is NOT a heading per CommonMark."""
    body = "#nope\n# yes\n"
    out = extract_headings(body)
    assert out == [(1, "yes")]


def test_extract_six_levels():
    body = "# 1\n## 2\n### 3\n#### 4\n##### 5\n###### 6\n"
    out = extract_headings(body)
    assert [lvl for lvl, _ in out] == [1, 2, 3, 4, 5, 6]


# ---- PreviewWithToc widget smoke -------------------------------------


def test_widget_populates_toc_from_markdown(qt_app):
    w = PreviewWithToc()
    try:
        w.setMarkdown("# Alpha\n## Beta\n### Gamma\nbody\n")
        assert w._toc.count() == 3  # noqa: SLF001
        assert w._toc.item(0).data(256) == "Alpha"  # noqa: SLF001 -- UserRole = 256
        # Visibility flag indicates the TOC has content.
        assert w.toc_visible()
    finally:
        w.deleteLater()


def test_widget_hides_toc_when_no_headings(qt_app):
    w = PreviewWithToc()
    try:
        w.setMarkdown("Just a paragraph with no headings.\n")
        assert w._toc.count() == 0  # noqa: SLF001
        assert not w.toc_visible()
    finally:
        w.deleteLater()


def test_widget_clears_toc_on_subsequent_set(qt_app):
    """A second setMarkdown with no headings hides the TOC."""
    w = PreviewWithToc()
    try:
        w.setMarkdown("# Alpha\n## Beta\n")
        assert w.toc_visible()
        w.setMarkdown("plain text only")
        assert not w.toc_visible()
        assert w._toc.count() == 0  # noqa: SLF001
    finally:
        w.deleteLater()


def test_widget_find_target_returns_inner_preview(qt_app):
    """Ctrl+F binds to the actual preview, not the TOC list."""
    w = PreviewWithToc()
    try:
        # find_target unwraps to the inner MarkdownPreview so the
        # FindBar searches the prose, not the heading list.
        target = w.find_target()
        assert target is w.preview()
    finally:
        w.deleteLater()


def test_widget_setplaceholdertext_forwards_to_preview(qt_app):
    """The API delegate must reach the underlying MarkdownPreview."""
    w = PreviewWithToc()
    try:
        w.setPlaceholderText("waiting for synthesis")
        assert w.preview().placeholderText() == "waiting for synthesis"
    finally:
        w.deleteLater()


def test_widget_clear_resets_state(qt_app):
    w = PreviewWithToc()
    try:
        w.setMarkdown("# A\n## B\n")
        assert w.toc_visible()
        w.clear()
        assert w._toc.count() == 0  # noqa: SLF001
        assert not w.toc_visible()
    finally:
        w.deleteLater()


def test_widget_preview_first_then_toc_in_splitter(qt_app):
    """Splitter order: preview on the left, TOC on the right.
    Headless Qt doesn't resolve actual splitter sizes until show()
    so we verify the widget order + count instead of the px split."""
    w = PreviewWithToc()
    try:
        assert w._splitter.count() == 2  # noqa: SLF001
        assert w._splitter.widget(0) is w.preview()  # noqa: SLF001
        # Widget at index 1 is the TOC list.
        assert w._splitter.widget(1) is w._toc  # noqa: SLF001
    finally:
        w.deleteLater()
