"""Markdown -> HTML conversion for the PDF / print pipeline (#94).

Qt's ``QTextDocument.setMarkdown`` parses markdown internal links
(``[text](#slug)``) but its PDF writer does NOT emit the corresponding
named anchors as PDF named destinations. Result: a markdown-built PDF
shows the TOC entries as visible underlined links but clicking them
does nothing in the PDF viewer.

Qt's ``QTextDocument.setHtml`` DOES emit ``<a name="slug">`` anchors
as PDF named destinations, and ``<a href="#slug">`` links as
clickable PDF link annotations that resolve to those destinations.

So for the print path we convert the markdown body to HTML via
mistune (already a dep -- used by the Notion + Confluence
converters) with a custom heading renderer that injects an
``<a name="slug">`` marker before each heading. The TOC's
``[text](#slug)`` links carry through mistune as
``<a href="#slug">text</a>`` and resolve to the named anchors in
the rendered PDF.

Pure-Python; no Qt dependency. The Qt side is in
``ui/print_document.py`` + ``utils/export_package.py``.
"""
from __future__ import annotations

import re
from html import unescape
from typing import Optional

import mistune

from .markdown_outline import slugify


def markdown_to_print_html(
    markdown_text: str,
    *,
    anchor_headings: bool = True,
) -> str:
    """Convert ``markdown_text`` to HTML for Qt's setHtml print path.

    When ``anchor_headings`` is True (default), each heading gets a
    leading ``<a name="slug"></a>`` marker so the PDF writer can
    emit it as a named destination -- the TOC links then become
    clickable in the PDF viewer.

    Uses mistune's HTML renderer plus the same plugins the Notion
    + Confluence converters use (strikethrough, table, task_lists,
    url) so the print path supports the same markdown surface the
    user sees in the Preview.
    """
    renderer = _PrintHtmlRenderer(anchor_headings=anchor_headings)
    parser = mistune.create_markdown(
        renderer=renderer,
        plugins=["strikethrough", "table", "task_lists", "url"],
    )
    html = parser(markdown_text or "")
    return html


_TAG_STRIP_RE = re.compile(r"<[^>]*>")


def _heading_plain_text(rendered_inline: str) -> str:
    """Reduce mistune's rendered inline HTML for a heading back to
    plain text so we can compute the slug.

    mistune passes already-rendered child HTML to the heading
    renderer (e.g. ``"<strong>1</strong> Intro"``). The slug needs
    to operate on the visible text, so we strip the tags and
    unescape entities.
    """
    return unescape(_TAG_STRIP_RE.sub("", rendered_inline or "")).strip()


class _PrintHtmlRenderer(mistune.HTMLRenderer):
    """HTML renderer that injects named-anchor markers before each
    heading so the rendered PDF carries them as named destinations.

    Inherits the entire mistune default HTML output (tables,
    images, code blocks, etc.) -- we only override the heading
    method.
    """

    def __init__(self, *, anchor_headings: bool = True) -> None:
        super().__init__()
        self._anchor_headings = anchor_headings

    def heading(self, text: str, level: int, **attrs) -> str:
        plain = _heading_plain_text(text)
        slug = slugify(plain) if self._anchor_headings else ""
        level = max(1, min(6, int(level)))
        anchor = f'<a name="{slug}"></a>' if slug else ""
        # Render the anchor BEFORE the heading element. Qt's PDF
        # writer respects this shape; putting the anchor INSIDE the
        # heading (`<h1><a name=...></a>X</h1>`) works too but the
        # before-form is more compatible with older Qt versions.
        return f"{anchor}<h{level}>{text}</h{level}>\n"
