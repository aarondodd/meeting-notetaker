"""Styled markdown source highlighting for the My Notes editor (#91).

A `QSyntaxHighlighter` that decorates the markdown source so structure
scans at a glance without giving up plain-text editing. Distinct from
the Preview tab: the syntax characters (`#`, `**`, `[]()`) stay visible
and editable; we just style what's between them.

Design choices documented inline near the relevant code, but the
high-level decisions live here so changes are anchored.

Format precedence (matters because regex matches can overlap):

  1. Headings -- line-anchored, win first when present
  2. Blockquotes -- line-anchored, win after headings
  3. Fenced code -- multi-line, tracked via setCurrentBlockState
  4. List markers -- line-anchored, partial-line (rest of line stays
     available for inline patterns)
  5. Inline patterns -- bold > italic > code > strike > link, in that
     order. Bold checked before italic so `**bold**` doesn't get
     interpreted as two-sided `*italic*`.

Font sizing scales off the editor's `defaultFont` at the time the
highlighter is instantiated. If the user changes their editor-font
preference, the caller should rebuild the highlighter so the new base
flows into the heading multipliers.

The marker characters (`#`, `**`, `_`, etc.) are not painted in a
distinct color today -- they retain the document's default color so
that selection contrast stays readable across themes. The visual cue
is the format applied to the *content*: bold, italic, monospace, etc.
The trade-off is intentional; the v1 highlighter prioritizes
readability over dimming aesthetics.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontDatabase,
    QPalette,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextDocument,
)


# Multi-line state for setCurrentBlockState / previousBlockState. Qt
# requires non-negative ints; we encode "in fenced code" as 1.
STATE_NORMAL = 0
STATE_IN_FENCE = 1


# Heading size multipliers vs base font. Chosen so H1 stands out
# without dominating the editor; H4-H6 are subtly distinct but won't
# overflow a typical viewport.
HEADING_MULTIPLIERS: tuple[float, ...] = (1.70, 1.50, 1.30, 1.18, 1.10, 1.04)


# ---- inline patterns (compiled module-load) -----------------------------

# Order matters -- bold patterns must match before italic so `**bold**`
# doesn't get reinterpreted as two-sided italic. Within a row, the
# longer marker (**) wins because the regex is anchored at the same
# position.
INLINE_BOLD_STAR_RE = re.compile(r"\*\*(?=\S)([^*\n]+?)(?<=\S)\*\*")
INLINE_BOLD_UNDER_RE = re.compile(r"__(?=\S)([^_\n]+?)(?<=\S)__")
INLINE_ITALIC_STAR_RE = re.compile(r"(?<![*\w])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![*\w])")
INLINE_ITALIC_UNDER_RE = re.compile(r"(?<![_\w])_(?=\S)([^_\n]+?)(?<=\S)_(?![_\w])")
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
INLINE_STRIKE_RE = re.compile(r"~~(?=\S)([^~\n]+?)(?<=\S)~~")
INLINE_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\n]+)\)")

# Line-anchored.
HEADING_RE = re.compile(r"^(#{1,6})(\s+)(.*)$")
BLOCKQUOTE_RE = re.compile(r"^(\s*)(>+)(\s*)(.*)$")
LIST_MARKER_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])(\s+)")
HR_RE = re.compile(r"^\s*(\*\s*\*\s*\*+|\-\s*\-\s*\-+|_\s*_\s*_+)\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True)
class FormatTriple:
    """Convenience bag for a (left-marker, content, right-marker) format
    application. The marker formats can be None when we don't want to
    style the markers distinctly."""

    content_format: QTextCharFormat
    left_marker_format: Optional[QTextCharFormat] = None
    right_marker_format: Optional[QTextCharFormat] = None


class MarkdownSourceHighlighter(QSyntaxHighlighter):
    """Styled markdown source view for QPlainTextEdit / QTextEdit.

    Caller pattern:

        editor = QPlainTextEdit()
        editor.setFont(my_font)
        highlighter = MarkdownSourceHighlighter(editor.document())

    Detach by setting `setDocument(None)` or replacing it with a
    plain `QSyntaxHighlighter()` instance.
    """

    def __init__(
        self,
        document: QTextDocument,
        *,
        base_font: Optional[QFont] = None,
        palette: Optional[QPalette] = None,
    ) -> None:
        super().__init__(document)
        self._base_font = QFont(base_font) if base_font else QFont(document.defaultFont())
        # Snapshot the palette colors at construction; the highlighter
        # gets rebuilt when the user toggles themes via the surrounding
        # widget, so we don't subscribe to dynamic palette changes here.
        self._palette = palette or QPalette()
        self._build_formats()

    def _build_formats(self) -> None:
        """Construct every QTextCharFormat the highlighter applies.

        Centralized here so font / palette changes can rebuild by
        calling _build_formats() instead of mutating individual
        attributes."""
        base_size = self._base_font.pointSizeF()
        if base_size <= 0:
            base_size = float(self._base_font.pointSize() or 11)

        # Heading formats -- one per level, progressively larger.
        self._heading_formats: list[QTextCharFormat] = []
        for multiplier in HEADING_MULTIPLIERS:
            fmt = QTextCharFormat()
            fmt.setFontPointSize(base_size * multiplier)
            fmt.setFontWeight(QFont.Weight.Bold)
            self._heading_formats.append(fmt)

        # Inline formats.
        self._bold_fmt = QTextCharFormat()
        self._bold_fmt.setFontWeight(QFont.Weight.Bold)

        self._italic_fmt = QTextCharFormat()
        self._italic_fmt.setFontItalic(True)

        # Monospace family for code -- preferred from the system's
        # fixed-width font so it matches the rest of the app.
        mono_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        self._code_fmt = QTextCharFormat()
        self._code_fmt.setFontFamily(mono_font.family())
        # Subtle background tint that adapts to theme via the palette.
        # AlternateBase is usually a slightly-shifted neutral color.
        bg = self._palette.color(QPalette.ColorRole.AlternateBase)
        if bg.isValid():
            self._code_fmt.setBackground(QBrush(bg))

        self._strike_fmt = QTextCharFormat()
        self._strike_fmt.setFontStrikeOut(True)

        # Link inner text -- underline + link color from palette.
        self._link_text_fmt = QTextCharFormat()
        self._link_text_fmt.setFontUnderline(True)
        link_color = self._palette.color(QPalette.ColorRole.Link)
        if link_color.isValid():
            self._link_text_fmt.setForeground(QBrush(link_color))

        # Blockquote -- italic + dimmer than body text. PlaceholderText
        # is Qt's role for "dim but still legible" and tracks OS
        # light/dark themes; Mid was chrome-grey and rendered
        # dark-on-dark in dark mode (#125).
        self._quote_fmt = QTextCharFormat()
        self._quote_fmt.setFontItalic(True)
        quote_color = self._palette.color(QPalette.ColorRole.PlaceholderText)
        if quote_color.isValid():
            self._quote_fmt.setForeground(QBrush(quote_color))

        # List marker (- / * / + / 1.). Distinct color so the structure
        # pops; rest of the line gets normal inline processing.
        self._list_marker_fmt = QTextCharFormat()
        accent = self._palette.color(QPalette.ColorRole.Highlight)
        if accent.isValid():
            self._list_marker_fmt.setForeground(QBrush(accent))
        self._list_marker_fmt.setFontWeight(QFont.Weight.Bold)

        # Horizontal rule.
        self._hr_fmt = QTextCharFormat()
        mid_color2 = self._palette.color(QPalette.ColorRole.Mid)
        if mid_color2.isValid():
            self._hr_fmt.setForeground(QBrush(mid_color2))

        # Fenced code (multi-line) -- same family as inline, applies
        # to whole lines inside the fence.
        self._fence_fmt = QTextCharFormat()
        self._fence_fmt.setFontFamily(mono_font.family())
        if bg.isValid():
            self._fence_fmt.setBackground(QBrush(bg))

    # ---- highlight entry point -----------------------------------------

    def highlightBlock(self, text: str) -> None:
        """Apply highlighting to a single line.

        Block-level checks first (heading, blockquote, fence, list,
        HR), then inline patterns within the remaining content. Order
        of inline pattern application matters; see module docstring.
        """
        # Fenced code state propagates across blocks via
        # setCurrentBlockState. The state machine is:
        #   prev=NORMAL + this='```'  -> set state IN_FENCE
        #   prev=IN_FENCE + this='```' -> set state NORMAL (closing)
        #   prev=IN_FENCE + this='...' -> stay in fence, format line
        prev_state = max(self.previousBlockState(), 0)
        fence_match = FENCE_RE.match(text)
        if fence_match:
            self.setFormat(0, len(text), self._fence_fmt)
            if prev_state == STATE_IN_FENCE:
                self.setCurrentBlockState(STATE_NORMAL)
            else:
                self.setCurrentBlockState(STATE_IN_FENCE)
            return
        if prev_state == STATE_IN_FENCE:
            self.setFormat(0, len(text), self._fence_fmt)
            self.setCurrentBlockState(STATE_IN_FENCE)
            return
        self.setCurrentBlockState(STATE_NORMAL)

        # Horizontal rule.
        if HR_RE.match(text):
            self.setFormat(0, len(text), self._hr_fmt)
            return

        # Heading -- format the whole line at the level's size.
        heading_match = HEADING_RE.match(text)
        if heading_match:
            level = len(heading_match.group(1))
            fmt = self._heading_formats[level - 1]
            self.setFormat(0, len(text), fmt)
            # No inline pattern processing inside a heading -- the
            # heading already implies bold and a sized run; layering
            # italic / code on top complicates layout for marginal
            # gain.
            return

        # Blockquote -- whole line italic + dimmer. We still let
        # inline patterns apply on top so `> **important**` reads as
        # quote + bold inside.
        bq_match = BLOCKQUOTE_RE.match(text)
        if bq_match:
            self.setFormat(0, len(text), self._quote_fmt)
            # Fall through so inline patterns can decorate further.

        # List marker -- color the marker character(s) only; the
        # rest of the line falls through to inline.
        list_match = LIST_MARKER_RE.match(text)
        if list_match:
            marker_start = len(list_match.group(1))
            marker_text = list_match.group(2)
            self.setFormat(marker_start, len(marker_text), self._list_marker_fmt)

        # Inline patterns. Order matters: bold before italic so
        # `**bold**` doesn't get treated as `*` + italic + `*`.
        # Each apply_inline call walks the text left to right and
        # applies the format to every non-overlapping match.
        self._apply_inline(text, INLINE_BOLD_STAR_RE, self._bold_fmt)
        self._apply_inline(text, INLINE_BOLD_UNDER_RE, self._bold_fmt)
        self._apply_inline(text, INLINE_ITALIC_STAR_RE, self._italic_fmt)
        self._apply_inline(text, INLINE_ITALIC_UNDER_RE, self._italic_fmt)
        self._apply_inline(text, INLINE_CODE_RE, self._code_fmt)
        self._apply_inline(text, INLINE_STRIKE_RE, self._strike_fmt)
        # Links: format the visible label, not the URL.
        self._apply_inline_link(text)

    # ---- inline helpers -----------------------------------------------

    def _apply_inline(
        self,
        text: str,
        pattern: re.Pattern,
        fmt: QTextCharFormat,
    ) -> None:
        """Apply `fmt` to every non-overlapping match of `pattern`.

        The format extends across the entire matched range -- markers
        included -- so e.g. `**bold**` ends up entirely bold (the
        asterisks + the inner text). The marker characters stay
        visible but rendered in the same weight as the content.
        Keeping the formatting consistent across the markers
        sidesteps a class of subtle off-by-one rendering glitches
        where Qt's measurement of bold vs non-bold contiguous runs
        can leave a 1-pixel kerning gap.
        """
        for m in pattern.finditer(text):
            start, end = m.start(), m.end()
            self.setFormat(start, end - start, fmt)

    def _apply_inline_link(self, text: str) -> None:
        """Style the visible label part of `[text](url)` distinctly.

        Group 1 is the label (between the brackets); group 2 is the
        URL. We underline + color group 1; group 2 stays in the
        document's default color so the user can still see the
        target without it competing with the label for attention.
        """
        for m in INLINE_LINK_RE.finditer(text):
            label_start = m.start(1)
            label_end = m.end(1)
            self.setFormat(label_start, label_end - label_start, self._link_text_fmt)

    # ---- public reload hook -------------------------------------------

    def reload_styling(
        self,
        *,
        base_font: Optional[QFont] = None,
        palette: Optional[QPalette] = None,
    ) -> None:
        """Rebuild internal formats against a new font / palette.

        Called by the editor when the user changes their font
        preference in Settings (#67) or switches themes. After
        reload, `rehighlight()` re-runs the entire document so the
        new sizes / colors take effect immediately.
        """
        if base_font is not None:
            self._base_font = QFont(base_font)
        if palette is not None:
            self._palette = palette
        self._build_formats()
        self.rehighlight()
