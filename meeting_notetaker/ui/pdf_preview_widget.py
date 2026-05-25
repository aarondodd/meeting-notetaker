"""Inline PDF preview via QtPdf (Qt 6.5+) with a graceful fallback.

`QPdfDocument` + `QPdfView` are the right widgets for a read-only
PDF rendering inside a Qt window: they handle page navigation,
zoom, and rendering at native fidelity. We use the multi-page
scroll mode so the user sees the whole document in one continuous
scroll.

When QtPdf isn't available (e.g. a stripped Linux dev install),
the widget falls back to a single QLabel reading "PDF preview not
available". The attachment_preview dispatcher then routes the user
to "Open externally" instead.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget


try:
    from PyQt6.QtPdf import QPdfDocument  # noqa: F401
    from PyQt6.QtPdfWidgets import QPdfView
    QTPDF_AVAILABLE = True
except ImportError:
    QPdfDocument = None  # type: ignore[assignment]
    QPdfView = None      # type: ignore[assignment]
    QTPDF_AVAILABLE = False


class PdfPreviewWidget(QWidget):
    """Render a PDF inline.

    Construct + call `load(path)` whenever the source changes;
    `clear()` empties the widget. The fallback path is silent --
    callers test via `is_supported()` to decide whether to route
    around it.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        if QTPDF_AVAILABLE:
            self._doc = QPdfDocument(self)
            self._view = QPdfView(self)
            self._view.setDocument(self._doc)
            # MultiPage gives a continuous scroll the user expects.
            # SinglePage feels claustrophobic for a multi-page doc.
            try:
                self._view.setPageMode(QPdfView.PageMode.MultiPage)
            except AttributeError:
                pass  # older Qt6 builds without setPageMode
            try:
                self._view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
            except AttributeError:
                pass
            layout.addWidget(self._view, 1)
            self._fallback_label: Optional[QLabel] = None
            # Backing buffer for the currently-loaded document. We
            # load PDFs from an in-memory QBuffer rather than a file
            # path so Qt never holds a Windows file handle on the
            # on-disk attachment -- without that, deleting an
            # attachment while it's open in the preview pane fails
            # with WinError 32 (file in use) and QPdfDocument.close()
            # doesn't synchronously release the handle.
            self._buffer: Optional[QBuffer] = None
            self._buffer_data: Optional[QByteArray] = None
        else:
            self._doc = None
            self._view = None
            self._fallback_label = QLabel(
                "PDF preview is unavailable in this build "
                "(PyQt6.QtPdf is missing). Use \"Open externally\" "
                "to view the file.",
                self,
            )
            self._fallback_label.setWordWrap(True)
            self._fallback_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(self._fallback_label, 1)

    # ---- public API ----
    def is_supported(self) -> bool:
        return QTPDF_AVAILABLE

    def load(self, path: Path) -> bool:
        """Load a PDF file. Returns True on success.

        Reads the file into a QBuffer and hands that to QPdfDocument
        instead of passing the path string. That keeps Qt off the
        filesystem entirely -- the OS handle closes the moment
        read_bytes() returns, so a later unlink on the same path
        works regardless of whether the preview is still showing
        the document. Trade-off: the whole PDF lives in RAM while
        previewed, which is fine for the few-MB attachments this
        tab handles.
        """
        if not QTPDF_AVAILABLE or self._doc is None:
            return False
        path = Path(path)
        if not path.exists():
            self.clear()
            return False
        try:
            raw = path.read_bytes()
        except OSError:
            return False
        # Tear down any previous buffer first so the old QByteArray
        # can be released and a fresh QBuffer owns the new bytes.
        self._release_buffer()
        self._buffer_data = QByteArray(raw)
        self._buffer = QBuffer(self)
        self._buffer.setData(self._buffer_data)
        if not self._buffer.open(QIODevice.OpenModeFlag.ReadOnly):
            self._release_buffer()
            return False
        try:
            self._doc.load(self._buffer)
        except Exception:
            self._release_buffer()
            return False
        # The QIODevice overload of QPdfDocument.load() returns None
        # (it's effectively a void function), so we have to ask the
        # document for its current status rather than trust the
        # return value. Status.Ready means the load completed; we
        # treat anything else -- Error, Loading, Unloading -- as a
        # failure for the purposes of the preview pane.
        try:
            from PyQt6.QtPdf import QPdfDocument as _QPD
            ready = (self._doc.status() == _QPD.Status.Ready)
        except (ImportError, AttributeError):
            ready = bool(self._doc.pageCount() > 0)
        if not ready:
            # Failed loads still get the buffer torn down so the
            # widget doesn't hold the bytes for a doc we can't show.
            try:
                self._doc.close()
            except Exception:
                pass
            self._release_buffer()
        return bool(ready)

    def clear(self) -> None:
        if self._doc is not None:
            try:
                self._doc.close()
            except Exception:
                pass
        self._release_buffer()

    def _release_buffer(self) -> None:
        """Tear down the in-memory backing for the prior load.

        QPdfDocument.close() doesn't always release its reference to
        the QIODevice synchronously, so we close the buffer ourselves
        and drop both Python-side references. The next event-loop
        tick lets Qt finish any pending cleanup.
        """
        if self._buffer is not None:
            try:
                if self._buffer.isOpen():
                    self._buffer.close()
            except Exception:
                pass
            self._buffer = None
        self._buffer_data = None
