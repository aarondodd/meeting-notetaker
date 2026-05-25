"""Type-dispatching preview pane for the Attachments tab.

Given a file path, picks the right inline preview:

* Images (PNG/JPG/GIF/WEBP/BMP) -> ScaledImageLabel.
* Plain text + markdown + source code -> QPlainTextEdit (read-only)
  or MarkdownPreview for `.md`.
* Audio (MP3/WAV/Opus/FLAC/M4A/etc.) -> AudioPlayer + scrubber.
* PDF -> PdfPreviewWidget (QtPdf-backed, with fallback).
* Office (.docx, .xlsx, .pptx, .doc, .xls, .ppt, .csv) -> async
  COM convert-to-PDF -> reuse the PDF preview widget.
* Video + everything else -> metadata + "Open externally" button.

When QtPdf isn't available, PDF + Office files fall through to the
unsupported pane (the user gets metadata + Open externally).
"""
from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QPixmap
from PyQt6.QtWidgets import (
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..utils.office_preview import (
    convert_office_to_pdf,
    is_office_extension,
    supported_extensions as office_supported_extensions,
)
from .markdown_preview import MarkdownPreview
from .pdf_preview_widget import PdfPreviewWidget, QTPDF_AVAILABLE
from .scaled_image_label import ScaledImageLabel


log = logging.getLogger(__name__)


_IMAGE_EXTS = frozenset({"png", "jpg", "jpeg", "gif", "webp", "bmp", "tiff", "tif"})
_AUDIO_EXTS = frozenset({"mp3", "wav", "opus", "flac", "m4a", "aac", "ogg"})
_VIDEO_EXTS = frozenset({"mp4", "mov", "webm", "mkv", "avi", "wmv"})
_TEXT_EXTS = frozenset({
    "txt", "log", "csv", "tsv", "json", "yaml", "yml", "xml", "ini", "toml",
    "py", "js", "ts", "tsx", "jsx", "html", "css", "sh", "ps1", "bat",
    "c", "cpp", "h", "hpp", "rs", "go", "java", "rb", "php", "sql",
    "conf", "cfg", "env",
})
_MARKDOWN_EXTS = frozenset({"md", "markdown"})


# Page indices on the inner QStackedWidget. Each preview type owns one.
_PAGE_PLACEHOLDER = 0
_PAGE_IMAGE = 1
_PAGE_TEXT = 2
_PAGE_MARKDOWN = 3
_PAGE_PDF = 4
_PAGE_UNSUPPORTED = 5
_PAGE_CONVERTING = 6


class AttachmentPreview(QWidget):
    """Single widget that swaps internal panes based on the file
    type passed to `load_file(path)`."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._stack = QStackedWidget(self)
        layout.addWidget(self._stack, 1)

        # Page 0: placeholder ("select an attachment").
        self._placeholder = QLabel(
            "Select an attachment to preview.", self._stack,
        )
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setStyleSheet("color: gray;")
        self._stack.addWidget(self._placeholder)

        # Page 1: image.
        self._image_label = ScaledImageLabel(self._stack)
        self._stack.addWidget(self._image_label)

        # Page 2: text.
        self._text_view = QPlainTextEdit(self._stack)
        self._text_view.setReadOnly(True)
        self._text_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._stack.addWidget(self._text_view)

        # Page 3: markdown.
        self._markdown_view = MarkdownPreview(self._stack)
        self._stack.addWidget(self._markdown_view)

        # Page 4: PDF (also reused for Office-converted PDFs).
        self._pdf_view = PdfPreviewWidget(self._stack)
        self._stack.addWidget(self._pdf_view)

        # Page 5: unsupported -- metadata + open externally button.
        unsupported_widget = QWidget(self._stack)
        unsupported_layout = QVBoxLayout(unsupported_widget)
        unsupported_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._unsupported_label = QLabel("", unsupported_widget)
        self._unsupported_label.setWordWrap(True)
        self._unsupported_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        unsupported_layout.addWidget(self._unsupported_label)
        self._open_externally_btn = QPushButton(
            "Open externally", unsupported_widget,
        )
        self._open_externally_btn.clicked.connect(self._on_open_externally)
        self._open_externally_btn.setMaximumWidth(220)
        unsupported_layout.addWidget(
            self._open_externally_btn,
            alignment=Qt.AlignmentFlag.AlignCenter,
        )
        self._stack.addWidget(unsupported_widget)

        # Page 6: converting (spinner-text while Office worker runs).
        self._converting_label = QLabel(
            "Converting Office document for preview...",
            self._stack,
        )
        self._converting_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._converting_label.setStyleSheet("color: gray;")
        self._stack.addWidget(self._converting_label)

        self._current_path: Optional[Path] = None
        self._office_worker: Optional[_OfficeConversionWorker] = None
        self.clear()

    # ---- public API ----
    def load_file(self, path: Path) -> None:
        """Display whatever preview applies to `path`."""
        self._cancel_office_worker()
        if path is None:
            self.clear()
            return
        path = Path(path)
        if not path.exists():
            self._show_unsupported(
                f"File not found: {path.name}", path,
            )
            return
        self._current_path = path
        ext = path.suffix.lower().lstrip(".")
        if ext in _IMAGE_EXTS:
            self._show_image(path)
        elif ext in _MARKDOWN_EXTS:
            self._show_markdown(path)
        elif ext in _TEXT_EXTS:
            self._show_text(path)
        elif ext == "pdf":
            self._show_pdf(path)
        elif is_office_extension(ext):
            self._show_office(path)
        elif ext in _AUDIO_EXTS or ext in _VIDEO_EXTS:
            self._show_unsupported(
                f"Audio / video preview isn't supported inline yet.",
                path,
            )
        else:
            self._show_unsupported(
                f"Preview not supported for this file type ({ext or 'unknown'}).",
                path,
            )

    def clear(self) -> None:
        self._cancel_office_worker()
        self._current_path = None
        self._stack.setCurrentIndex(_PAGE_PLACEHOLDER)
        self._image_label.set_image_path(None)
        self._text_view.clear()
        self._markdown_view.clear()
        self._pdf_view.clear()

    # ---- per-type loaders ----
    def _show_image(self, path: Path) -> None:
        self._image_label.set_image_path(path)
        self._stack.setCurrentIndex(_PAGE_IMAGE)

    def _show_text(self, path: Path) -> None:
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            self._show_unsupported(f"Could not read file: {exc}", path)
            return
        # 100k char cap on the text preview; anything larger is
        # probably not a useful "skim" target and risks freezing
        # the QPlainTextEdit on layout.
        if len(body) > 100_000:
            body = body[:100_000] + "\n\n... (file truncated for preview)"
        self._text_view.setPlainText(body)
        self._stack.setCurrentIndex(_PAGE_TEXT)

    def _show_markdown(self, path: Path) -> None:
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            self._show_unsupported(f"Could not read file: {exc}", path)
            return
        self._markdown_view.setSearchPaths([str(path.parent)])
        self._markdown_view.setMarkdown(body)
        self._stack.setCurrentIndex(_PAGE_MARKDOWN)

    def _show_pdf(self, path: Path) -> None:
        if not QTPDF_AVAILABLE:
            self._show_unsupported(
                "PDF preview is unavailable in this build "
                "(PyQt6.QtPdf is missing).",
                path,
            )
            return
        if not self._pdf_view.load(path):
            self._show_unsupported(
                "Could not load the PDF for preview.",
                path,
            )
            return
        self._stack.setCurrentIndex(_PAGE_PDF)

    def _show_office(self, path: Path) -> None:
        """Run COM conversion on a worker thread; reuse PDF preview."""
        if not QTPDF_AVAILABLE:
            self._show_unsupported(
                "Office preview requires the PDF viewer "
                "(PyQt6.QtPdf is missing in this build).",
                path,
            )
            return
        self._stack.setCurrentIndex(_PAGE_CONVERTING)
        self._converting_label.setText(
            f"Converting {path.name} for preview..."
        )
        worker = _OfficeConversionWorker(path)
        worker.conversion_done.connect(
            lambda pdf, src=path: self._on_office_converted(src, pdf)
        )
        worker.finished.connect(worker.deleteLater)
        self._office_worker = worker
        worker.start()

    def _on_office_converted(
        self, src_path: Path, pdf_path: Optional[Path],
    ) -> None:
        # Avoid stale callbacks landing on a different selection.
        if self._current_path != src_path:
            return
        self._office_worker = None
        if pdf_path is None:
            self._show_unsupported(
                "Could not convert this Office document. Office may "
                "not be installed, or the file may be open elsewhere.",
                src_path,
            )
            return
        if not self._pdf_view.load(pdf_path):
            self._show_unsupported(
                "The conversion succeeded but the PDF wouldn't load. "
                "Use Open externally to view the file.",
                src_path,
            )
            return
        self._stack.setCurrentIndex(_PAGE_PDF)

    def _show_unsupported(self, message: str, path: Path) -> None:
        size = ""
        try:
            size_bytes = path.stat().st_size
            size = f"\n\n{_format_size(size_bytes)}"
        except OSError:
            pass
        mime, _ = mimetypes.guess_type(str(path))
        mime_str = f" ({mime})" if mime else ""
        self._unsupported_label.setText(
            f"<b>{path.name}</b>{mime_str}{size}\n\n{message}"
        )
        self._stack.setCurrentIndex(_PAGE_UNSUPPORTED)

    def _on_open_externally(self) -> None:
        if self._current_path is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._current_path)))

    def _cancel_office_worker(self) -> None:
        if self._office_worker is not None and self._office_worker.isRunning():
            # Best-effort cancel: disconnect the signal so a late
            # finish doesn't paint over the new selection.
            try:
                self._office_worker.conversion_done.disconnect()
            except TypeError:
                pass
        self._office_worker = None


class _OfficeConversionWorker(QThread):
    """Off-UI-thread Office -> PDF conversion."""

    conversion_done = pyqtSignal(object)  # Optional[Path]

    def __init__(self, src_path: Path) -> None:
        super().__init__()
        self._src_path = src_path

    def run(self) -> None:  # type: ignore[override]
        try:
            pdf = convert_office_to_pdf(self._src_path)
        except Exception:
            log.exception("Office conversion worker crashed")
            pdf = None
        self.conversion_done.emit(pdf)


def _format_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.0f} TB"
