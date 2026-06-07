"""Markdown preview pane + clickable Table-of-Contents sidebar.

Wraps `MarkdownPreview` in a `QSplitter` with a heading-list on the
right. The TOC defaults to 1/4 of the widget's horizontal space.
Clicking a TOC entry scrolls the preview to that heading via a
QTextCursor.find(); we don't rely on HTML anchor ids because Qt's
setMarkdown doesn't always emit them consistently.

API mirrors `MarkdownPreview` (setMarkdown / setHtml / setSearchPaths
/ setPlaceholderText) so it can drop into LiveNotesWidget's
QStackedWidget without the caller knowing it's now a composite
widget. find_target() returns the inner preview so the Ctrl+F bar
binds to the right thing.
"""
from __future__ import annotations

import re
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QTextCursor
from PyQt6.QtWidgets import (
    QListWidget,
    QListWidgetItem,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .markdown_preview import MarkdownPreview


# Matches ATX-style headings (# / ## / ### ...). Multiline so it
# scans the whole document in one finditer pass.
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)


class PreviewWithToc(QWidget):
    """Composite widget: preview pane on the left, TOC list on the right."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._splitter.setChildrenCollapsible(False)

        self._preview = MarkdownPreview(self._splitter)
        self._splitter.addWidget(self._preview)

        self._toc = QListWidget(self._splitter)
        self._toc.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self._toc.itemClicked.connect(self._on_toc_item_clicked)
        self._toc.itemActivated.connect(self._on_toc_item_clicked)
        self._toc.setToolTip(
            "Table of contents for this tab. Click a heading to jump "
            "to that section."
        )
        self._splitter.addWidget(self._toc)

        # Default 3:1 split (TOC is 1/4 of the viewport). stretchFactor
        # carries the ratio when the parent widget resizes.
        self._splitter.setStretchFactor(0, 3)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([600, 200])

        layout.addWidget(self._splitter)

        # Remember the most recent body so the TOC can be rebuilt
        # later (e.g. if the user resizes / focuses).
        self._current_body: str = ""
        # Heading numbering toggle (#92). When True, both the body
        # passed to setMarkdown AND the sidebar TOC reflect the
        # numbering, so they stay consistent.
        self._number_headings_enabled: bool = False
        self._number_headings_skip_h1: bool = False

    def set_heading_numbering(
        self, enabled: bool, *, skip_h1: bool = False,
    ) -> None:
        """Toggle heading auto-numbering for the wrapped preview (#92).

        The transform applies at this level so both the rendered
        body and the sidebar TOC see the same numbered headings.
        Forwards to the inner MarkdownPreview as well so any direct
        setMarkdown call into the inner widget stays consistent.
        """
        self._number_headings_enabled = bool(enabled)
        self._number_headings_skip_h1 = bool(skip_h1)
        self._preview.set_heading_numbering(enabled, skip_h1=skip_h1)

    # ---- MarkdownPreview-shaped public API ----
    def setMarkdown(self, text: str) -> None:  # noqa: N802 (Qt convention)
        self._current_body = text or ""
        if self._number_headings_enabled:
            from ..utils.markdown_outline import number_headings  # noqa: PLC0415
            rendered = number_headings(
                self._current_body, skip_h1=self._number_headings_skip_h1,
            )
        else:
            rendered = self._current_body
        # Inner MarkdownPreview also has the toggle wired; passing
        # the already-numbered body in is harmless because
        # number_headings is idempotent against numbered input only
        # when nothing else has changed -- the inner preview's
        # toggle is the deciding factor for whether a second pass
        # runs. Disable the inner toggle while we hand off the
        # already-transformed body to avoid the double-prefix risk.
        previous_inner = self._preview._number_headings_enabled  # noqa: SLF001
        self._preview._number_headings_enabled = False  # noqa: SLF001
        try:
            self._preview.setMarkdown(rendered)
        finally:
            self._preview._number_headings_enabled = previous_inner  # noqa: SLF001
        self._rebuild_toc(rendered)

    def setHtml(self, text: str) -> None:  # noqa: N802
        self._current_body = ""
        self._preview.setHtml(text or "")
        # HTML caller didn't give us the source markdown; clear the
        # TOC (the preview is showing arbitrary HTML, headings
        # might not match an extractable pattern).
        self._toc.clear()
        self._toggle_toc_visible(False)

    def setSearchPaths(self, paths: list[str]) -> None:  # noqa: N802
        self._preview.setSearchPaths(paths)

    def setPlaceholderText(self, text: str) -> None:  # noqa: N802
        self._preview.setPlaceholderText(text)

    def clear(self) -> None:
        self._current_body = ""
        self._preview.clear()
        self._toc.clear()
        self._toggle_toc_visible(False)

    # ---- helpers used by callers that want the inner widgets ----
    def find_target(self) -> QWidget:
        """Ctrl+F binds to the actual preview, not the TOC."""
        return self._preview

    def preview(self) -> MarkdownPreview:
        return self._preview

    def toc_visible(self) -> bool:
        return not self._toc.isHidden()

    # ---- internals ----
    def _rebuild_toc(self, body: str) -> None:
        self._toc.clear()
        headings = extract_headings(body)
        if not headings:
            self._toggle_toc_visible(False)
            return
        self._toggle_toc_visible(True)
        # Normalize indent: smallest level seen becomes the left edge.
        min_level = min(h[0] for h in headings)
        for level, text in headings:
            indent = level - min_level
            label = ("    " * indent) + text
            item = QListWidgetItem(label, self._toc)
            item.setData(Qt.ItemDataRole.UserRole, text)
            # Top-level entries render slightly bolder so the user can
            # see hierarchy at a glance.
            if level <= min_level:
                font = QFont(item.font())
                font.setBold(True)
                item.setFont(font)

    def _toggle_toc_visible(self, visible: bool) -> None:
        # Hiding the TOC entirely when there are no headings keeps
        # the preview from running narrower than it needs to for
        # plain-prose tabs.
        self._toc.setVisible(visible)
        if visible:
            # Restore the 3:1 split (a prior hide may have collapsed
            # the second pane to 0).
            sizes = self._splitter.sizes()
            if sizes[1] == 0:
                total = max(1, sum(sizes))
                self._splitter.setSizes(
                    [int(total * 0.75), int(total * 0.25)],
                )

    def _on_toc_item_clicked(self, item: QListWidgetItem) -> None:
        target = item.data(Qt.ItemDataRole.UserRole) or item.text().strip()
        if not target:
            return
        self._scroll_preview_to_heading(str(target))

    def _scroll_preview_to_heading(self, heading_text: str) -> None:
        """Find the heading in the preview and scroll to it.

        QTextDocument.find() returns a fresh QTextCursor positioned
        at the match; we use it as the preview's text cursor so the
        viewport scrolls + the heading line highlights briefly.
        """
        doc = self._preview.document()
        cursor = doc.find(heading_text)
        if cursor.isNull():
            return
        # Move to the start of the line so the heading sits at the
        # top of the viewport rather than mid-screen.
        cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        self._preview.setTextCursor(cursor)
        self._preview.ensureCursorVisible()


def extract_headings(body: str) -> list[tuple[int, str]]:
    """Pull (level, text) pairs from an ATX-headings markdown body.

    Skips fenced code blocks so a `# foo` inside a code fence
    isn't mistaken for a heading. Trims trailing `#` characters
    (the optional closing form `## Section ##`).
    """
    if not body:
        return []
    out: list[tuple[int, str]] = []
    in_fence = False
    fence_marker: Optional[str] = None
    for raw_line in body.splitlines():
        stripped = raw_line.lstrip()
        # Detect fenced code blocks: ``` or ~~~ on its own.
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = "```" if stripped.startswith("```") else "~~~"
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif fence_marker == marker:
                in_fence = False
                fence_marker = None
            continue
        if in_fence:
            continue
        match = _HEADING_RE.match(raw_line)
        if match:
            level = len(match.group(1))
            text = match.group(2).strip()
            if text:
                out.append((level, text))
    return out
