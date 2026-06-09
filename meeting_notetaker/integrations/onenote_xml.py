"""Markdown -> OneNote page XML (issue #100).

OneNote pages are set via ``UpdatePageContent(xml, ...)``. The XML
lives under the 2013 schema (``http://schemas.microsoft.com/office/
onenote/2013/onenote``) and accepts a small set of structural
elements:

  * ``one:Title`` -- the page title; quoted CDATA inside one:T.
  * ``one:Outline`` -- a top-level content container; we use one
    per page.
  * ``one:OE`` -- "outline element": a paragraph-ish row. Carries
    ``quickStyleIndex`` for H1..H6 (values 1..6) plus a few other
    built-in styles.
  * ``one:OEChildren`` -- nested list / sub-outline structure.
  * ``one:T`` -- inline content. CDATA-wrapped because the body
    can contain HTML-ish formatting tags (b / i / span / a).
  * ``one:Image`` -- inline image. ``one:Data`` is base64 of the
    raw bytes; ``format`` tells OneNote how to render.
  * ``one:Table`` -- rows + cells; supports cell-level
    ``shadingColor`` which we use for code blocks (no native code
    primitive exists in the schema).
  * ``one:InsertedFile`` -- copy-on-update file embedding;
    ``pathSource`` is the absolute disk path OneNote copies from.

The renderer walks mistune's AST and emits these elements. Pure
Python; no PyQt or COM imports here. The COM side
(``onenote_com.py``) hands this module a section + page title +
markdown body and gets back a complete ``<one:Page>`` document
ready to pass to ``UpdatePageContent``.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse
from xml.sax.saxutils import escape as xml_escape

import mistune


ONENOTE_NS = "http://schemas.microsoft.com/office/onenote/2013/onenote"


_PARSER_OPTIONS = {
    "renderer": "ast",
    "plugins": ["strikethrough", "table", "task_lists", "url"],
}


# ---- public types --------------------------------------------------------


@dataclass
class OneNoteImageRef:
    """One image reference resolved to its on-disk bytes (or a remote URL)."""
    url: str
    alt: str = ""
    data: bytes = b""
    format: str = "png"  # "png" / "jpeg" / "gif" / "bmp"

    @property
    def base64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")


@dataclass
class PageXmlStats:
    """Lightweight counters for the worker's progress log."""
    headings: int = 0
    paragraphs: int = 0
    images_embedded: int = 0
    images_skipped_remote: int = 0
    tables: int = 0
    code_blocks: int = 0
    attached_files: int = 0


@dataclass
class OneNoteAttachedFile:
    """A session file to embed as ``one:InsertedFile``."""
    path: Path
    label: str = ""


# ---- public entrypoint ---------------------------------------------------


def build_page_xml(
    *,
    page_id: str,
    title: str,
    markdown_body: str,
    image_resolver: Optional[Callable[[str, str], Optional[OneNoteImageRef]]] = None,
    attached_files: Optional[list[OneNoteAttachedFile]] = None,
) -> tuple[str, PageXmlStats]:
    """Return ``(page_xml, stats)`` ready for UpdatePageContent.

    ``page_id`` is the COM object id from a prior ``CreateNewPage``
    call -- the XML's root carries it so OneNote knows which page
    to update. ``image_resolver`` is called for each markdown
    ``![alt](url)`` reference; remote URLs that the resolver can't
    fetch should return None and the resulting XML notes the
    omission visibly.
    """
    parser = mistune.create_markdown(**_PARSER_OPTIONS)
    ast = parser(markdown_body or "")
    stats = PageXmlStats()
    renderer = _OneNoteRenderer(
        image_resolver=image_resolver or (lambda _u, _a: None),
        stats=stats,
    )
    body_oes = renderer.render(ast)
    attached_xml = ""
    if attached_files:
        attached_xml = _build_inserted_files_section(
            attached_files, stats,
        )
    xml = _build_page_envelope(
        page_id=page_id,
        title=title,
        body_oes=body_oes,
        attached_xml=attached_xml,
    )
    return xml, stats


# ---- envelope ------------------------------------------------------------


