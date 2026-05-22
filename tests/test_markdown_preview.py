"""MarkdownPreview: image clamping + Copy Image context-menu action.

The widget addresses two bugs reported in v0.6.3:

1. Right-click on an image showed only the stock QTextBrowser menu, in
   which "Copy" is greyed out unless there's a text selection. There's
   now an explicit "Copy Image" action that puts the QImage on the
   clipboard.
2. Images in the Preview and PDF export rendered at their native pixel
   size and extended past the visible viewport / page width. The
   preview re-clamps on resize; the print path clamps to the printer's
   paint rect.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtGui")
pytest.importorskip("PyQt6.QtWidgets")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, QUrl  # noqa: E402
from PyQt6.QtGui import (  # noqa: E402
    QColor,
    QImage,
    QPainter,
    QTextCursor,
    QTextDocument,
    QTextImageFormat,
)
from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.ui.markdown_preview import (  # noqa: E402
    MarkdownPreview,
    clamp_image_widths,
)


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture
def session_with_wide_image(tmp_path):
    """A session-dir-shaped folder with a 1600px-wide PNG."""
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    img = QImage(1600, 200, QImage.Format.Format_RGB32)
    img.fill(QColor(40, 120, 200))
    painter = QPainter(img)
    painter.setPen(QColor(255, 255, 255))
    painter.drawRect(0, 0, 1599, 199)
    painter.end()
    img_path = images_dir / "wide.png"
    img.save(str(img_path), "PNG")
    return tmp_path, img_path


def _doc_with_image(base_dir: Path) -> QTextDocument:
    doc = QTextDocument()
    doc.setBaseUrl(QUrl.fromLocalFile(str(base_dir) + "/"))
    doc.setMarkdown(
        "# Heading\n\nSome text.\n\n![wide](images/wide.png)\n\nMore text.\n"
    )
    return doc


def _image_format_for(doc: QTextDocument) -> QTextImageFormat:
    """Return the first QTextImageFormat in the document."""
    block = doc.begin()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            if frag.isValid() and frag.charFormat().isImageFormat():
                return frag.charFormat().toImageFormat()
            it += 1
        block = block.next()
    raise AssertionError("no image fragment found in document")


def test_clamp_pins_oversized_image(qt_app, session_with_wide_image):
    base_dir, _ = session_with_wide_image
    doc = _doc_with_image(base_dir)
    # Pre-clamp: width is 0 (unset -> renders at natural 1600px).
    assert _image_format_for(doc).width() == 0

    adjustments = clamp_image_widths(doc, max_width=800)
    assert adjustments == 1
    assert _image_format_for(doc).width() == pytest.approx(800.0)


def test_clamp_releases_when_room_returns(qt_app, session_with_wide_image):
    """Growing the viewport past natural width unpins the format."""
    base_dir, _ = session_with_wide_image
    doc = _doc_with_image(base_dir)

    clamp_image_widths(doc, max_width=600)
    assert _image_format_for(doc).width() == pytest.approx(600.0)
    # Now room beyond natural -> width should reset to 0 (natural).
    clamp_image_widths(doc, max_width=2000)
    assert _image_format_for(doc).width() == 0


def test_clamp_leaves_small_image_alone(qt_app, tmp_path):
    """A 100px image in a 1000px-wide window stays at natural size."""
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    img = QImage(100, 50, QImage.Format.Format_RGB32)
    img.fill(QColor(0, 200, 0))
    img.save(str(images_dir / "small.png"), "PNG")

    doc = QTextDocument()
    doc.setBaseUrl(QUrl.fromLocalFile(str(tmp_path) + "/"))
    doc.setMarkdown("![s](images/small.png)\n")
    adjustments = clamp_image_widths(doc, max_width=1000)
    # No pin needed; clamp is a no-op on already-fitting images.
    assert adjustments == 0
    assert _image_format_for(doc).width() == 0


def test_clamp_ignores_zero_max_width(qt_app, session_with_wide_image):
    """Edge case: widget hasn't been laid out yet (viewport=0). Skip."""
    base_dir, _ = session_with_wide_image
    doc = _doc_with_image(base_dir)
    assert clamp_image_widths(doc, max_width=0) == 0
    assert clamp_image_widths(doc, max_width=-50) == 0


