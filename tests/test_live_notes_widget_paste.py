"""Paste-hook tests for _MarkdownEditor.

Verifies that pasting QMimeData with text/html into the My Notes /
Synthesis editor inserts Markdown source (not the lossy text/plain
variant) and that pasted images still flow through the existing
image_pasted signal path.

Both tabs in SessionView (My Notes, Synthesis) share the same
_MarkdownEditor subclass, so covering it once here pins behavior
for both surfaces.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
pytest.importorskip("markdownify")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QMimeData  # noqa: E402
from PyQt6.QtGui import QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.ui.live_notes_widget import (  # noqa: E402
    LiveNotesWidget,
    _MarkdownEditor,
)


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


# ---- HTML -> Markdown on paste -------------------------------------------


def test_html_paste_inserts_markdown(qt_app):
    w = LiveNotesWidget()
    try:
        mime = QMimeData()
        mime.setHtml(
            '<h2>Project status</h2>'
            '<p>See <a href="https://example.com/d">doc</a>.</p>'
        )
        # Set a lossy plain text variant too -- the hook should
        # prefer the HTML conversion over this.
        mime.setText("Project statusSee doc.")
        w._editor.insertFromMimeData(mime)  # noqa: SLF001
        body = w.toPlainText()
        assert "## Project status" in body
        assert "[doc](https://example.com/d)" in body
        # The lossy plain-text variant should NOT appear.
        assert "Project statusSee doc." not in body
    finally:
        w.deleteLater()


def test_html_paste_preserves_nested_list(qt_app):
    w = LiveNotesWidget()
    try:
        mime = QMimeData()
        mime.setHtml(
            '<ul>'
            '<li>Parent<ul><li>Child</li></ul></li>'
            '</ul>'
        )
        w._editor.insertFromMimeData(mime)  # noqa: SLF001
        body = w.toPlainText()
        assert "- Parent" in body
        assert "  - Child" in body
    finally:
        w.deleteLater()


def test_html_paste_preserves_fenced_code_with_language(qt_app):
    w = LiveNotesWidget()
    try:
        mime = QMimeData()
        mime.setHtml(
            '<pre><code class="language-python">x = 1</code></pre>'
        )
        w._editor.insertFromMimeData(mime)  # noqa: SLF001
        body = w.toPlainText()
        assert "```python" in body
        assert "x = 1" in body
    finally:
        w.deleteLater()


# ---- plain-text fallback ------------------------------------------------


def test_plain_text_only_paste_falls_through(qt_app):
    w = LiveNotesWidget()
    try:
        mime = QMimeData()
        mime.setText("just a plain line")
        # No HTML; no image. Default insertFromMimeData should handle.
        w._editor.insertFromMimeData(mime)  # noqa: SLF001
        body = w.toPlainText()
        assert body == "just a plain line"
    finally:
        w.deleteLater()


def test_empty_html_falls_through_to_plain_text(qt_app):
    """If the HTML variant is empty / strips to nothing, the helper
    returns "" and the editor should fall through to the plain-text
    paste path so the user still gets the text they copied."""
    w = LiveNotesWidget()
    try:
        mime = QMimeData()
        mime.setHtml("")
        mime.setText("fallback content")
        w._editor.insertFromMimeData(mime)  # noqa: SLF001
        body = w.toPlainText()
        assert body == "fallback content"
    finally:
        w.deleteLater()


# ---- image paste path unchanged -----------------------------------------


def test_image_paste_still_routes_to_image_signal(qt_app):
    """The existing image-paste hook must keep priority over the new
    HTML branch. An HTML+image clipboard (some browsers emit both)
    routes to the image handler so the user gets a saved image, not
    a markdown <img> tag pointing at a data: URI.

    Uses the bare _MarkdownEditor (no LiveNotesWidget wrapper) so the
    test doesn't fire LiveNotesWidget._on_image_pasted's QMessageBox
    when session_dir is None -- under offscreen Qt the modal blocks
    the test runner. The routing decision lives entirely in the
    editor's insertFromMimeData override; verifying the signal fires
    is sufficient.
    """
    editor = _MarkdownEditor()
    received: list = []
    editor.image_pasted.connect(received.append)
    try:
        img = QImage(4, 4, QImage.Format.Format_RGB32)
        img.fill(0)
        mime = QMimeData()
        mime.setImageData(img)
        mime.setHtml('<img src="data:image/png;base64,xxx">')
        editor.insertFromMimeData(mime)
        assert len(received) == 1
        # Editor body should NOT contain the HTML img tag or markdown
        # img ref -- the image branch returned early.
        body = editor.toPlainText()
        assert "<img" not in body
        assert "![](" not in body
    finally:
        editor.deleteLater()


# ---- canInsertFromMimeData --------------------------------------------


def test_can_insert_accepts_html(qt_app):
    w = LiveNotesWidget()
    try:
        mime = QMimeData()
        mime.setHtml("<p>x</p>")
        assert w._editor.canInsertFromMimeData(mime) is True  # noqa: SLF001
    finally:
        w.deleteLater()


def test_can_insert_accepts_image(qt_app):
    w = LiveNotesWidget()
    try:
        img = QImage(2, 2, QImage.Format.Format_RGB32)
        img.fill(0)
        mime = QMimeData()
        mime.setImageData(img)
        assert w._editor.canInsertFromMimeData(mime) is True  # noqa: SLF001
    finally:
        w.deleteLater()


def test_markdown_editor_can_be_instantiated_standalone(qt_app):
    """Belt-and-braces: ensure _MarkdownEditor can stand on its own
    without the LiveNotesWidget wrapper, since the paste-hook logic
    is independent of the surrounding widget."""
    editor = _MarkdownEditor()
    try:
        mime = QMimeData()
        mime.setHtml("<p><strong>bold</strong></p>")
        editor.insertFromMimeData(mime)
        assert "**bold**" in editor.toPlainText()
    finally:
        editor.deleteLater()
