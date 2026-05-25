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

from PyQt6.QtCore import Qt
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
        """Load a PDF file. Returns True on success."""
        if not QTPDF_AVAILABLE or self._doc is None:
            return False
        path = Path(path)
        if not path.exists():
            self.clear()
            return False
        try:
            status = self._doc.load(str(path))
        except Exception:
            return False
        # QPdfDocument.Status.Ready / Status.Error in newer Qt; older
        # builds return an int that's non-zero on error.
        try:
            from PyQt6.QtPdf import QPdfDocument as _QPD
            ready = (status == _QPD.Error.None_)
        except (ImportError, AttributeError):
            ready = bool(status == 0) or (
                getattr(self._doc, "status", lambda: None)()
                in (0, None)
            )
        return bool(ready)

    def clear(self) -> None:
        if self._doc is not None:
            try:
                self._doc.close()
            except Exception:
                pass