def _build_page_envelope(
    *,
    page_id: str,
    title: str,
    body_oes: str,
    attached_xml: str,
) -> str:
    page_id_attr = f' ID="{xml_attr(page_id)}"' if page_id else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<one:Page xmlns:one="{ONENOTE_NS}"{page_id_attr}>'
          '<one:Title>'
            '<one:OE>'
              f'<one:T>{cdata(title)}</one:T>'
            '</one:OE>'
          '</one:Title>'
          '<one:Outline>'
            '<one:OEChildren>'
              f'{body_oes}'
            '</one:OEChildren>'
          '</one:Outline>'
          f'{attached_xml}'
        '</one:Page>'
    )


def _build_inserted_files_section(
    files: list[OneNoteAttachedFile],
    stats: PageXmlStats,
) -> str:
    """Append a separate one:Outline with a heading + one:InsertedFile
    per file. Kept in a sibling outline so the visual layout puts
    attachments below the main content."""
    rows = [
        f'<one:OE quickStyleIndex="2"><one:T>{cdata("Attachments")}</one:T></one:OE>',
    ]
    for f in files:
        if not f.path.is_file():
            continue
        rows.append(
            '<one:OE>'
              '<one:InsertedFile'
              f' pathSource="{xml_attr(str(f.path.resolve()))}"'
              f' preferredName="{xml_attr(f.label or f.path.name)}"'
              '/>'
            '</one:OE>'
        )
        stats.attached_files += 1
    if not rows[1:]:
        return ""
    return (
        '<one:Outline>'
          '<one:OEChildren>'
            + "".join(rows) +
          '</one:OEChildren>'
        '</one:Outline>'
    )


# ---- renderer ------------------------------------------------------------


# Pale-grey background for the cell that wraps a code block. OneNote's
# UI shows this as a subtle box around fixed-pitch text -- the closest
# native equivalent to a markdown code fence.
_CODE_CELL_SHADING = "#F2F2F2"


_HEADING_STYLES = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}


