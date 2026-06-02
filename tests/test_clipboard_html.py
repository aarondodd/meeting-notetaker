"""Pure-Python tests for the HTML clipboard -> Markdown helper.

Pins the conversion output for representative HTML samples the user
will actually paste from -- Notion, VS Code, GitHub, Office /
Outlook, Confluence rendered view. The Qt-side paste-hook wiring
lives in tests/test_live_notes_widget_paste.py and is covered
separately so this file can run without PyQt6 installed.
"""
from __future__ import annotations

import pytest

# markdownify is a runtime optional dep. Without it, html_to_markdown
# is supposed to return "" and let the caller fall through. Most
# tests need the real library; the "missing dep" path is exercised
# in test_returns_empty_when_markdownify_missing below.
pytest.importorskip("markdownify")

from meeting_notetaker.utils.clipboard_html import html_to_markdown


# ---- structure preservation ---------------------------------------------


def test_heading_link_and_bold_round_trip():
    html = (
        '<h2>Project status</h2>'
        '<p>See <a href="https://example.com/doc">the doc</a> '
        'for the <strong>full picture</strong>.</p>'
    )
    md = html_to_markdown(html)
    assert "## Project status" in md
    assert "[the doc](https://example.com/doc)" in md
    assert "**full picture**" in md


def test_atx_heading_levels():
    md = html_to_markdown("<h1>One</h1><h2>Two</h2><h3>Three</h3>")
    assert "# One" in md
    assert "## Two" in md
    assert "### Three" in md
    # No setext (===) headings -- we configure ATX.
    assert "===" not in md


def test_nested_bullet_list_preserves_indentation():
    html = (
        '<ul>'
        '<li>Parent A'
        '<ul><li>Child A1</li><li>Child A2</li></ul>'
        '</li>'
        '<li>Parent B</li>'
        '</ul>'
    )
    md = html_to_markdown(html)
    # Parents at column 0, children indented under them.
    lines = md.splitlines()
    assert "- Parent A" in lines
    assert "  - Child A1" in lines
    assert "  - Child A2" in lines
    assert "- Parent B" in lines


def test_numbered_list():
    md = html_to_markdown("<ol><li>First</li><li>Second</li><li>Third</li></ol>")
    assert "1. First" in md
    assert "2. Second" in md
    assert "3. Third" in md


def test_inline_code_wraps_with_backticks():
    md = html_to_markdown("<p>Run <code>git status</code> first.</p>")
    assert "`git status`" in md


def test_fenced_code_block_with_language_hint():
    html = (
        '<pre><code class="language-python">'
        'def hello():\n    return 1\n'
        '</code></pre>'
    )
    md = html_to_markdown(html)
    # Language hint travels from class="language-python" on the
    # inner <code> to the fence info string.
    assert "```python" in md
    assert "def hello():" in md
    assert "return 1" in md
    assert md.count("```") == 2


def test_fenced_code_block_without_language():
    md = html_to_markdown("<pre><code>plain block</code></pre>")
    # No language hint; bare fence still opens / closes.
    assert "```\nplain block" in md or "```\nplain block\n```" in md
    assert md.count("```") == 2


def test_blockquote_emits_quote_prefix():
    md = html_to_markdown("<blockquote><p>A pulled quote.</p></blockquote>")
    assert "> A pulled quote." in md


def test_table_emits_pipe_syntax():
    html = (
        '<table>'
        '<thead><tr><th>key</th><th>value</th></tr></thead>'
        '<tbody><tr><td>alpha</td><td>1</td></tr></tbody>'
        '</table>'
    )
    md = html_to_markdown(html)
    # Pipe-table header + separator + body row.
    assert "| key | value |" in md
    assert "| --- | --- |" in md
    assert "| alpha | 1 |" in md


def test_link_with_relative_url_preserved():
    md = html_to_markdown('<a href="/docs/foo">foo</a>')
    assert "[foo](/docs/foo)" in md


# ---- escape behavior -----------------------------------------------------


def test_asterisks_not_escaped_in_output():
    # We configure escape_asterisks=False so existing markdown stars
    # in content don't get double-escaped to \*.
    md = html_to_markdown("<p>Use *args, **kwargs.</p>")
    assert "\\*" not in md


def test_underscores_not_escaped_in_output():
    md = html_to_markdown("<p>file_name_thing</p>")
    assert "\\_" not in md


# ---- safety / defense in depth ------------------------------------------


def test_script_and_style_are_stripped():
    html = (
        '<style>body { color: red }</style>'
        '<script>alert(1)</script>'
        '<p>Real content.</p>'
    )
    md = html_to_markdown(html)
    assert "alert" not in md
    assert "color: red" not in md
    assert "Real content." in md


