"""Markdown -> Notion block JSON converter (#79).

Walks a mistune AST and emits a list of Notion API block objects
suitable for the ``children`` array on ``POST /v1/pages`` (or for
``PATCH /v1/blocks/<id>/children`` on overflow).

Why a hand-rolled visitor instead of an SDK: the Notion SDK (Python)
is a thin REST wrapper -- it doesn't help with the formatting layer
where the real complexity lives. We need precise control over rich
text annotations, list-nesting, code-language hints, and image
references; a visitor against mistune's AST is the cleanest way.

Image handling: the converter accepts an optional ``image_resolver``
callback that maps a Markdown image URL to a Notion ``image`` block
payload's inner object -- either
``{"type": "external", "external": {"url": ...}}`` for HTTP refs or
``{"type": "file_upload", "file_upload": {"id": ...}}`` once a local
file has been uploaded via NotionClient.upload_image. The orchestrator
(export thread) wires the resolver; the converter never makes
network calls itself.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

import mistune


# Notion supports headings 1-3 only; deeper Markdown headings collapse
# to heading_3 (with the body content carrying the level visually).
_HEADING_TYPE = {
    1: "heading_1",
    2: "heading_2",
    3: "heading_3",
    4: "heading_3",
    5: "heading_3",
    6: "heading_3",
}


def build_toc_block() -> dict[str, Any]:
    """Return Notion's native auto-TOC block (#94).

    Notion renders the ``table_of_contents`` block as a clickable
    nested list of the page's headings -- server-side, auto-updating,
    and styled to match Notion's native UI. This is strictly better
    than emitting our own markdown TOC bullet list, which Notion
    renders as non-clickable text.

    Notion's TOC block has no max_depth parameter -- it shows all
    heading levels found on the page. Configure ``toc_max_depth`` on
    the source-side numbering / outline transforms if you want to
    suppress deeper headings before they reach Notion.
    """
    return {
        "object": "block",
        "type": "table_of_contents",
        "table_of_contents": {},
    }


def markdown_to_blocks(
    markdown_text: str,
    *,
    image_resolver: Optional[Callable[[str, str], dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Convert ``markdown_text`` to a list of Notion block objects.

    ``image_resolver`` is called as ``image_resolver(url, alt)`` and
    must return a dict suitable as the ``image`` block's inner value
    (``{"type": "external", ...}`` or ``{"type": "file_upload", ...}``)
    plus an optional ``caption`` rich-text array. When omitted, all
    images are emitted as ``external`` with the raw URL.
    """
    parser = mistune.create_markdown(
        renderer="ast",
        plugins=["strikethrough", "table", "task_lists", "url"],
    )
    ast = parser(markdown_text or "")
    converter = _Converter(image_resolver=image_resolver)
    return converter.convert_blocks(ast)