class _OneNoteRenderer:
    def __init__(
        self,
        *,
        image_resolver: Callable[[str, str], Optional[OneNoteImageRef]],
        stats: PageXmlStats,
    ) -> None:
        self._image_resolver = image_resolver
        self._stats = stats

    def render(self, nodes: list[dict]) -> str:
        return "".join(self._render_block(n) for n in nodes)

    # ---- block-level dispatch -------------------------------------------

    def _render_block(self, node: dict) -> str:
        ntype = node.get("type", "")
        if ntype == "heading":
            return self._render_heading(node)
        if ntype == "paragraph":
            return self._render_paragraph(node)
        if ntype in ("list",):
            return self._render_list(node)
        if ntype == "block_code":
            return self._render_code_block(node)
        if ntype == "block_quote":
            return self._render_blockquote(node)
        if ntype == "thematic_break":
            # OneNote has no horizontal rule; render as an empty
            # spacer line so the visual break isn't lost.
            return '<one:OE><one:T><![CDATA[<span>---</span>]]></one:T></one:OE>'
        if ntype == "block_html":
            return self._render_block_html(node)
        if ntype == "table":
            return self._render_table(node)
        if ntype == "blank_line":
            return ""
        # Unknown block: fall back to whatever inline rendering can
        # produce, wrapped in an OE. Better to surface something than
        # to silently drop content.
        text = self._render_inline_children(node.get("children") or [])
        if not text:
            return ""
        return f'<one:OE><one:T>{cdata(text)}</one:T></one:OE>'

    def _render_heading(self, node: dict) -> str:
        level = max(1, min(6, int(node.get("attrs", {}).get("level", 1))))
        text = self._render_inline_children(node.get("children") or [])
        self._stats.headings += 1
        return (
            f'<one:OE quickStyleIndex="{_HEADING_STYLES[level]}">'
              f'<one:T>{cdata(text)}</one:T>'
            '</one:OE>'
        )

    def _render_paragraph(self, node: dict) -> str:
        children = node.get("children") or []
        image_oes = self._extract_inline_images(children)
        # When every child is an image, the paragraph is image-only:
        # emit the image OEs and drop the text OE entirely so we don't
        # render a stray "[alt]" placeholder beside the image.
        non_image = [
            c for c in children if c.get("type") != "image"
        ]
        if image_oes and not non_image:
            return image_oes
        text = self._render_inline_children(children)
        out_parts: list[str] = []
        if text.strip():
            out_parts.append(
                f'<one:OE><one:T>{cdata(text)}</one:T></one:OE>'
            )
            self._stats.paragraphs += 1
        out_parts.append(image_oes)
        return "".join(out_parts)

    def _render_list(self, node: dict) -> str:
        ordered = bool(node.get("attrs", {}).get("ordered"))
        items_xml: list[str] = []
        for item in node.get("children") or []:
            if item.get("type") != "list_item":
                continue
            item_children = item.get("children") or []
            # First block in the item becomes the row text; nested
            # lists become one:OEChildren.
            row_text = ""
            nested_children: list[str] = []
            for child in item_children:
                ctype = child.get("type")
                if ctype == "block_text":
                    row_text = self._render_inline_children(
                        child.get("children") or [],
                    )
                elif ctype == "paragraph" and not row_text:
                    row_text = self._render_inline_children(
                        child.get("children") or [],
                    )
                elif ctype in ("list",):
                    nested_children.append(self._render_list(child))
                else:
                    nested_children.append(self._render_block(child))
            list_style = (
                ' listMuid="{D9F9C72E-DB73-4B69-B4D5-0A7FF1F4D8B5}"'
            )
            list_attrs = (
                f'<one:List><one:Number bulletCharacter="1." text="1." '
                f'fontColor="automatic"{list_style}/></one:List>'
                if ordered else
                f'<one:List><one:Bullet bullet="2"{list_style}/></one:List>'
            )
            inner = (
                f'<one:OE>{list_attrs}'
                f'<one:T>{cdata(row_text)}</one:T>'
            )
            if nested_children:
                inner += (
                    '<one:OEChildren>'
                    + "".join(nested_children)
                    + '</one:OEChildren>'
                )
            inner += '</one:OE>'
            items_xml.append(inner)
        return "".join(items_xml)

    def _render_code_block(self, node: dict) -> str:
        """OneNote has no fenced-code primitive. Render the block as a
        single-cell one:Table with a pale-grey ``shadingColor`` and
        Consolas inside; the closest visual match to a markdown
        fence."""
        raw = node.get("raw") or ""
        # Inside a one:T we have to switch newlines into <br/> so the
        # table cell preserves line breaks. CDATA does not turn raw
        # newlines into visible line breaks in OneNote.
        body = (
            '<span style=\'font-family:Consolas;font-size:10.0pt\'>'
            + xml_escape(raw, {'"': "&quot;"}).replace("\n", "<br/>")
            + '</span>'
        )
        self._stats.code_blocks += 1
        return (
            '<one:OE>'
              '<one:Table bordersVisible="false">'
                '<one:Columns><one:Column index="0" width="500"/></one:Columns>'
                '<one:Row>'
                  f'<one:Cell shadingColor="{_CODE_CELL_SHADING}">'
                    '<one:OEChildren>'
                      '<one:OE>'
                        f'<one:T><![CDATA[{body}]]></one:T>'
                      '</one:OE>'
                    '</one:OEChildren>'
                  '</one:Cell>'
                '</one:Row>'
              '</one:Table>'
            '</one:OE>'
        )

    def _render_blockquote(self, node: dict) -> str:
        # No native blockquote element. Indent the content as a sub-
        # outline; visually distinct from body text.
        children = node.get("children") or []
        rows = [self._render_block(c) for c in children]
        return (
            '<one:OE quickStyleIndex="7">'
              '<one:T>' + cdata('') + '</one:T>'
              '<one:OEChildren>' + "".join(rows) + '</one:OEChildren>'
            '</one:OE>'
        )

    def _render_block_html(self, node: dict) -> str:
        raw = node.get("raw") or ""
        if not raw.strip():
            return ""
        return f'<one:OE><one:T>{cdata(raw)}</one:T></one:OE>'

    def _render_table(self, node: dict) -> str:
        head_node = next(
            (c for c in node.get("children") or []
             if c.get("type") == "table_head"),
            None,
        )
        body_node = next(
            (c for c in node.get("children") or []
             if c.get("type") == "table_body"),
            None,
        )
        head_rows = [head_node] if head_node else []
        body_rows = list(body_node.get("children") or []) if body_node else []
        if not (head_rows or body_rows):
            return ""
        all_rows = head_rows + body_rows
        n_cols = max(
            len(r.get("children") or []) for r in all_rows
        ) if all_rows else 1
        col_xml = "".join(
            f'<one:Column index="{i}" width="120"/>' for i in range(n_cols)
        )
        rows_xml: list[str] = []
        for i, row in enumerate(all_rows):
            is_header = (i == 0 and head_rows)
            cells = []
            for cell in row.get("children") or []:
                text = self._render_inline_children(cell.get("children") or [])
                if is_header:
                    text = f'<span style="font-weight:bold">{text}</span>'
                cells.append(
                    '<one:Cell>'
                      '<one:OEChildren>'
                        '<one:OE>'
                          f'<one:T>{cdata(text)}</one:T>'
                        '</one:OE>'
                      '</one:OEChildren>'
                    '</one:Cell>'
                )
            rows_xml.append('<one:Row>' + "".join(cells) + '</one:Row>')
        self._stats.tables += 1
        return (
            '<one:OE>'
              '<one:Table hasHeaderRow="true">'
                f'<one:Columns>{col_xml}</one:Columns>'
                + "".join(rows_xml) +
              '</one:Table>'
            '</one:OE>'
        )

    # ---- inline rendering -----------------------------------------------

    def _render_inline_children(self, nodes: list[dict]) -> str:
        return "".join(self._render_inline(n) for n in nodes)

    def _render_inline(self, node: dict) -> str:
        ntype = node.get("type", "")
        if ntype == "text":
            return xml_escape(node.get("raw") or "")
        if ntype == "strong":
            inner = self._render_inline_children(node.get("children") or [])
            return f'<span style="font-weight:bold">{inner}</span>'
        if ntype == "emphasis":
            inner = self._render_inline_children(node.get("children") or [])
            return f'<span style="font-style:italic">{inner}</span>'
        if ntype == "strikethrough":
            inner = self._render_inline_children(node.get("children") or [])
            return (
                '<span style="text-decoration:line-through">'
                + inner + '</span>'
            )
        if ntype == "codespan":
            raw = xml_escape(node.get("raw") or "")
            return f'<span style="font-family:Consolas">{raw}</span>'
        if ntype == "link":
            url = (node.get("attrs", {}).get("url") or "").strip()
            inner = self._render_inline_children(node.get("children") or [])
            return f'<a href="{xml_attr(url)}">{inner}</a>'
        if ntype == "image":
            # Inline images are placed via _render_paragraph's
            # extract path; here we leave a small alt placeholder so
            # mixed-content paragraphs stay readable.
            alt = (
                "".join(
                    c.get("raw") or ""
                    for c in node.get("children") or []
                    if c.get("type") == "text"
                )
            )
            return f'<span>[{xml_escape(alt) or "image"}]</span>'
        if ntype == "linebreak":
            return "<br/>"
        if ntype == "softbreak":
            return " "
        # Unknown inline type: render children verbatim.
        return self._render_inline_children(node.get("children") or [])

    # ---- inline image extraction ---------------------------------------

    def _extract_inline_images(self, nodes: list[dict]) -> str:
        """Walk an inline child list + emit one OE per image, with the
        bytes resolved via the resolver. Returns "" if no images."""
        oes: list[str] = []
        for n in nodes:
            if n.get("type") != "image":
                continue
            url = (n.get("attrs", {}).get("url") or "").strip()
            alt = "".join(
                c.get("raw") or ""
                for c in n.get("children") or []
                if c.get("type") == "text"
            )
            oes.append(self._render_image(url, alt))
        return "".join(oes)

    def _render_image(self, url: str, alt: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme in ("http", "https"):
            self._stats.images_skipped_remote += 1
            return (
                '<one:OE><one:T>'
                + cdata(
                    f'<a href="{xml_attr(url)}">[remote image: {xml_escape(alt or url)}]</a>',
                )
                + '</one:T></one:OE>'
            )
        ref = self._image_resolver(url, alt)
        if ref is None:
            return (
                '<one:OE><one:T>'
                + cdata(f'(image missing: {xml_escape(alt or url)})')
                + '</one:T></one:OE>'
            )
        self._stats.images_embedded += 1
        return (
            '<one:OE>'
              f'<one:Image format="{xml_attr(ref.format)}">'
                f'<one:Data>{ref.base64}</one:Data>'
              '</one:Image>'
            '</one:OE>'
        )


# ---- helpers -------------------------------------------------------------


_TAG_RE = re.compile(r"<[^>]*>")


def _strip_html_tags(text: str) -> str:
    return _TAG_RE.sub("", text or "")


def cdata(text: str) -> str:
    """Wrap text in CDATA. OneNote XML uses CDATA inside one:T so
    inline HTML formatting tags (b / i / span / a) survive."""
    safe = (text or "").replace("]]>", "]]]]><![CDATA[>")
    return f"<![CDATA[{safe}]]>"


def xml_attr(value: str) -> str:
    return (value or "").replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
