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

Three layers, ordered so the bytes path is testable without Qt:

  - sanitize_qt_html() : pure-Python pass that strips Qt-specific noise
    from QTextDocument.toHtml() output. Qt emits `<meta name="qrichtext"
    content="1">` and a head `<style>` block with `::marker` unicode
    escapes -- some paste targets (Microsoft Teams desktop, Outlook on
    the Web) see those, decide the content is Qt-internal, and fall
    back to the text/plain side. Inline `-qt-*` CSS properties are
    non-standard and trip strict CSS sanitizers. Stripping all three
    gives Teams/OWA a clean enough HTML payload that they keep the
    formatting (data: URI images still strip there by Microsoft's
    web-paste sanitizer; that's a fundamental limit, not an HTML
    quality issue).

  - rewrite_img_srcs_to_data_uris() : pure-Python HTML rewrite. Resolves
    each <img src="relative/path"> against base_dir, base64-encodes the
    file bytes if it exists and lives inside base_dir, replaces the src
    in-place. Out-of-base-dir refs and missing files are left as-is so
    no exception escapes a copy operation.

  - copy_markdown_with_images() : Qt-dependent. Renders Markdown -> HTML
    via QTextDocument.toHtml(), runs the sanitizer + image rewrite,
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


# Qt-specific noise that trips Teams/OWA paste sanitizers. These run
# in order against the toHtml() output; the result still validates as
# a well-formed HTML document but no longer signals "Qt internal."
_QRICHTEXT_META_RE = re.compile(
    r"""<meta\b[^>]*\bname\s*=\s*["']qrichtext["'][^>]*/?>""",
    re.IGNORECASE,
)
# Qt's <head><style> block has hardcoded `\2610` / `\2612` marker
# escapes for checkbox glyphs that no other renderer understands.
# Strip the whole element rather than trying to surgically patch it.
_HEAD_STYLE_BLOCK_RE = re.compile(
    r"""<style\b[^>]*>.*?</style>""",
    re.IGNORECASE | re.DOTALL,
)
# Match every style="..." attribute. We walk the captured CSS payload
# and remove Qt-specific properties; the cleaned-up attribute is
# rebuilt without them. Standard CSS (font-weight, font-style, etc.)
# survives so bold + italic still render in target apps.
_STYLE_ATTR_RE = re.compile(
    r"""\bstyle\s*=\s*"([^"]*)"|\bstyle\s*=\s*'([^']*)'""",
    re.IGNORECASE,
)
# CSS properties that exist purely for Qt's own rendering and that
# either (a) are non-standard (`-qt-*`) or (b) tend to fight the
# destination app's layout for no semantic reason.
_QT_STYLE_PROPS_TO_DROP: tuple[str, ...] = (
    "-qt-block-indent",
    "-qt-list-indent",
    "-qt-paragraph-type",
    "-qt-user-state",
    # Qt's verbose per-element margins/padding are tautological with
    # the default block-layout that any rich-text receiver applies
    # itself, but they confuse some paste pipelines into thinking the
    # content carries authored styling. Drop them.
    "margin-top",
    "margin-bottom",
    "margin-left",
    "margin-right",
    "text-indent",
    # Qt emits a body-wide font-family ("Sans Serif") that overrides
    # whatever font the destination uses; let the destination decide.
    "font-family",
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


def _clean_style_payload(payload: str) -> str:
    """Drop Qt-specific properties from a single `style="..."` value."""
    out_parts: list[str] = []
    for raw in payload.split(";"):
        chunk = raw.strip()
        if not chunk:
            continue
        prop, sep, _value = chunk.partition(":")
        if not sep:
            # Malformed CSS chunk -- drop rather than re-emit broken syntax.
            continue
        prop_name = prop.strip().lower()
        if prop_name in _QT_STYLE_PROPS_TO_DROP:
            continue
        out_parts.append(chunk)
    return "; ".join(out_parts)


def sanitize_qt_html(html: str) -> str:
    """Strip Qt-specific markers + non-standard CSS from QTextDocument output.

    Run before clipboard.setHtml(). Removes:

    - the `<meta name="qrichtext">` declaration that signals Qt-internal
      content (Microsoft Teams + Outlook Web fall back to text/plain
      when they detect this),
    - the `<head><style>` block that ships Qt's own list-marker
      definitions (apps that don't understand `::marker` unicode
      escapes occasionally choke parsing it),
    - `-qt-*` and overly-verbose tautological CSS properties from
      every inline `style=""` attribute.

    Leaves semantic markup (`<h1>`, `<p>`, `<ul>`, `<strong>`,
    `<img>`, etc.) and content-bearing styles (font-weight,
    font-style, font-size when set) untouched, so bold / italic /
    headings still render correctly after sanitization.
    """
    if not html:
        return html

    cleaned = _QRICHTEXT_META_RE.sub("", html)
    cleaned = _HEAD_STYLE_BLOCK_RE.sub("", cleaned)

    def _rewrite_style(match: re.Match[str]) -> str:
        payload = match.group(1) if match.group(1) is not None else match.group(2)
        new_payload = _clean_style_payload(payload)
        if not new_payload:
            return ""  # drop the whole attribute when nothing survives
        return f'style="{new_payload}"'

    cleaned = _STYLE_ATTR_RE.sub(_rewrite_style, cleaned)
    # Collapse the run of whitespace that the attribute-strip leaves
    # behind, e.g. `<p  >`. Cosmetic; trims a few bytes off the payload.
    cleaned = re.sub(r"\s+>", ">", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned


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

    Qt-dependent. Returns the rewritten + sanitized HTML; raises
    ImportError if PyQt6 is not available (the helper is only useful
    in the running Qt app).
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
    html = sanitize_qt_html(html)
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