class _Converter:
    def __init__(
        self,
        *,
        image_resolver: Optional[Callable[[str, str], dict[str, Any]]],
    ) -> None:
        self._image_resolver = image_resolver

    # ---- block-level walk ------------------------------------------------

    def convert_blocks(self, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for node in nodes or []:
            block = self._convert_block(node)
            if isinstance(block, list):
                out.extend(block)
            elif block is not None:
                out.append(block)
        return out

    def _convert_block(self, node: dict[str, Any]):
        t = node.get("type")
        if t == "blank_line":
            return None  # Notion handles inter-block spacing itself.
        if t == "heading":
            level = (node.get("attrs") or {}).get("level", 1)
            block_type = _HEADING_TYPE.get(int(level), "heading_3")
            return _wrap_block(block_type, {
                "rich_text": self._convert_inline(node.get("children", [])),
            })
        if t == "paragraph":
            children = node.get("children", []) or []
            # A paragraph whose only child is an image becomes an
            # image block in Notion (paragraphs can't carry image
            # children directly the same way Markdown does).
            if len(children) == 1 and children[0].get("type") == "image":
                return self._convert_image(children[0])
            return _wrap_block("paragraph", {
                "rich_text": self._convert_inline(children),
            })
        if t == "thematic_break":
            return _wrap_block("divider", {})
        if t == "block_code":
            info = (node.get("attrs") or {}).get("info") or ""
            language = _normalize_code_language(info)
            return _wrap_block("code", {
                "rich_text": [_text_token(node.get("raw") or "")],
                "language": language,
            })
        if t == "block_quote":
            # Notion's quote block carries a rich_text line + optional
            # children (other quoted blocks). We collapse multi-line
            # quotes into one quote block whose rich_text reads the
            # concatenated paragraphs; nested blocks attach as children.
            inner_blocks = self.convert_blocks(node.get("children", []))
            # First paragraph (if any) supplies the rich_text; the rest
            # become children of the quote.
            quote_rich_text: list[dict[str, Any]] = []
            child_blocks: list[dict[str, Any]] = []
            seen_first_paragraph = False
            for child in node.get("children", []) or []:
                if not seen_first_paragraph and child.get("type") == "paragraph":
                    quote_rich_text = self._convert_inline(child.get("children", []))
                    seen_first_paragraph = True
                else:
                    extra = self._convert_block(child)
                    if isinstance(extra, list):
                        child_blocks.extend(extra)
                    elif extra is not None:
                        child_blocks.append(extra)
            body = {"rich_text": quote_rich_text}
            if child_blocks:
                body["children"] = child_blocks
            del inner_blocks  # paths above already consumed children
            return _wrap_block("quote", body)
        if t == "list":
            return self._convert_list(node)
        if t == "table":
            return self._convert_table(node)
        if t == "image":
            return self._convert_image(node)
        if t == "block_html":
            # Raw HTML in markdown is rare in our notes; emit as a
            # paragraph with the literal text so it's preserved rather
            # than silently dropped.
            raw = node.get("raw") or ""
            return _wrap_block("paragraph", {
                "rich_text": [_text_token(raw)],
            })
        # Unknown block type: surface the raw text so nothing is silently lost.
        raw = node.get("raw") or ""
        if raw:
            return _wrap_block("paragraph", {
                "rich_text": [_text_token(raw)],
            })
        return None

    # ---- list handling ---------------------------------------------------

    def _convert_list(self, node: dict[str, Any]) -> list[dict[str, Any]]:
        ordered = bool((node.get("attrs") or {}).get("ordered"))
        out: list[dict[str, Any]] = []
        for item in node.get("children", []) or []:
            if item.get("type") == "task_list_item":
                attrs = item.get("attrs") or {}
                out.append(self._build_list_item(
                    item,
                    "to_do",
                    extra={"checked": bool(attrs.get("checked"))},
                ))
            else:
                block_type = (
                    "numbered_list_item" if ordered else "bulleted_list_item"
                )
                out.append(self._build_list_item(item, block_type))
        return out

    def _build_list_item(
        self,
        item_node: dict[str, Any],
        block_type: str,
        *,
        extra: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        rich_text: list[dict[str, Any]] = []
        children_blocks: list[dict[str, Any]] = []
        for child in item_node.get("children", []) or []:
            ctype = child.get("type")
            if ctype == "block_text":
                rich_text.extend(self._convert_inline(child.get("children", [])))
            elif ctype == "paragraph":
                # mistune emits paragraph children in loose lists.
                if rich_text:
                    # Subsequent paragraphs become child paragraph blocks
                    # so each item still reads as one logical list entry.
                    children_blocks.append(_wrap_block("paragraph", {
                        "rich_text": self._convert_inline(child.get("children", [])),
                    }))
                else:
                    rich_text = self._convert_inline(child.get("children", []))
            elif ctype == "list":
                children_blocks.extend(self._convert_list(child))
            else:
                nested = self._convert_block(child)
                if isinstance(nested, list):
                    children_blocks.extend(nested)
                elif nested is not None:
                    children_blocks.append(nested)
        body: dict[str, Any] = {"rich_text": rich_text}
        if extra:
            body.update(extra)
        if children_blocks:
            body["children"] = children_blocks
        return _wrap_block(block_type, body)

    # ---- table handling --------------------------------------------------

    def _convert_table(self, node: dict[str, Any]) -> dict[str, Any]:
        # Walk head + body, flattening to a uniform list of rows. The
        # first row is the header.
        rows: list[list[list[dict[str, Any]]]] = []
        for section in node.get("children", []) or []:
            if section.get("type") == "table_head":
                rows.append(self._table_row_from_cells(section.get("children", [])))
            elif section.get("type") == "table_body":
                for r in section.get("children", []) or []:
                    rows.append(self._table_row_from_cells(r.get("children", [])))
        if not rows:
            return _wrap_block("paragraph", {"rich_text": []})
        width = max(len(r) for r in rows) if rows else 0
        return _wrap_block("table", {
            "table_width": width,
            "has_column_header": True,
            "has_row_header": False,
            "children": [
                {
                    "object": "block",
                    "type": "table_row",
                    "table_row": {
                        "cells": [
                            (r[i] if i < len(r) else [])
                            for i in range(width)
                        ],
                    },
                }
                for r in rows
            ],
        })

    def _table_row_from_cells(
        self, cells: list[dict[str, Any]],
    ) -> list[list[dict[str, Any]]]:
        # mistune emits cells as table_cell with children = inline nodes.
        return [
            self._convert_inline(c.get("children", []))
            for c in cells
        ]

    # ---- images ----------------------------------------------------------

    def _convert_image(self, node: dict[str, Any]) -> dict[str, Any]:
        url = (node.get("attrs") or {}).get("url", "")
        alt = _inline_to_plain(node.get("children", []))
        if self._image_resolver is not None:
            inner = self._image_resolver(url, alt)
        else:
            inner = {"type": "external", "external": {"url": url}}
        # Notion expects the image block to embed the type-specific
        # payload directly under "image".
        if alt and "caption" not in inner:
            inner["caption"] = [_text_token(alt)]
        return _wrap_block("image", inner)

    # ---- inline walk -----------------------------------------------------

    def _convert_inline(
        self,
        nodes: list[dict[str, Any]],
        *,
        bold: bool = False,
        italic: bool = False,
        strike: bool = False,
        link_url: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for node in nodes or []:
            t = node.get("type")
            if t == "text":
                token = _text_token(
                    node.get("raw", ""),
                    bold=bold, italic=italic, strike=strike, link_url=link_url,
                )
                out.append(token)
            elif t in ("softbreak", "linebreak"):
                # Notion renders \n inside rich_text as a hard break.
                out.append(_text_token(
                    "\n", bold=bold, italic=italic, strike=strike, link_url=link_url,
                ))
            elif t == "strong":
                out.extend(self._convert_inline(
                    node.get("children", []),
                    bold=True, italic=italic, strike=strike, link_url=link_url,
                ))
            elif t == "emphasis":
                out.extend(self._convert_inline(
                    node.get("children", []),
                    bold=bold, italic=True, strike=strike, link_url=link_url,
                ))
            elif t in ("strikethrough", "del"):
                out.extend(self._convert_inline(
                    node.get("children", []),
                    bold=bold, italic=italic, strike=True, link_url=link_url,
                ))
            elif t == "codespan":
                out.append(_text_token(
                    node.get("raw", ""),
                    bold=bold, italic=italic, strike=strike, link_url=link_url,
                    code=True,
                ))
            elif t == "link":
                url = (node.get("attrs") or {}).get("url", "")
                out.extend(self._convert_inline(
                    node.get("children", []),
                    bold=bold, italic=italic, strike=strike, link_url=url,
                ))
            elif t == "image":
                # Inline images aren't directly representable as Notion
                # rich text; surface as the alt text so the content
                # survives, and a real image block will appear for
                # paragraph-level images.
                alt = _inline_to_plain(node.get("children", []))
                if alt:
                    out.append(_text_token(
                        alt,
                        bold=bold, italic=italic, strike=strike, link_url=link_url,
                    ))
            else:
                # Unknown inline: best-effort plain text.
                raw = node.get("raw") or _inline_to_plain(node.get("children", []))
                if raw:
                    out.append(_text_token(
                        raw,
                        bold=bold, italic=italic, strike=strike, link_url=link_url,
                    ))
        return _merge_adjacent_tokens(out)


def _wrap_block(block_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "object": "block",
        "type": block_type,
        block_type: payload,
    }


def _text_token(
    content: str,
    *,
    bold: bool = False,
    italic: bool = False,
    strike: bool = False,
    code: bool = False,
    link_url: Optional[str] = None,
) -> dict[str, Any]:
    text_obj: dict[str, Any] = {"content": content or ""}
    if link_url:
        text_obj["link"] = {"url": link_url}
    annotations: dict[str, Any] = {}
    if bold:
        annotations["bold"] = True
    if italic:
        annotations["italic"] = True
    if strike:
        annotations["strikethrough"] = True
    if code:
        annotations["code"] = True
    token: dict[str, Any] = {
        "type": "text",
        "text": text_obj,
        "plain_text": content or "",
    }
    if annotations:
        token["annotations"] = annotations
    if link_url:
        token["href"] = link_url
    return token


def _inline_to_plain(nodes: list[dict[str, Any]]) -> str:
    """Flatten an inline AST to plain text (used for image alts +
    fallback content extraction)."""
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


def _merge_adjacent_tokens(
    tokens: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse consecutive tokens with identical annotations + link.

    Many markdown ASTs emit "a", "b" as two text nodes; merging keeps
    the Notion rich_text array compact. Tokens with images, code, or
    differing annotations stay separate.
    """
    if not tokens:
        return tokens
    merged: list[dict[str, Any]] = []
    for t in tokens:
        if (
            merged
            and merged[-1].get("type") == "text"
            and t.get("type") == "text"
            and merged[-1].get("annotations") == t.get("annotations")
            and (merged[-1].get("text") or {}).get("link")
                == (t.get("text") or {}).get("link")
        ):
            prev = merged[-1]
            prev_content = (prev.get("text") or {}).get("content", "")
            new_content = prev_content + (t.get("text") or {}).get("content", "")
            prev["text"]["content"] = new_content
            prev["plain_text"] = prev.get("plain_text", "") + t.get("plain_text", "")
        else:
            merged.append(t)
    return merged


# Notion's code-block language enum is a closed set. Common Markdown
# language tokens map cleanly; unrecognized tokens default to
# "plain text" so the API call succeeds rather than 400ing.
_NOTION_CODE_LANGUAGES = {
    "abap", "arduino", "bash", "basic", "c", "clojure", "coffeescript",
    "c++", "c#", "css", "dart", "diff", "docker", "elixir", "elm",
    "erlang", "flow", "fortran", "f#", "gherkin", "glsl", "go",
    "graphql", "groovy", "haskell", "html", "java", "javascript",
    "json", "julia", "kotlin", "latex", "less", "lisp", "livescript",
    "lua", "makefile", "markdown", "markup", "matlab", "mermaid",
    "nix", "objective-c", "ocaml", "pascal", "perl", "php",
    "plain text", "powershell", "prolog", "protobuf", "python", "r",
    "reason", "ruby", "rust", "sass", "scala", "scheme", "scss",
    "shell", "sql", "swift", "typescript", "vb.net", "verilog",
    "vhdl", "visual basic", "webassembly", "xml", "yaml",
}


def _normalize_code_language(token: str) -> str:
    """Map a Markdown ```code-block language token to Notion's enum."""
    if not token:
        return "plain text"
    t = token.strip().lower()
    # Common aliases.
    alias = {
        "js": "javascript",
        "ts": "typescript",
        "py": "python",
        "rb": "ruby",
        "sh": "shell",
        "bash": "bash",
        "yml": "yaml",
        "cpp": "c++",
        "csharp": "c#",
        "fsharp": "f#",
        "kt": "kotlin",
        "rs": "rust",
    }
    t = alias.get(t, t)
    if t in _NOTION_CODE_LANGUAGES:
        return t
    return "plain text"
