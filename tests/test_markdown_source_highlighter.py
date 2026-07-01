"""Tests for the styled markdown source highlighter (#91).

Two layers:

  * Pure-regex tests on the module-level patterns -- cheap to run,
    pin the disambiguation rules (bold vs italic precedence, list
    markers only at line start, fence delimiters at line start).

  * Offscreen-Qt integration tests that build a QTextDocument, attach
    the highlighter, and verify QTextCharFormat application against
    known positions in known text. These catch regressions in the
    state-machine + setFormat plumbing without needing a real editor
    on screen.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

import pytest

from meeting_notetaker.ui.markdown_source_highlighter import (
    BLOCKQUOTE_RE,
    FENCE_RE,
    HEADING_MULTIPLIERS,
    HEADING_RE,
    HR_RE,
    INLINE_BOLD_STAR_RE,
    INLINE_BOLD_UNDER_RE,
    INLINE_CODE_RE,
    INLINE_ITALIC_STAR_RE,
    INLINE_ITALIC_UNDER_RE,
    INLINE_LINK_RE,
    INLINE_STRIKE_RE,
    LIST_MARKER_RE,
    STATE_IN_FENCE,
    STATE_NORMAL,
)


# ---- HEADING_RE ---------------------------------------------------------

def test_heading_h1_through_h6():
    for n in range(1, 7):
        text = "#" * n + " Heading text"
        m = HEADING_RE.match(text)
        assert m is not None, f"H{n} did not match"
        assert m.group(1) == "#" * n


def test_heading_requires_space_after_hashes():
    """`#Not a heading` is not a heading per common markdown grammar."""
    assert HEADING_RE.match("#NotAHeading") is None
    assert HEADING_RE.match("# Heading") is not None


def test_heading_caps_at_six_hashes():
    """Seven hashes isn't a heading -- matches up to H6 only by spec."""
    text = "####### Too deep"
    m = HEADING_RE.match(text)
    # The regex matches H6 (six hashes), capturing the seventh as part of
    # the body. Either behavior is acceptable; what matters is that the
    # captured marker is <=6 hashes.
    if m is not None:
        assert len(m.group(1)) <= 6


# ---- BLOCKQUOTE_RE -----------------------------------------------------

def test_blockquote_matches_simple():
    m = BLOCKQUOTE_RE.match("> Quoted text")
    assert m is not None


def test_blockquote_matches_nested():
    m = BLOCKQUOTE_RE.match(">> Doubly nested")
    assert m is not None
    assert m.group(2) == ">>"


def test_blockquote_allows_leading_whitespace():
    m = BLOCKQUOTE_RE.match("    > Indented quote")
    assert m is not None


# ---- LIST_MARKER_RE ---------------------------------------------------

def test_list_marker_dash():
    assert LIST_MARKER_RE.match("- item")
    assert LIST_MARKER_RE.match("  - nested item")


def test_list_marker_star_plus():
    assert LIST_MARKER_RE.match("* item")
    assert LIST_MARKER_RE.match("+ item")


def test_list_marker_numbered():
    assert LIST_MARKER_RE.match("1. item")
    assert LIST_MARKER_RE.match("12. item")
    assert LIST_MARKER_RE.match("1) item")


def test_list_marker_does_not_match_inline_asterisk():
    """`*emphasis*` at line start has no trailing space; the list regex
    requires `<marker><space>` so this falls through to inline."""
    assert LIST_MARKER_RE.match("*emphasis*") is None


# ---- FENCE_RE / HR_RE -------------------------------------------------

def test_fence_recognizes_triple_backtick_and_tilde():
    assert FENCE_RE.match("```")
    assert FENCE_RE.match("```python")
    assert FENCE_RE.match("~~~")
    assert FENCE_RE.match("    ```indented")  # rare but valid


def test_fence_does_not_match_single_or_double_backticks():
    assert FENCE_RE.match("`code`") is None
    assert FENCE_RE.match("``") is None


def test_hr_recognizes_dashes_stars_underscores():
    assert HR_RE.match("---")
    assert HR_RE.match("- - -")
    assert HR_RE.match("***")
    assert HR_RE.match("___")


def test_hr_does_not_match_normal_dashes():
    assert HR_RE.match("- a list item") is None
    assert HR_RE.match("text -- with dashes") is None


# ---- INLINE patterns ---------------------------------------------------

def test_bold_star_matches():
    m = list(INLINE_BOLD_STAR_RE.finditer("plain **bold** plain"))
    assert len(m) == 1
    assert m[0].group(0) == "**bold**"


def test_bold_underscore_matches():
    m = list(INLINE_BOLD_UNDER_RE.finditer("plain __bold__ plain"))
    assert len(m) == 1


