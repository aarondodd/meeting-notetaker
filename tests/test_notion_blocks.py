"""Markdown -> Notion blocks converter tests (#79).

Pins the block shape produced for each Markdown construct so the
visitor can be refactored without silently changing what gets POSTed
to Notion.
"""
from __future__ import annotations

import pytest

pytest.importorskip("mistune")

from meeting_notetaker.integrations.notion_blocks import markdown_to_blocks


# ---- headings -------------------------------------------------------------


def test_heading_levels_map_to_notion_types():
    blocks = markdown_to_blocks("# h1\n\n## h2\n\n### h3\n\n#### h4")
    types = [b["type"] for b in blocks]
    assert types == ["heading_1", "heading_2", "heading_3", "heading_3"]
    assert blocks[0]["heading_1"]["rich_text"][0]["text"]["content"] == "h1"


# ---- paragraph + inline annotations ---------------------------------------


def test_paragraph_with_bold_italic_code_link():
    md = "Plain **bold** and *italic* and `code` and [a link](https://example.com)."
    blocks = markdown_to_blocks(md)
    assert len(blocks) == 1
    rt = blocks[0]["paragraph"]["rich_text"]
    # Find the link token.
    link_tokens = [t for t in rt if (t.get("text") or {}).get("link")]
    assert len(link_tokens) == 1
    assert link_tokens[0]["text"]["link"]["url"] == "https://example.com"
    assert link_tokens[0]["text"]["content"] == "a link"
    # Bold + italic + code annotations show up.
    bolds = [t for t in rt if (t.get("annotations") or {}).get("bold")]
    italics = [t for t in rt if (t.get("annotations") or {}).get("italic")]
    codes = [t for t in rt if (t.get("annotations") or {}).get("code")]
    assert any(t["text"]["content"] == "bold" for t in bolds)
    assert any(t["text"]["content"] == "italic" for t in italics)
    assert any(t["text"]["content"] == "code" for t in codes)


def test_adjacent_plain_text_tokens_merge():
    """The mistune AST emits text in shards (e.g. around inline code);
    merging keeps the rich_text array small."""
    md = "foo `code` bar baz"
    blocks = markdown_to_blocks(md)
    rt = blocks[0]["paragraph"]["rich_text"]
    # Expect: "foo ", code("code"), " bar baz" -- the trailing plain
    # text shouldn't split into "bar" + " " + "baz".
    plain_after_code = [t for t in rt if t["text"]["content"] == " bar baz"]
    assert plain_after_code, f"adjacent plain text not merged: {rt}"


# ---- lists ----------------------------------------------------------------


def test_bulleted_list_each_item_becomes_block():
    md = "- one\n- two\n- three"
    blocks = markdown_to_blocks(md)
    assert [b["type"] for b in blocks] == [
        "bulleted_list_item", "bulleted_list_item", "bulleted_list_item",
    ]
    assert blocks[0]["bulleted_list_item"]["rich_text"][0]["text"]["content"] == "one"


def test_numbered_list_becomes_numbered_list_items():
    md = "1. first\n2. second"
    blocks = markdown_to_blocks(md)
    assert [b["type"] for b in blocks] == [
        "numbered_list_item", "numbered_list_item",
    ]


def test_task_list_emits_to_do_blocks_with_checked_flag():
    md = "- [ ] open task\n- [x] done task"
    blocks = markdown_to_blocks(md)
    assert [b["type"] for b in blocks] == ["to_do", "to_do"]
    assert blocks[0]["to_do"]["checked"] is False
    assert blocks[1]["to_do"]["checked"] is True


def test_nested_list_nests_as_children():
    md = "- parent\n  - child\n  - sibling"
    blocks = markdown_to_blocks(md)
    assert blocks[0]["type"] == "bulleted_list_item"
    children = blocks[0]["bulleted_list_item"].get("children", [])
    assert [c["type"] for c in children] == [
        "bulleted_list_item", "bulleted_list_item",
    ]
    assert children[0]["bulleted_list_item"]["rich_text"][0]["text"]["content"] == "child"


# ---- code block -----------------------------------------------------------


def test_code_block_with_language_hint():
    md = "```python\ndef hello():\n    return 1\n```"
    blocks = markdown_to_blocks(md)
    assert blocks[0]["type"] == "code"
    assert blocks[0]["code"]["language"] == "python"
    assert "def hello" in blocks[0]["code"]["rich_text"][0]["text"]["content"]


