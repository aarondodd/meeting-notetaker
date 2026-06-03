"""QTextDocument subclass that reliably resolves image refs for printing.

QTextDocument.setBaseUrl() works for the in-app Preview (QTextBrowser
respects setSearchPaths), but is unreliable when the same document is
handed to QPrinter -- the print engine makes many loadResource() calls
during pagination and the base URL is honored on the first call only;
subsequent calls receive the raw relative ref ("images/foo.png") and
the default implementation returns nothing, so the image is drawn as
Qt's "broken document" placeholder icon.

Overriding loadResource() and reading the file off disk directly gives
the print engine a real QImage on every call. The cache prevents
re-reading the same file once per page.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QImage, QPageLayout, QTextDocument

from .markdown_preview import clamp_image_widths


# Default stylesheet applied to every printable document. Sets the
# anchor color to black with an underline so Markdown links remain
# identifiable as links in the rendered PDF / printed page without
# Qt's default cyan, which is hard to read on white. setHtml /
# setMarkdown still emit clickable anchors in the PDF; only the
# visual treatment changes (#78).
_PRINT_STYLESHEET = """
a { color: black; text-decoration: underline; }
a:visited { color: black; }
"""


class PrintTextDocument(QTextDocument):
    """QTextDocument that loads images relative to a base directory.

    Markdown like `![alt](images/foo.png)` resolves against `base_dir`;
    absolute `file://` refs resolve to the path they encode. Anything
    that doesn't resolve falls back to QTextDocument's default behavior
    (which the print engine will render as a broken-image icon -- same
    as today).
    """

    def __init__(self, base_dir: Path, parent=None) -> None:
        super().__init__(parent)
        self._base_dir = Path(base_dir)
        # Cache per resolved URL string. The print engine asks for the
        # same image many times during pagination; reading the bytes
        # once is enough.
        self._image_cache: dict[str, QImage] = {}
        # Mirror setBaseUrl so QTextBrowser-style behaviors (e.g. an
        # in-app preview that reuses the same doc) keep working.
        self.setBaseUrl(QUrl.fromLocalFile(str(self._base_dir) + "/"))
        # Default stylesheet must land BEFORE any setMarkdown / setHtml
        # call -- Qt resolves CSS at render time and caches the parse,
        # so a later setDefaultStyleSheet doesn't repaint existing
        # content.
        self.setDefaultStyleSheet(_PRINT_STYLESHEET)

    def loadResource(self, resource_type, name):  # type: ignore[override]
        if int(resource_type) == int(QTextDocument.ResourceType.ImageResource):
            resolved = self._resolve_image_path(name)
            if resolved is not None:
                key = str(resolved)
                cached = self._image_cache.get(key)
                if cached is not None:
                    return cached
                img = QImage(key)
                if not img.isNull():
                    self._image_cache[key] = img
                    return img
        return super().loadResource(resource_type, name)

    def clamp_images_to_printer(self, printer) -> int:
        """Pin every image's rendered width to fit the printer's page.

        Without this step, an image whose natural pixel size exceeds the
        printable page width spills off the right margin in the rendered
        PDF (and gets clipped by the print engine in hard-copy output).
        Call this after setMarkdown() and before doc.print(printer).

        Uses the same walk as the in-app preview. The clamp value is the
        printer's paint rect width in *points* (1/72 inch). That matches
        the unit QTextDocument::print() picks for the page size when the
        document's own pageSize is left unset -- which is the case here.
        Touching pageSize directly skews the print engine's scale ratio
        (printerPageRect / pageSize) and shrinks all the body text to
        match; this path leaves pagination alone and only adjusts the
        per-image format width.
        """
        layout = printer.pageLayout()
        paint_rect = layout.paintRect(QPageLayout.Unit.Point)
        if paint_rect.width() <= 0 or paint_rect.height() <= 0:
            return 0
        return clamp_image_widths(self, float(paint_rect.width()))

    def _resolve_image_path(self, name) -> Optional[Path]:
        """Resolve a QUrl (or string-coercible) to an absolute Path on disk.

        Returns None when the candidate doesn't exist; the caller falls
        back to the default loadResource path in that case.
        """
        url_str = name.toString() if hasattr(name, "toString") else str(name)
        if not url_str:
            return None
        if url_str.startswith("file://"):
            parsed = urlparse(url_str)
            path_str = unquote(parsed.path)
            # urlparse on Windows leaves the leading slash before the
            # drive letter (e.g. /C:/foo); strip it so Path() handles
            # the absolute form correctly.
            if sys.platform.startswith("win") and path_str.startswith("/"):
                path_str = path_str.lstrip("/")
            candidate = Path(path_str)
        else:
            candidate = self._base_dir / url_str

        try:
            if candidate.exists():
                return candidate
        except OSError:
            return None
        return None