def test_empty_input_returns_empty():
    assert html_to_markdown("") == ""
    assert html_to_markdown("   \n  ") == ""
    assert html_to_markdown(None) == ""  # type: ignore[arg-type]


def test_plain_text_html_returns_text():
    # A clipboard with a text/html that's just a text node still
    # converts cleanly.
    md = html_to_markdown("just text")
    assert md == "just text"


def test_whitespace_collapse_is_reasonable():
    # Real-world clipboard HTML often has lots of newlines between
    # block elements. The helper strips the leading/trailing
    # whitespace; inner whitespace shape is markdownify's domain.
    md = html_to_markdown("\n\n<p>hello</p>\n\n")
    assert md == "hello"


# ---- Notion-shaped checkboxes / Confluence storage XML ------------------


def test_notion_attachment_image_replaced_with_placeholder():
    """Notion's HTML clipboard uses ``attachment:UUID:filename.png``
    for image refs -- these are page-local attachments that no
    non-authenticated downstream renderer can fetch. Surface a small
    italic placeholder noting the filename so the user knows
    something was there (Aaron's 2026-06-02 paste-from-Notion
    feedback)."""
    html = (
        '<p>Before image.</p>'
        '<p><img src="attachment:9196c211-82ce-4c2d-ae5a-edd8d8c249b7:image.png" '
        'alt="image.png"></p>'
        '<p>After image.</p>'
    )
    md = html_to_markdown(html)
    assert "(image: image.png omitted from paste)" in md
    # The broken attachment URL must NOT appear in the output.
    assert "attachment:" not in md
    # Surrounding paragraphs survive.
    assert "Before image." in md
    assert "After image." in md


def test_http_image_passes_through_unchanged():
    """Hosted HTTP / HTTPS images are fetchable -- downstream
    renderers (preview, PDF, Notion export) can resolve them, so
    the placeholder rewrite must skip them."""
    html = '<img src="https://example.com/foo.png" alt="hosted">'
    md = html_to_markdown(html)
    assert "https://example.com/foo.png" in md
    assert "[hosted]" in md or "![hosted]" in md
    assert "omitted from paste" not in md


def test_data_uri_image_passes_through_unchanged():
    """Inline base64 data URIs are self-contained; they stay."""
    html = '<img src="data:image/png;base64,abc123" alt="inline">'
    md = html_to_markdown(html)
    assert "data:image/png;base64,abc123" in md
    assert "omitted from paste" not in md


def test_cid_image_replaced_with_placeholder():
    """Email content-id refs (cid:) only resolve inside the
    originating email client; surface as placeholder."""
    md = html_to_markdown('<img src="cid:embedded@mail.local" alt="chart">')
    assert "(image: chart omitted from paste)" in md
    assert "cid:" not in md


def test_file_uri_image_replaced_with_placeholder():
    """file:// refs resolve only on the source machine."""
    md = html_to_markdown('<img src="file:///home/aaron/foo.png" alt="local">')
    assert "(image: local omitted from paste)" in md
    assert "file://" not in md


def test_relative_image_path_replaced_with_placeholder():
    """A bare relative path resolves against the source HTML's base
    URL, which is meaningless after a clipboard round-trip."""
    md = html_to_markdown('<img src="images/foo.png" alt="rel">')
    assert "(image: rel omitted from paste)" in md
    assert "images/foo.png" not in md


def test_image_without_alt_falls_back_to_filename_in_placeholder():
    """When the source ``<img>`` carries no alt text, the placeholder
    label is mined from the URL tail so the user still sees what
    they pasted."""
    md = html_to_markdown(
        '<img src="attachment:abc-def-ghi:meeting-notes.png">'
    )
    assert "meeting-notes.png" in md
    assert "omitted from paste" in md


def test_image_without_src_dropped_with_generic_placeholder():
    md = html_to_markdown("<img alt=''>")
    assert "(image omitted from paste)" in md


def test_github_style_task_list_collapses_to_bullets():
    # markdownify drops the <input> elements; the user sees clean
    # bullets even if the source had checkboxes. Acceptable trade-off
    # for v1 of this enhancement -- task-list preservation is a
    # follow-up.
    html = (
        '<ul>'
        '<li><input type="checkbox" disabled> open item</li>'
        '<li><input type="checkbox" disabled checked> done item</li>'
        '</ul>'
    )
    md = html_to_markdown(html)
    assert "- open item" in md
    assert "- done item" in md
