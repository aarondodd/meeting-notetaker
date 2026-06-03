"""Markdown -> Confluence storage XML converter (#79).

L-2026-05-17-001: We do NOT post Markdown to Confluence with
``content_format=markdown``. The converter on Atlassian's side
corrupts code blocks (collapses newlines), nested lists (flattens
hierarchy), and adjacent blockquote lines (merges into one
paragraph). Render directly to storage XML and post with
``content_format=storage``.

Walks a mistune AST and emits Confluence storage XML. Pure Python;
unit-testable; no Qt.

Image handling mirrors the Notion converter: an optional
``image_resolver`` callback maps a Markdown image URL to a storage
XML fragment (``<ac:image>...</ac:image>``). The orchestrator
attaches local images to the destination page first, then plugs an
``ri:attachment`` resolver that emits
``<ac:image><ri:attachment ri:filename="..." /></ac:image>``.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

import mistune


def markdown_to_storage(
    markdown_text: str,
    *,
    image_resolver: Optional[Callable[[str, str], str]] = None,
) -> str:
    """Convert ``markdown_text`` to Confluence storage XML.

    ``image_resolver(url, alt) -> storage_xml_fragment`` is invoked
    for every Markdown image. When omitted, images become
    ``<ac:image><ri:url ri:value="..." /></ac:image>`` which works
    for hosted (HTTP) images but not local paths -- the orchestrator
    is responsible for uploading + supplying a resolver that emits
    ``<ri:attachment ri:filename="..." />`` for local files.
    """
    parser = mistune.create_markdown(
        renderer="ast",
        plugins=["strikethrough", "table", "task_lists", "url"],
    )
    ast = parser(markdown_text or "")
    converter = _Converter(image_resolver=image_resolver)
    return converter.render_blocks(ast)


class _Converter:
    def __init__(
        self,
        *,
        image_resolver: Optional[Callable[[str, str], str]],
    ) -> None:
        self._image_resolver = image_resolver

    # ---- block-level walk ------------------------------------------------

    def render_blocks(self, nodes: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for node in nodes or []:
            piece = self._render_block(node)
            if piece:
                parts.append(piece)
        # Storage XML doesn't require an outer wrapper; siblings stack.
        return "".join(parts)

    def _render_block(self, node: dict[str, Any]) -> str:
        t = node.get("type")
        if t in ("blank_line",):
            return ""
        if t == "heading":
            level = int((node.get("attrs") or {}).get("level", 1))
            tag = f"h{min(6, max(1, level))}"
            inner = self._render_inline(node.get("children", []))
            return f"<{tag}>{inner}</{tag}>"
        if t == "paragraph":
            children = node.get("children", []) or []
            # Image-only paragraph: emit as an image block, not <p>.
            if len(children) == 1 and children[0].get("type") == "image":
                return self._render_image(children[0])
            return f"<p>{self._render_inline(children)}</p>"
        if t == "thematic_break":
            return "<hr/>"
        if t == "block_code":
            info = (node.get("attrs") or {}).get("info") or ""
            language = (info.strip().split()[0] if info.strip() else "").lower()
            raw = node.get("raw") or ""
            return _build_code_macro(language, raw)
        if t == "block_quote":
            inner = "".join(
                self._render_block(c) for c in node.get("children", []) or []
            )
            return f"<blockquote>{inner}</blockquote>"
        if t == "list":
            return self._render_list(node)
        if t == "table":
            return self._render_table(node)
        if t == "image":
            return self._render_image(node)
        if t == "block_html":
            # Pass through raw HTML; Confluence accepts a constrained
            # subset inside storage format and silently strips the rest.
            return node.get("raw") or ""
        # Unknown -- preserve raw text as a paragraph rather than dropping.
        raw = node.get("raw") or ""
        if raw:
            return f"<p>{_escape_text(raw)}</p>"
        return ""

    # ---- lists -----------------------------------------------------------

    def _render_list(self, node: dict[str, Any]) -> str:
        attrs = node.get("attrs") or {}
        ordered = bool(attrs.get("ordered"))
        # Tasklist heuristic: any task_list_item children -> render as
        # Confluence task list macro instead of an ordered/unordered list.
        children = node.get("children", []) or []
        is_task = any(c.get("type") == "task_list_item" for c in children)
        if is_task:
            return self._render_task_list(children)
        tag = "ol" if ordered else "ul"
        items = "".join(self._render_list_item(c) for c in children)
        return f"<{tag}>{items}</{tag}>"

    def _render_list_item(self, item: dict[str, Any]) -> str:
        parts: list[str] = []
        for child in item.get("children", []) or []:
            ctype = child.get("type")
            if ctype == "block_text":
                parts.append(
                    f"<p>{self._render_inline(child.get('children', []))}</p>"
                )
            elif ctype == "paragraph":
                parts.append(
                    f"<p>{self._render_inline(child.get('children', []))}</p>"
                )
            elif ctype == "list":
                parts.append(self._render_list(child))
            else:
                parts.append(self._render_block(child))
        return f"<li>{''.join(parts)}</li>"

    def _render_task_list(self, items: list[dict[str, Any]]) -> str:
        # Confluence task list macro: tasks live inside <ac:task-list>.
        rows: list[str] = []
        for item in items:
            attrs = item.get("attrs") or {}
            checked = bool(attrs.get("checked"))
            status = "complete" if checked else "incomplete"
            body_parts: list[str] = []
            for child in item.get("children", []) or []:
                ctype = child.get("type")
                if ctype == "block_text":
                    body_parts.append(self._render_inline(child.get("children", [])))
                elif ctype == "paragraph":
                    body_parts.append(self._render_inline(child.get("children", [])))
            body = "".join(body_parts)
            rows.append(
                "<ac:task>"
                "<ac:task-id>0</ac:task-id>"
                f"<ac:task-status>{status}</ac:task-status>"
                f"<ac:task-body>{body}</ac:task-body>"
                "</ac:task>"
            )
        return "<ac:task-list>" + "".join(rows) + "</ac:task-list>"

    # ---- tables ----------------------------------------------------------

    def _render_table(self, node: dict[str, Any]) -> str:
        sections_html: list[str] = []
        head_html = ""
        body_rows: list[str] = []
        for section in node.get("children", []) or []:
            stype = section.get("type")
            if stype == "table_head":
                cells = section.get("children", []) or []
                rendered_cells = "".join(
                    f"<th>{self._render_inline(c.get('children', []))}</th>"
                    for c in cells
                )
                head_html = f"<thead><tr>{rendered_cells}</tr></thead>"
            elif stype == "table_body":
                for r in section.get("children", []) or []:
                    cells = r.get("children", []) or []
                    rendered_cells = "".join(
                        f"<td>{self._render_inline(c.get('children', []))}</td>"
                        for c in cells
                    )
                    body_rows.append(f"<tr>{rendered_cells}</tr>")
        body_html = ""
        if body_rows:
            body_html = f"<tbody>{''.join(body_rows)}</tbody>"
        sections_html.append(head_html)
        sections_html.append(body_html)
        return "<table>" + "".join(sections_html) + "</table>"

    # ---- images ----------------------------------------------------------

    def _render_image(self, node: dict[str, Any]) -> str:
        url = (node.get("attrs") or {}).get("url", "")
        alt = _inline_to_plain(node.get("children", []))
        if self._image_resolver is not None:
            inner = self._image_resolver(url, alt)
            if not inner:
                return ""
            return inner
        # Default: hosted external URL. Works for HTTP refs; will not
        # work for local paths -- the caller's resolver is expected to
        # handle those.
        alt_attr = (
            f' ac:alt="{_escape_attr(alt)}"' if alt else ""
        )
        return (
            f'<ac:image{alt_attr}>'
            f'<ri:url ri:value="{_escape_attr(url)}" />'
            f'</ac:image>'
        )

    # ---- inline walk -----------------------------------------------------

    def _render_inline(self, nodes: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for node in nodes or []:
            t = node.get("type")
            if t == "text":
                parts.append(_escape_text(node.get("raw") or ""))
            elif t == "softbreak":
                parts.append("\n")
            elif t == "linebreak":
                parts.append("<br/>")
            elif t == "strong":
                parts.append(
                    "<strong>"
                    + self._render_inline(node.get("children", []))
                    + "</strong>"
                )
            elif t == "emphasis":
                parts.append(
                    "<em>"
                    + self._render_inline(node.get("children", []))
                    + "</em>"
                )
            elif t in ("strikethrough", "del"):
                parts.append(
                    "<s>"
                    + self._render_inline(node.get("children", []))
                    + "</s>"
                )
            elif t == "codespan":
                parts.append(
                    "<code>" + _escape_text(node.get("raw") or "") + "</code>"
                )
            elif t == "link":
                url = (node.get("attrs") or {}).get("url", "")
                inner = self._render_inline(node.get("children", []))
                # Confluence storage supports both <a href> and
                # <ac:link><ri:url ri:value="..."> -- the simpler
                # <a href> works for external URLs and renders
                # identically in the page view.
                parts.append(
                    f'<a href="{_escape_attr(url)}">{inner}</a>'
                )
            elif t == "image":
                # Inline image inside other inline content -- surface
                # the alt as text. Block-level images (paragraph with
                # only an image child) are handled by _render_image.
                alt = _inline_to_plain(node.get("children", []))
                if alt:
                    parts.append(_escape_text(alt))
            else:
                # Unknown inline: best-effort plain text.
                raw = node.get("raw") or _inline_to_plain(node.get("children", []))
                if raw:
                    parts.append(_escape_text(raw))
        return "".join(parts)


# ---- helpers --------------------------------------------------------------


def _build_code_macro(language: str, body: str) -> str:
    """Confluence's storage-format code-block macro."""
    parts = ['<ac:structured-macro ac:name="code">']
    if language:
        parts.append(
            '<ac:parameter ac:name="language">'
            + _escape_text(language)
            + "</ac:parameter>"
        )
    parts.append(
        "<ac:plain-text-body><![CDATA["
        + (body or "")
        + "]]></ac:plain-text-body>"
    )
    parts.append("</ac:structured-macro>")
    return "".join(parts)


def _escape_text(s: str) -> str:
    """XML-escape plain text content. The set is small but mandatory
    for storage format to parse: & first (so we don't double-escape),
    then < > and surrogate-friendly characters are left alone."""
    if not s:
        return ""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _escape_attr(s: str) -> str:
    """Escape a value for use in an XML attribute (adds quote handling)."""
    return _escape_text(s).replace('"', "&quot;").replace("'", "&apos;")


def _inline_to_plain(nodes: list[dict[str, Any]]) -> str:
    out: list[str] = []
    for node in nodes or []:
        t = node.get("type")
        if t == "text":
            out.append(node.get("raw") or "")
        elif "children" in node:
            out.append(_inline_to_plain(node["children"]))
        elif "raw" in node:
            out.append(node["raw"])
    return "".join(out)