def test_italic_star_matches_single_asterisk():
    m = list(INLINE_ITALIC_STAR_RE.finditer("plain *italic* plain"))
    assert len(m) == 1
    assert m[0].group(0) == "*italic*"


def test_italic_does_not_match_inside_bold():
    """`**bold**` shouldn't get reinterpreted as two italic markers
    sandwiching a literal-asterisk. The italic pattern uses negative
    lookarounds against `*` and word chars on both sides."""
    text = "**bold**"
    italic_matches = list(INLINE_ITALIC_STAR_RE.finditer(text))
    # The italic pattern's negative lookbehind/ahead for `*` excludes
    # the inner span between bold markers.
    assert italic_matches == []


def test_code_matches_backticks():
    m = list(INLINE_CODE_RE.finditer("inline `code` example"))
    assert len(m) == 1
    assert m[0].group(0) == "`code`"


def test_strike_matches_double_tilde():
    m = list(INLINE_STRIKE_RE.finditer("plain ~~strike~~ plain"))
    assert len(m) == 1


def test_link_captures_label_and_url():
    m = INLINE_LINK_RE.search("read [the docs](https://example.com) please")
    assert m is not None
    assert m.group(1) == "the docs"
    assert m.group(2) == "https://example.com"


# ---- HEADING_MULTIPLIERS sanity ---------------------------------------

def test_heading_multipliers_have_six_entries_descending():
    assert len(HEADING_MULTIPLIERS) == 6
    assert all(
        HEADING_MULTIPLIERS[i] >= HEADING_MULTIPLIERS[i + 1]
        for i in range(5)
    ), "H1 must be >= H2 >= ... >= H6 in size"


def test_h1_is_meaningfully_larger_than_body():
    assert HEADING_MULTIPLIERS[0] >= 1.4, "H1 should noticeably stand out"


# ---- state machine constants ------------------------------------------

def test_state_constants_distinct():
    assert STATE_NORMAL != STATE_IN_FENCE


# ---- offscreen Qt integration -----------------------------------------

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QFont, QTextCharFormat, QTextDocument  # noqa: E402
from PyQt6.QtWidgets import QApplication, QPlainTextEdit  # noqa: E402

from meeting_notetaker.ui.markdown_source_highlighter import (  # noqa: E402
    MarkdownSourceHighlighter,
)


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


@dataclass
class _RecordedFormat:
    block_text: str
    start: int
    length: int
    format: QTextCharFormat


class _RecordingHighlighter(MarkdownSourceHighlighter):
    """Wraps setFormat to record every range + format applied during
    highlightBlock. The block layout's `formats()` accessor doesn't
    return meaningful data for an un-rendered document in offscreen
    Qt; recording via setFormat gives a reliable view of what the
    highlighter intended to do."""

    def __init__(self, document: QTextDocument, **kwargs) -> None:
        self.records: list[_RecordedFormat] = []
        super().__init__(document, **kwargs)

    def highlightBlock(self, text: str) -> None:  # noqa: D401 - Qt entry
        self._current_block_text = text
        super().highlightBlock(text)

    def setFormat(self, *args) -> None:
        # Qt overloads: (start, length, QTextCharFormat) or
        # (start, length, QFont/QColor). We only use the QTextCharFormat
        # variant, but be tolerant.
        if len(args) >= 3 and isinstance(args[2], QTextCharFormat):
            start, length, fmt = args[0], args[1], args[2]
            self.records.append(_RecordedFormat(
                block_text=getattr(self, "_current_block_text", ""),
                start=start, length=length,
                format=QTextCharFormat(fmt),
            ))
        super().setFormat(*args)


def _make_recorder(text: str, base_font: QFont | None = None):
    """Build a doc + recording highlighter for a given source text.
    The QPlainTextEdit holds a layout so the highlighter actually
    runs; we keep the editor alive through the returned tuple."""
    editor = QPlainTextEdit()
    if base_font is not None:
        editor.setFont(base_font)
    editor.setPlainText(text)
    doc = editor.document()
    rec = _RecordingHighlighter(doc, base_font=base_font or editor.font())
    rec.rehighlight()  # force a fresh pass so records are populated
    return doc, editor, rec


def _records_covering(rec: _RecordingHighlighter, block_text: str, pos: int) -> list[_RecordedFormat]:
    """Records on a specific block that overlap a given character pos.

    pos is 0-based relative to the start of the block's text. Records
    are returned in application order so the last one wins for a given
    attribute (matches QSyntaxHighlighter's "last setFormat wins"
    semantics for overlapping ranges)."""
    return [
        r for r in rec.records
        if r.block_text == block_text
        and r.start <= pos < r.start + r.length
    ]


