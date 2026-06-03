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

# URL schemes whose images we KEEP in the converted Markdown -- a
# downstream renderer (in-app preview, PDF export, Notion / Confluence
# upload) can actually fetch or embed these.
_FETCHABLE_IMAGE_SCHEMES = ("http", "https", "data")


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


def _rewrite_unfetchable_images(soup) -> None:
    """Replace ``<img>`` tags whose src can't be fetched with a small
    italic placeholder noting the filename.

    Notion's HTML clipboard puts page-local refs like
    ``attachment:UUID:filename.png`` into ``<img src>``. The receiver
    can't fetch those (they're internal page-attachment refs, not URLs
    a non-authenticated client can resolve), so markdownify dutifully
    emits ``![filename.png](attachment:...)`` and the editor preview /
    PDF / Notion export all render a broken image.

    Replace those with ``<em>(image: filename.png omitted from paste)</em>``
    so the user knows something was there and can re-insert it via the
    Image toolbar action if they need it. Schemes that downstream
    renderers can actually resolve (http, https, data) pass through
    unchanged.

    ``cid:`` (email content-id), ``file://`` (local to the source
    machine), and any other custom scheme get the same placeholder
    treatment for the same reason: the destination renderer can't
    fetch them, so a placeholder is more honest than a broken icon.
    """
    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        if src:
            scheme = src.split(":", 1)[0].lower() if ":" in src else ""
            # Relative paths (no scheme) typically resolve against the
            # source HTML's base URL -- meaningless after a clipboard
            # round-trip, so treat them as unfetchable too.
            fetchable = (
                scheme in _FETCHABLE_IMAGE_SCHEMES
                if scheme
                else False
            )
        else:
            fetchable = False
        if fetchable:
            continue
        label = (img.get("alt") or "").strip()
        if not label and src:
            # Pull a filename hint out of the broken URL when alt is
            # blank -- Notion's "attachment:UUID:filename.png" puts
            # the user-visible filename at the tail.
            tail = src.rsplit(":", 1)[-1].rsplit("/", 1)[-1]
            if tail and tail != src:
                label = tail
        # Aaron asked for emoji bookends so the placeholder is
        # obvious at a glance and doesn't blend into surrounding
        # italicized prose (#74 polish, 2026-06-02). U+274C CROSS MARK
        # is one of the few cases where this project ships a Unicode
        # glyph in user-visible output -- explicit user request.
        msg = (
            f"❌ (image: {label} could not be pasted) ❌" if label
            else "❌ (image could not be pasted) ❌"
        )
        placeholder = soup.new_tag("em")
        placeholder.string = msg
        img.replace_with(placeholder)


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
        _rewrite_unfetchable_images(soup)
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
