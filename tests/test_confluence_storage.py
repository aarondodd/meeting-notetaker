"""Markdown -> Confluence storage XML tests (#79).

Pins the storage-format output for each Markdown construct. The
inputs intentionally cover the three constructs L-2026-05-17-001
flagged as mangled by Confluence's own markdown converter -- code
blocks, nested lists, and adjacent blockquote lines -- so we know
our converter actually solves the problem the lesson described.
"""
from __future__ import annotations

import pytest

pytest.importorskip("mistune")

from meeting_notetaker.integrations.confluence_storage import markdown_to_storage


# ---- headings + inline ---------------------------------------------------


def test_headings_emit_h_tags():
    out = markdown_to_storage("# h1\n\n## h2\n\n### h3")
    assert "<h1>h1</h1>" in out
    assert "<h2>h2</h2>" in out
    assert "<h3>h3</h3>" in out


def test_paragraph_bold_italic_code_link():
    md = "Plain **bold** *italic* `code` and [a link](https://example.com)."
    out = markdown_to_storage(md)
    assert "<strong>bold</strong>" in out
    assert "<em>italic</em>" in out
    assert "<code>code</code>" in out
    assert '<a href="https://example.com">a link</a>' in out


def test_inline_text_xml_special_chars_escaped():
    """A markdown paragraph containing <, >, & must escape into the
    storage XML; otherwise Confluence treats the body as malformed."""
    out = markdown_to_storage("Use a < b && c > d as a rule.")
    assert "<p>" in out
    # No unescaped tag-like sequences in the body.
    assert "< b" not in out
    assert "&amp;&amp;" in out
    assert "&lt;" in out
    assert "&gt;" in out


# ---- code block (lesson regression) --------------------------------------


def test_code_block_uses_structured_macro_with_cdata():
    """L-2026-05-17-001: triple-backtick code blocks must land inside
    an <ac:structured-macro ac:name="code"> with CDATA-wrapped body
    so newlines + special characters survive."""
    md = '```python\ndef f():\n    return "<x>"\n```'
    out = markdown_to_storage(md)
    assert '<ac:structured-macro ac:name="code">' in out
    assert '<ac:parameter ac:name="language">python</ac:parameter>' in out
    # CDATA preserves the literal body, including the < that would
    # otherwise need to be escaped.
    assert "<![CDATA[" in out
    assert "def f():" in out
    assert "]]>" in out


def test_code_block_without_language_omits_parameter():
    out = markdown_to_storage("```\nplain block\n```")
    assert '<ac:structured-macro ac:name="code">' in out
    # No language parameter element when the fence carries no hint.
    assert "<ac:parameter" not in out
    assert "plain block" in out


# ---- nested lists (lesson regression) ------------------------------------


def test_nested_bulleted_list_preserves_hierarchy_natively():
    """L-2026-05-17-001 cited the markdown converter flattening
    nested lists. The storage path uses native <ul><li><p>...</p>
    <ul>...</ul></li></ul> so structure survives."""
    md = "- parent\n  - child\n  - sibling\n- second parent"
    out = markdown_to_storage(md)
    # Outer ul + inner ul both present.
    assert out.count("<ul>") >= 2
    # Inner ul nested inside the first li, with each child rendering
    # as its own <li><p>...</p></li>. Both child + sibling appear,
    # second parent too -- and the nesting is structural, not flat.
    assert "<li><p>child</p></li>" in out
    assert "<li><p>sibling</p></li>" in out
    assert "<li><p>second parent</p></li>" in out
    # The parent's <li> opens BEFORE the inner <ul>, proving nesting.
    parent_idx = out.index("<li><p>parent</p>")
    inner_ul_idx = out.index("<ul>", parent_idx + 1)
    assert parent_idx < inner_ul_idx


