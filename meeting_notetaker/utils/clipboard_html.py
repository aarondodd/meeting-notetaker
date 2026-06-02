"""HTML clipboard -> Markdown conversion for the notes editors.

When the user pastes formatted content (Notion, VS Code, GitHub, Office,
Confluence's rendered view, etc.) the QMimeData carries both a
``text/plain`` and a ``text/html`` representation. The source app's
``text/plain`` variant is often lossy: Notion strips link URLs, flattens
nested lists, and drops bold / italic / code-fence markers. Reading the
HTML variant and converting to Markdown preserves the structure as
Markdown source so it lives naturally inside the editor's buffer.

The helper is intentionally pure Python (no Qt imports) so it can be
unit-tested without a display. The Qt side lives in
``ui/live_notes_widget.py`` where ``_MarkdownEditor.insertFromMimeData``
calls into ``html_to_markdown`` when ``source.hasHtml()`` is true.
"""
from __future__ import annotations


_MARKDOWNIFY_OPTIONS = {
    # Match the existing toolbar's emit shape so paste output looks
    # the same as toolbar-produced markdown.
    "heading_style": "ATX",
    "bullets": "-",
    # Cleaner readback in markdown source; the editor stores raw
    # source so over-escaping is just visual noise.
    "escape_asterisks": False,
    "escape_underscores": False,
}

# Elements whose entire subtree (tag + contents) gets removed before
# markdownify runs. markdownify's own `strip=` option drops the wrapper
# but keeps the text content -- which would leak script/style bodies
# into the user's notes buffer. Pre-stripping via BeautifulSoup
# discards them entirely.
_DROP_ELEMENTS = ("script", "style", "noscript", "iframe", "object", "embed")


def _code_language_from_pre(pre_el) -> str:
    """Extract the ``language-foo`` hint from a ``<pre><code>`` block.

    Markdownify's ``code_language_callback`` receives the ``<pre>`` el;
    the language class lives on the inner ``<code>`` (Notion, GitHub,
    VS Code copy-as-html all use ``class="language-python"`` on the
    ``<code>``). Returns the bare language token or empty string.
    """
    code = pre_el.find("code")
    if code is None:
        return ""
    classes = code.get("class") or []
    for cls in classes:
        if cls.startswith("language-"):
            return cls[len("language-"):]
    return ""


def html_to_markdown(html: str) -> str:
    """Convert an HTML clipboard fragment to Markdown source.

    Returns an empty string for empty / whitespace-only / None-ish
    input so the caller can fall through to the plain-text paste path
    without a special case. Never raises -- a malformed HTML payload
    that BeautifulSoup can't parse degrades to plain-text extraction.
    """
    if not html or not html.strip():
        return ""
    try:
        from bs4 import BeautifulSoup  # local import: optional dep
        from markdownify import markdownify
    except ImportError:
        return ""
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag_name in _DROP_ELEMENTS:
            for el in soup.find_all(tag_name):
                el.decompose()
        out = markdownify(
            str(soup),
            code_language_callback=_code_language_from_pre,
            **_MARKDOWNIFY_OPTIONS,
        )
    except Exception:
        # BeautifulSoup parse failure or markdownify edge case; the
        # caller will see "" and fall through to plain-text paste.
        return ""
    return out.strip()