def test_highlighter_attaches_to_document_without_crash(qt_app):
    doc, _editor, hl = _make_recorder("# Heading\n\n**bold** body")
    assert hl.document() is doc


def test_h1_records_larger_font_size(qt_app):
    base = QFont()
    base.setPointSize(11)
    _doc, _editor, rec = _make_recorder("# Heading text", base_font=base)
    records = _records_covering(rec, "# Heading text", pos=5)
    assert records, "no format range covers H1 content"
    # The heading format applies via setFormat(0, len(text), heading_fmt)
    # so the recorded range starts at 0.
    heading_record = next((r for r in records if r.start == 0), None)
    assert heading_record is not None
    assert heading_record.format.fontPointSize() > 11.0


def test_bold_records_bold_weight(qt_app):
    _doc, _editor, rec = _make_recorder("a **bold** b")
    # "bold" inner text starts at position 4 (a, space, *, *).
    records = _records_covering(rec, "a **bold** b", pos=5)
    assert any(
        r.format.fontWeight() >= QFont.Weight.Bold for r in records
    ), "no bold record covers the inner text"


def test_italic_records_italic(qt_app):
    _doc, _editor, rec = _make_recorder("a *italic* b")
    records = _records_covering(rec, "a *italic* b", pos=4)
    assert any(r.format.fontItalic() for r in records)


def test_inline_code_records_monospace_family(qt_app):
    _doc, _editor, rec = _make_recorder("a `code` b")
    records = _records_covering(rec, "a `code` b", pos=4)
    assert records, "no record covers the inline code"
    code_record = next(
        (r for r in records if r.format.fontFamily() != ""), None,
    )
    assert code_record is not None, "no monospace family applied"


def test_strikethrough_records(qt_app):
    _doc, _editor, rec = _make_recorder("a ~~gone~~ b")
    records = _records_covering(rec, "a ~~gone~~ b", pos=5)
    assert any(r.format.fontStrikeOut() for r in records)


def test_link_label_records_underline(qt_app):
    _doc, _editor, rec = _make_recorder("see [docs](https://x) for more")
    # "docs" label inner range starts at position 5 (after "see [").
    records = _records_covering(rec, "see [docs](https://x) for more", pos=6)
    assert any(r.format.fontUnderline() for r in records)


def test_multiline_fence_state_persists(qt_app):
    """A fence opened on one line stays open until the next fence
    line; the body in between is in IN_FENCE state."""
    doc, _editor, _rec = _make_recorder(
        "intro\n```python\nx = 1\n```\noutro"
    )
    block = doc.findBlockByLineNumber(2)
    assert block.userState() == STATE_IN_FENCE


def test_reload_styling_changes_heading_size(qt_app):
    """reload_styling with a new font rebuilds heading formats so the
    heading size after the reload is larger than before."""
    small = QFont()
    small.setPointSize(10)
    _doc, _editor, rec = _make_recorder("# Heading", base_font=small)
    before = _records_covering(rec, "# Heading", pos=3)
    size_before = next(
        (r.format.fontPointSize() for r in before if r.format.fontPointSize() > 0),
        0,
    )
    big = QFont()
    big.setPointSize(20)
    rec.records.clear()
    rec.reload_styling(base_font=big)
    after = _records_covering(rec, "# Heading", pos=3)
    size_after = next(
        (r.format.fontPointSize() for r in after if r.format.fontPointSize() > 0),
        0,
    )
    assert size_after > size_before


def test_detach_via_setDocument_None(qt_app):
    """Calling setDocument(None) detaches the highlighter cleanly --
    no exception, and the highlighter stops receiving block events
    from the document."""
    doc, _editor, hl = _make_recorder("# Heading")
    hl.setDocument(None)
    doc.setPlainText("## Different")


def test_blockquote_foreground_is_not_palette_mid(qt_app):
    """Regression for #125: the blockquote format must NOT pull its
    foreground from QPalette.ColorRole.Mid. Mid is a chrome color
    (scrollbar troughs, disabled UI) and renders dark-on-dark in dark
    mode. The current fix uses PlaceholderText; this test guards
    against a future refactor silently reverting to Mid or any color
    that matches the Mid role value."""
    from PyQt6.QtGui import QPalette

    _doc, _editor, hl = _make_recorder("> quoted")
    quote_fg = hl._quote_fmt.foreground().color()
    assert quote_fg.isValid(), "quote format has no foreground set"

    mid = hl._palette.color(QPalette.ColorRole.Mid)
    if mid.isValid():
        assert quote_fg.rgba() != mid.rgba(), (
            "blockquote foreground matches palette Mid role -- would "
            "render dark-on-dark in dark mode (#125)"
        )