def test_ordered_list_uses_ol_tag():
    out = markdown_to_storage("1. one\n2. two\n3. three")
    assert "<ol>" in out
    assert "</ol>" in out
    # Each item wrapped in li.
    assert out.count("<li>") == 3


# ---- task list -----------------------------------------------------------


def test_task_list_uses_ac_task_list_macro():
    md = "- [ ] open\n- [x] done"
    out = markdown_to_storage(md)
    assert "<ac:task-list>" in out
    assert "<ac:task-status>incomplete</ac:task-status>" in out
    assert "<ac:task-status>complete</ac:task-status>" in out
    assert "<ac:task-body>open</ac:task-body>" in out
    assert "<ac:task-body>done</ac:task-body>" in out


# ---- blockquote ----------------------------------------------------------


def test_blockquote_emits_blockquote_tag():
    out = markdown_to_storage("> a quote")
    assert "<blockquote>" in out
    assert "</blockquote>" in out


def test_multiple_blockquote_lines_share_one_blockquote():
    """L-2026-05-17-001 second case: the markdown converter merged
    adjacent blockquote lines into one paragraph with no separators.
    Our path emits the markdown converter's natural structure --
    one <blockquote> per markdown block -- and each blockquote
    contains a <p> with the joined inline content. That's the
    correct shape; the lesson's failure case was specifically about
    the rendered TEXT collapsing, which CommonMark + storage XML
    handle correctly when we set the structure ourselves."""
    md = "> line a\n> line b\n\n> next quote"
    out = markdown_to_storage(md)
    # Two distinct blockquotes, not one merged blob.
    assert out.count("<blockquote>") == 2
    assert "<blockquote><p>line a\nline b</p></blockquote>" in out


# ---- divider + table -----------------------------------------------------


def test_horizontal_rule_emits_hr():
    out = markdown_to_storage("before\n\n---\n\nafter")
    assert "<hr/>" in out


def test_table_renders_native_thead_and_tbody():
    md = "| k | v |\n|---|---|\n| a | 1 |\n| b | 2 |\n"
    out = markdown_to_storage(md)
    assert "<table>" in out
    assert "<thead>" in out
    assert "<th>k</th><th>v</th>" in out
    assert "<tbody>" in out
    assert "<td>a</td><td>1</td>" in out
    assert "<td>b</td><td>2</td>" in out


# ---- images --------------------------------------------------------------


def test_image_default_renders_ri_url_for_http():
    out = markdown_to_storage("![alt](https://example.com/foo.png)")
    assert '<ac:image' in out
    assert 'ri:value="https://example.com/foo.png"' in out
    assert 'ac:alt="alt"' in out


def test_image_resolver_overrides_default_rendering():
    calls = []

    def resolver(url, alt):
        calls.append((url, alt))
        return f'<ac:image><ri:attachment ri:filename="{url.split("/")[-1]}" /></ac:image>'

    out = markdown_to_storage(
        "![local](images/foo.png)", image_resolver=resolver,
    )
    assert calls == [("images/foo.png", "local")]
    assert '<ri:attachment ri:filename="foo.png" />' in out


# ---- edge cases ----------------------------------------------------------


def test_empty_input_returns_empty_string():
    assert markdown_to_storage("") == ""
    assert markdown_to_storage("   \n   ") == ""


def test_xml_attribute_escaping_in_links():
    """Ampersand inside a URL must escape to &amp; in the href
    attribute. Quotes are URL-percent-encoded by mistune upstream
    (to %22) so the raw " never reaches the attribute escaper, which
    is fine -- the resulting attribute is still well-formed XML."""
    md = '[click](https://example.com/?q=a&b=z)'
    out = markdown_to_storage(md)
    assert "&amp;" in out
    # No unescaped & in the attribute body.
    assert '?q=a&b=z' not in out


def test_inline_code_with_lt_gt_escaped():
    out = markdown_to_storage("Inline `<tag>` here.")
    assert "<code>&lt;tag&gt;</code>" in out
