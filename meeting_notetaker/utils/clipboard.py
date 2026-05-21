"""Multi-format clipboard helper for note + synthesis copy.

The Copy button on the My Notes and Synthesis tabs needs to round-trip
embedded images through paste targets that don't resolve local file
paths (Notion, Word, OneNote, browsers). Solution: put both formats on
the clipboard via QMimeData --

  - text/plain : the unchanged Markdown source.
  - text/html  : the rendered Markdown converted to HTML, with each
                 local image src rewritten to a data:image/<ext>;base64
                 URI.

Paste targets pick whichever format they prefer. HTML-aware apps inline
the image bytes directly; plain-text editors get the original Markdown
unchanged.

Two layers here so the bytes path is testable without Qt:

  - rewrite_img_srcs_to_data_uris() : pure-Python HTML rewrite. Resolves
    each <img src="relative/path"> against base_dir, base64-encodes the
    file bytes if it exists and lives inside base_dir, replaces the src
    in-place. Out-of-base-dir refs and missing files are left as-is so
    no exception escapes a copy operation.

  - copy_markdown_with_images() : Qt-dependent. Renders Markdown -> HTML
    via QTextDocument.toHtml(), feeds the result through the rewriter,
    sets text/plain + text/html on a QMimeData, hands it to the
    clipboard.
"""
from __future__ import annotations

import base64
import logging
import re
from html import unescape
from pathlib import Path
from typing import Optional


log = logging.getLogger(__name__)


# Mime suffix -> content-type lookup for the data: URI. Limited to the
# image types Qt + the in-app image-paste path produce; falls back to
# octet-stream so unknown types still copy (rendering is up to the paste
# target).
_MIME_BY_EXT: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}


# Match <img ...> tags and capture the src attribute. The HTML emitted
# by QTextDocument.toHtml() uses double-quoted attributes; we accept
# single-quoted too for robustness against hand-crafted HTML.
_IMG_TAG_RE = re.compile(
    r"""(<img\b[^>]*?\bsrc\s*=\s*)(['"])(?P<src>[^'"]+)\2""",
    re.IGNORECASE,
)


def _content_type_for(path: Path) -> str:
    return _MIME_BY_EXT.get(path.suffix.lower(), "application/octet-stream")


def _is_absolute_url(src: str) -> bool:
    """True for http(s):, data:, file:, anything with a URL scheme.

    We never rewrite these; the assumption is the source already
    expresses a paste-ready reference.
    """
    if not src:
        return True
    return (
        src.startswith("http://")
        or src.startswith("https://")
        or src.startswith("data:")
        or src.startswith("file:")
        or src.startswith("mailto:")
    )


def _resolve_safely(base_dir: Path, raw_src: str) -> Optional[Path]:
    """Resolve raw_src against base_dir, refusing parent-dir escapes.

    Returns the resolved path if it exists AND lives at or under
    base_dir; None otherwise. Path traversal attempts
    (e.g. 'images/../../../etc/passwd') are rejected on the
    is_relative_to() check rather than silently followed.
    """
    try:
        resolved = (base_dir / raw_src).resolve(strict=False)
        base_resolved = base_dir.resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    try:
        if not resolved.is_relative_to(base_resolved):
            return None
    except AttributeError:
        # Python 3.8 fallback; the project requires 3.11+ so this
        # shouldn't fire. Belt-and-suspenders for tests in old envs.
        try:
            resolved.relative_to(base_resolved)
        except ValueError:
            return None
    if not resolved.is_file():
        return None
    return resolved


def _encode_as_data_uri(path: Path) -> Optional[str]:
    try:
        data = path.read_bytes()
    except OSError:
        log.warning("could not read image for clipboard: %s", path)
        return None
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{_content_type_for(path)};base64,{encoded}"


def rewrite_img_srcs_to_data_uris(html: str, base_dir: Path) -> str:
    """Rewrite <img src="relative/path"> -> data: URIs against base_dir.

    Absolute URLs (http, https, data, file, mailto) pass through. Refs
    that resolve outside base_dir or to a non-existent file pass through
    unchanged -- fail-soft, since a broken-image result in the paste
    target is no worse than today's behavior.
    """
    if not html:
        return html

    def _replace(match: re.Match[str]) -> str:
        prefix = match.group(1)
        quote = match.group(2)
        src = unescape(match.group("src"))
        if _is_absolute_url(src):
            return match.group(0)
        target = _resolve_safely(base_dir, src)
        if target is None:
            return match.group(0)
        encoded = _encode_as_data_uri(target)
        if encoded is None:
            return match.group(0)
        return f"{prefix}{quote}{encoded}{quote}"

    return _IMG_TAG_RE.sub(_replace, html)


def markdown_to_html_with_images(markdown_text: str, base_dir: Path) -> str:
    """Render Markdown to HTML and inline local image refs as data: URIs.

    Qt-dependent. Returns the rewritten HTML; raises ImportError if
    PyQt6 is not available (the helper is only useful in the running
    Qt app).
    """
    from PyQt6.QtCore import QUrl
    from PyQt6.QtGui import QTextDocument

    doc = QTextDocument()
    # setBaseUrl makes any relative <img src> Qt sees during the
    # markdown parse resolvable against the session dir; we then walk
    # the produced HTML and replace those refs with data: URIs.
    doc.setBaseUrl(QUrl.fromLocalFile(str(base_dir) + "/"))
    doc.setMarkdown(markdown_text)
    html = doc.toHtml()
    return rewrite_img_srcs_to_data_uris(html, base_dir)


def copy_markdown_with_images(
    markdown_text: str,
    base_dir: Path,
    *,
    clipboard=None,
) -> str:
    """Put text/plain + image-inlined text/html on the clipboard.

    Returns the rewritten HTML payload so callers can log size,
    surface diagnostics, or write tests. If `clipboard` is None we
    grab QGuiApplication.clipboard(); passing one explicitly lets
    unit tests inject a fake.
    """
    from PyQt6.QtCore import QMimeData
    from PyQt6.QtGui import QGuiApplication

    html = markdown_to_html_with_images(markdown_text, base_dir)
    mime = QMimeData()
    mime.setText(markdown_text)
    mime.setHtml(html)

    target = clipboard if clipboard is not None else QGuiApplication.clipboard()
    target.setMimeData(mime)
    return html
