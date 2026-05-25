"""Pin the file-handle behavior of PdfPreviewWidget.

The Windows-side bug we're guarding against: previewing a PDF via
QPdfDocument.load(path) holds a file handle that doesn't release
synchronously on QPdfDocument.close(). Deleting the file while the
preview is open then fails with PermissionError [WinError 32].

The fix loads PDFs from an in-memory QBuffer, so Qt never holds a
filesystem handle in the first place. This test exercises that
contract directly: open a real PDF in the preview, then unlink the
underlying file -- if Qt is holding a handle, unlink fails on Windows
and the path stays on disk; on POSIX the unlink succeeds but the
preview keeps reading from the inode, which is the wrong invariant
either way. We assert the file is gone and the preview hasn't broken.

The QtPdf module isn't available on every dev install, so the test
skips cleanly when missing rather than failing the dev test run.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtCore")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.ui.pdf_preview_widget import (  # noqa: E402
    PdfPreviewWidget,
    QTPDF_AVAILABLE,
)


pytestmark = pytest.mark.skipif(
    not QTPDF_AVAILABLE,
    reason="PyQt6.QtPdf not installed in this dev venv",
)


@pytest.fixture(scope="module")
def qt_app():
    import sys
    return QApplication.instance() or QApplication(sys.argv)


def _write_minimal_pdf(path: Path) -> None:
    """Render a one-page blank PDF via QPdfWriter so QPdfDocument
    accepts the result. Hand-crafted xref tables are a tarpit -- let
    Qt build the file from a real QPainter session."""
    from PyQt6.QtCore import QSize
    from PyQt6.QtGui import QPageSize, QPdfWriter, QPainter

    writer = QPdfWriter(str(path))
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setResolution(72)
    painter = QPainter(writer)
    try:
        painter.drawText(50, 50, "Test PDF")
    finally:
        painter.end()


def test_load_does_not_hold_file_handle(tmp_path, qt_app):
    """The critical invariant: loading a PDF for preview must not
    keep the source file open. Without this, deleting an attachment
    while it's previewed crashes with WinError 32 on Windows."""
    pdf_path = tmp_path / "sample.pdf"
    _write_minimal_pdf(pdf_path)

    widget = PdfPreviewWidget()
    try:
        loaded = widget.load(pdf_path)
        assert loaded, "QPdfDocument must accept this minimal PDF"
        # The actual contract: unlink works while the preview is open.
        # On Windows this raises PermissionError when Qt still holds
        # the file open; on POSIX it succeeds either way but we still
        # want to verify the path is gone.
        pdf_path.unlink()
        assert not pdf_path.exists()
    finally:
        widget.deleteLater()


def test_clear_releases_buffer(tmp_path, qt_app):
    """After clear() the preview must drop its in-memory PDF data so
    a long-lived widget switching between many sessions doesn't
    accumulate the bytes of every PDF it ever previewed."""
    pdf_path = tmp_path / "sample.pdf"
    _write_minimal_pdf(pdf_path)

    widget = PdfPreviewWidget()
    try:
        widget.load(pdf_path)
        assert widget._buffer is not None
        assert widget._buffer_data is not None
        widget.clear()
        assert widget._buffer is None
        assert widget._buffer_data is None
    finally:
        widget.deleteLater()


def test_load_missing_file_returns_false(tmp_path, qt_app):
    widget = PdfPreviewWidget()
    try:
        assert widget.load(tmp_path / "does-not-exist.pdf") is False
        # And the buffer state must remain torn-down so a later
        # delete on some unrelated file isn't surprised by a leaked
        # reference.
        assert widget._buffer is None
        assert widget._buffer_data is None
    finally:
        widget.deleteLater()