def test_clamp_handles_missing_image_file(qt_app, tmp_path):
    """An unresolvable image ref must not raise -- clamp just skips it."""
    doc = QTextDocument()
    doc.setBaseUrl(QUrl.fromLocalFile(str(tmp_path) + "/"))
    doc.setMarkdown("![missing](images/does-not-exist.png)\n")
    # The fragment exists but its resource resolves to a null QImage.
    # We expect clamp_image_widths to return 0 (nothing to do) without
    # raising.
    assert clamp_image_widths(doc, max_width=400) == 0


def test_preview_clamps_on_set_markdown(qt_app, session_with_wide_image):
    """End-to-end: setMarkdown on the widget pins oversized images."""
    base_dir, _ = session_with_wide_image
    w = MarkdownPreview()
    w.resize(640, 480)
    w.setSearchPaths([str(base_dir)])
    w.show()
    qt_app.processEvents()
    w.setMarkdown("![wide](images/wide.png)\n")
    qt_app.processEvents()

    fmt = _image_format_for(w.document())
    # Viewport is ~640 minus padding; clamped width should be strictly
    # less than the natural 1600.
    assert 0 < fmt.width() < 1600
    w.close()


def test_preview_reclamps_on_resize(qt_app, session_with_wide_image):
    """Resizing the widget triggers a re-clamp; growing past natural
    unpins to native width."""
    base_dir, _ = session_with_wide_image
    w = MarkdownPreview()
    w.setSearchPaths([str(base_dir)])
    w.resize(640, 480)
    w.show()
    qt_app.processEvents()
    w.setMarkdown("![wide](images/wide.png)\n")
    qt_app.processEvents()
    narrow_width = _image_format_for(w.document()).width()
    assert 0 < narrow_width < 1600

    # Grow well past the image's natural 1600 width.
    w.resize(2400, 600)
    qt_app.processEvents()
    # The clamp should release.
    wide_width = _image_format_for(w.document()).width()
    assert wide_width == 0
    w.close()


def test_copy_image_action_puts_image_on_clipboard(
    qt_app, session_with_wide_image
):
    """Calling the Copy Image handler directly with a known QImage puts
    it on the system clipboard. The right-click hit-test is exercised
    separately; here we pin the clipboard contract."""
    base_dir, img_path = session_with_wide_image
    w = MarkdownPreview()
    w.setSearchPaths([str(base_dir)])
    w.setMarkdown("![wide](images/wide.png)\n")
    qt_app.processEvents()

    # Use the widget's own resolver so we go through the same QPixmap
    # -> QImage path the right-click handler uses in production.
    resolved = w._resolve_image("images/wide.png")  # noqa: SLF001
    assert isinstance(resolved, QImage) and not resolved.isNull()

    # Empty the clipboard, then run the handler.
    clipboard = QApplication.clipboard()
    clipboard.clear()
    w._copy_image_to_clipboard(resolved)  # noqa: SLF001
    qt_app.processEvents()

    clipped = clipboard.image()
    assert not clipped.isNull()
    assert clipped.width() == resolved.width()
    assert clipped.height() == resolved.height()
    w.close()


def test_image_name_at_returns_url_for_image_position(
    qt_app, session_with_wide_image
):
    """Hit-test helper returns the image's source URL when the cursor
    position lands on the image character; empty string otherwise."""
    base_dir, _ = session_with_wide_image
    w = MarkdownPreview()
    w.setSearchPaths([str(base_dir)])
    w.resize(900, 600)
    w.show()
    qt_app.processEvents()
    w.setMarkdown(
        "Some preamble text.\n\n![wide](images/wide.png)\n\nTrailing text.\n"
    )
    qt_app.processEvents()

    # Find the image fragment's position in the document.
    doc = w.document()
    block = doc.begin()
    image_pos = -1
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            if frag.isValid() and frag.charFormat().isImageFormat():
                image_pos = frag.position()
                break
            it += 1
        if image_pos >= 0:
            break
        block = block.next()
    assert image_pos >= 0, "image fragment not found"

    # Translate that document position to a viewport coordinate.
    cursor = QTextCursor(doc)
    cursor.setPosition(image_pos)
    rect = w.cursorRect(cursor)
    # Probe a pixel a couple of px to the right of the cursor so we're
    # inside the image glyph rather than at its leading edge.
    probe = QPoint(rect.x() + 4, rect.y() + max(2, rect.height() // 2))
    name = w._image_name_at(probe)  # noqa: SLF001
    assert name == "images/wide.png"

    # Probe a clearly text-only position (start of the preamble).
    text_name = w._image_name_at(QPoint(5, 5))  # noqa: SLF001
    assert text_name == ""
    w.close()