def test_code_block_unknown_language_falls_back_to_plain_text():
    md = "```foobar\nx\n```"
    blocks = markdown_to_blocks(md)
    assert blocks[0]["code"]["language"] == "plain text"


def test_code_block_aliases_resolved():
    """Common aliases (js, ts, py) map to Notion's canonical names."""
    assert markdown_to_blocks("```js\nx\n```")[0]["code"]["language"] == "javascript"
    assert markdown_to_blocks("```py\nx\n```")[0]["code"]["language"] == "python"
    assert markdown_to_blocks("```yml\nx\n```")[0]["code"]["language"] == "yaml"


# ---- blockquote -----------------------------------------------------------


def test_blockquote_emits_quote_block():
    md = "> a quoted line"
    blocks = markdown_to_blocks(md)
    assert blocks[0]["type"] == "quote"
    assert blocks[0]["quote"]["rich_text"][0]["text"]["content"] == "a quoted line"


# ---- divider -------------------------------------------------------------


def test_horizontal_rule_emits_divider():
    md = "Before\n\n---\n\nAfter"
    blocks = markdown_to_blocks(md)
    assert any(b["type"] == "divider" for b in blocks)


# ---- table ---------------------------------------------------------------


def test_table_emits_table_block_with_header_row():
    md = (
        "| k | v |\n"
        "|---|---|\n"
        "| a | 1 |\n"
        "| b | 2 |\n"
    )
    blocks = markdown_to_blocks(md)
    assert blocks[0]["type"] == "table"
    table = blocks[0]["table"]
    assert table["table_width"] == 2
    assert table["has_column_header"] is True
    rows = table["children"]
    assert [r["type"] for r in rows] == ["table_row", "table_row", "table_row"]
    # Header row first.
    assert rows[0]["table_row"]["cells"][0][0]["text"]["content"] == "k"
    assert rows[1]["table_row"]["cells"][0][0]["text"]["content"] == "a"


# ---- images --------------------------------------------------------------


def test_image_paragraph_emits_external_image_block_when_no_resolver():
    md = "![alt text](https://example.com/foo.png)"
    blocks = markdown_to_blocks(md)
    assert blocks[0]["type"] == "image"
    img = blocks[0]["image"]
    assert img["type"] == "external"
    assert img["external"]["url"] == "https://example.com/foo.png"
    # Alt copied into the caption.
    assert img["caption"][0]["text"]["content"] == "alt text"


def test_image_resolver_called_with_url_and_alt():
    calls = []

    def resolver(url, alt):
        calls.append((url, alt))
        return {"type": "file_upload", "file_upload": {"id": "upl-1"}}

    md = "![sample](images/foo.png)"
    blocks = markdown_to_blocks(md, image_resolver=resolver)
    assert calls == [("images/foo.png", "sample")]
    assert blocks[0]["type"] == "image"
    assert blocks[0]["image"]["type"] == "file_upload"
    assert blocks[0]["image"]["file_upload"]["id"] == "upl-1"


def test_inline_image_in_paragraph_emits_alt_text():
    """An image mixed inside a paragraph (rare but legal) should not
    silently disappear; the alt text survives as plain text."""
    md = "Look at this: ![the chart](foo.png) -- great!"
    blocks = markdown_to_blocks(md)
    # Paragraph (not image block) because the image isn't alone.
    assert blocks[0]["type"] == "paragraph"
    contents = "".join(
        t["text"]["content"] for t in blocks[0]["paragraph"]["rich_text"]
    )
    assert "the chart" in contents
    assert "great" in contents


# ---- empty / edge cases ---------------------------------------------------


def test_empty_input_returns_empty_list():
    assert markdown_to_blocks("") == []
    assert markdown_to_blocks("   \n  \n") == []


def test_blank_lines_dont_emit_blocks():
    md = "Line 1.\n\n\n\nLine 2."
    blocks = markdown_to_blocks(md)
    assert all(b["type"] != "blank_line" for b in blocks)
    assert len(blocks) == 2


def test_block_is_well_formed_for_api():
    """Every emitted block has the API contract shape:
    ``{"object": "block", "type": "...", <type>: {...}}``."""
    md = "# H\n\nP\n\n- L\n\n```\nC\n```\n"
    for b in markdown_to_blocks(md):
        assert b["object"] == "block"
        assert b["type"]
        assert b["type"] in b, f"block missing type-keyed payload: {b}"
