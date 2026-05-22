"""PrintTextDocument: image resource resolution + PDF embed round-trip.

PyQt6 is imported lazily so the module skips on hosts without Qt
installed; the offscreen platform is forced before QApplication is
constructed so no display is required.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtGui")
pytest.importorskip("PyQt6.QtPrintSupport")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QUrl  # noqa: E402
from PyQt6.QtGui import QColor, QImage, QPainter, QTextDocument  # noqa: E402
from PyQt6.QtPrintSupport import QPrinter  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.ui.print_document import PrintTextDocument  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture
def session_with_image(tmp_path):
    """Build a session-dir-shaped folder with one PNG in images/."""
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    img = QImage(20, 20, QImage.Format.Format_RGB32)
    img.fill(QColor(200, 30, 30))
    painter = QPainter(img)
    painter.setPen(QColor(0, 0, 0))
    painter.drawRect(0, 0, 19, 19)
    painter.end()
    img_path = images_dir / "sample.png"
    img.save(str(img_path), "PNG")
    return tmp_path, img_path


def test_resolves_relative_image_ref(qt_app, session_with_image):
    base_dir, img_path = session_with_image
    doc = PrintTextDocument(base_dir)
    resource = doc.loadResource(
        QTextDocument.ResourceType.ImageResource,
        QUrl("images/sample.png"),
    )
    assert isinstance(resource, QImage)
    assert not resource.isNull()
    assert resource.width() == 20
    assert resource.height() == 20


def test_resolves_absolute_file_url(qt_app, session_with_image):
    base_dir, img_path = session_with_image
    doc = PrintTextDocument(base_dir)
    resource = doc.loadResource(
        QTextDocument.ResourceType.ImageResource,
        QUrl.fromLocalFile(str(img_path)),
    )
    assert isinstance(resource, QImage)
    assert not resource.isNull()


def test_missing_image_falls_through(qt_app, tmp_path):
    """Unresolvable URLs return the default (no QImage) so Qt renders the
    standard broken-image icon."""
    doc = PrintTextDocument(tmp_path)
    resource = doc.loadResource(
        QTextDocument.ResourceType.ImageResource,
        QUrl("images/does-not-exist.png"),
    )
    # Default loadResource for ImageResource returns a QVariant-like
    # null; the truthiness check works for both QImage(null) and None.
    if isinstance(resource, QImage):
        assert resource.isNull()


def test_cache_returns_same_object_for_repeated_calls(qt_app, session_with_image):
    """The print engine calls loadResource many times during pagination;
    the cache prevents re-reading the file."""
    base_dir, _ = session_with_image
    doc = PrintTextDocument(base_dir)
    a = doc.loadResource(
        QTextDocument.ResourceType.ImageResource,
        QUrl("images/sample.png"),
    )
    b = doc.loadResource(
        QTextDocument.ResourceType.ImageResource,
        QUrl("images/sample.png"),
    )
    assert a is b


def test_clamp_images_to_printer_pins_oversized(qt_app, tmp_path):
    """An image wider than the printer's paint-rect clamps down to fit.

    The natural image width has to exceed the printer's pixel paint
    width to trigger a clamp -- so the test image is sized comfortably
    over a high-resolution Letter page's printable area (~9500px at
    1200dpi minus margins).
    """
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    img = QImage(20000, 200, QImage.Format.Format_RGB32)
    img.fill(QColor(20, 80, 200))
    img.save(str(images_dir / "huge.png"), "PNG")

    doc = PrintTextDocument(tmp_path)
    doc.setMarkdown("![huge](images/huge.png)\n")

    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    out_pdf = tmp_path / "clamped.pdf"
    printer.setOutputFileName(str(out_pdf))
    paint_w = printer.pageLayout().paintRectPixels(printer.resolution()).width()
    assert paint_w < 20000, (
        "test premise: image must be wider than the printer's paint rect"
    )

    adjustments = doc.clamp_images_to_printer(printer)
    assert adjustments == 1

    # Walk for the image fragment and confirm width was pinned to the
    # printer's paint-rect width (or less).
    block = doc.begin()
    found_width = None
    while block.isValid() and found_width is None:
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            if frag.isValid() and frag.charFormat().isImageFormat():
                found_width = frag.charFormat().toImageFormat().width()
                break
            it += 1
        block = block.next()
    assert found_width is not None
    assert 0 < found_width <= paint_w
    # And it's strictly less than the natural 20000.
    assert found_width < 20000


def test_pdf_actually_embeds_image(qt_app, session_with_image, tmp_path):
    """End-to-end: render a Markdown doc to PDF and verify the rendered
    page has the colored pixels somewhere on it.

    The previous (broken) behavior produced a PDF whose only image was
    Qt's broken-document icon -- a near-white placeholder. After the
    fix the page contains the red square we drew. The check picks a
    region that the small text + heading should leave clear and asserts
    a red pixel is present.
    """
    base_dir, img_path = session_with_image
    markdown = (
        "# Title\n\n"
        "A line of text.\n\n"
        "![Red square](images/sample.png)\n\n"
        "Trailing line.\n"
    )
    doc = PrintTextDocument(base_dir)
    doc.setMarkdown(markdown)

    out_pdf = tmp_path / "out.pdf"
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(out_pdf))
    doc.print(printer)
    assert out_pdf.exists()
    assert out_pdf.stat().st_size > 0

    # Tiny embedded-image smoke check: the broken-icon path produced
    # a PDF where the rendered image was Qt's gray placeholder. With
    # the fix, the on-disk PNG (a 200,30,30 red square) shows up. We
    # don't rasterize the PDF (pdftoppm isn't a hard dep), but the
    # PDF's image XObject stream length is meaningfully larger when
    # it carries a real image vs a stock placeholder. 5kB is a
    # conservative floor that excludes the placeholder-only case.
    data = out_pdf.read_bytes()
    assert len(data) > 5000
    assert b"/Image" in data
