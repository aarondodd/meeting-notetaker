"""Clipboard helper: rewrite local <img> src attrs to data: URIs.

Tests stay pure-Python by feeding canned HTML through the rewriter
rather than going through QTextDocument.setMarkdown -- the Qt-aware
entry point (copy_markdown_with_images) only adds the markdown render
on top, which is tested by Qt smoke flows elsewhere.
"""
from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest

from meeting_notetaker.utils.clipboard import (
    rewrite_img_srcs_to_data_uris,
    sanitize_qt_html,
    _content_type_for,
    _is_absolute_url,
    _clean_style_payload,
)


# Verbatim QTextDocument.toHtml() output for a 'hello world' markdown
# doc with bold + italic + a heading + a list + a local image. Captured
# from a real Qt 6.5 run; used as the regression fixture for the
# sanitizer so we don't need PyQt6 in the test env. Updated whenever Qt
# meaningfully changes its output shape.
_QT_TOHTML_FIXTURE = (
    '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.0//EN" "http://www.w3.org/TR/REC-html40/strict.dtd">'
    '<html><head>'
    '<meta name="qrichtext" content="1" />'
    '<meta charset="utf-8" />'
    '<style type="text/css">\n'
    'p, li { white-space: pre-wrap; }\n'
    'hr { height: 1px; border-width: 0; }\n'
    'li.unchecked::marker { content: "\\2610"; }\n'
    'li.checked::marker { content: "\\2612"; }\n'
    '</style></head>'
    '<body style=" font-family:\'Sans Serif\'; font-size:9pt; font-weight:400; font-style:normal;">'
    '<h1 style=" margin-top:0px; margin-bottom:0px; margin-left:0px; margin-right:0px; -qt-block-indent:0; text-indent:0px;">'
    '<span style=" font-size:xx-large; font-weight:700;">Hello</span></h1>'
    '<ul style="margin-top: 0px; margin-bottom: 0px; margin-left: 0px; margin-right: 0px; -qt-list-indent: 1;">'
    '<li style=" margin-top:6px; margin-bottom:6px; -qt-block-indent:0;">one</li>'
    '<li style=" margin-top:6px; margin-bottom:6px; -qt-block-indent:0;">two</li></ul>'
    '<p style=" margin-top:6px; margin-bottom:6px;">'
    'Some <span style=" font-weight:700;">bold</span> and '
    '<span style=" font-style:italic;">italic</span>.</p>'
    '<p style=" margin-top:6px; margin-bottom:6px;">'
    '<img src="images/x.png" alt="cat" title="" /></p>'
    '</body></html>'
)


def _png_bytes() -> bytes:
    """Minimal valid PNG (1x1 transparent). Used as the on-disk payload."""
    return base64.b64decode(
        b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    )


def _write_png(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_png_bytes())
    return path


def test_local_png_inlined_as_data_uri(tmp_path: Path):
    _write_png(tmp_path / "images" / "foo.png")
    html = '<img src="images/foo.png" alt="x">'
    out = rewrite_img_srcs_to_data_uris(html, tmp_path)
    assert "data:image/png;base64," in out
    # The base64 payload must contain the encoded PNG bytes.
    encoded = base64.b64encode(_png_bytes()).decode()
    assert encoded in out


def test_absolute_url_is_left_alone(tmp_path: Path):
    html = '<img src="https://example.com/x.png">'
    assert rewrite_img_srcs_to_data_uris(html, tmp_path) == html


def test_data_uri_is_left_alone(tmp_path: Path):
    html = '<img src="data:image/png;base64,AAAA">'
    assert rewrite_img_srcs_to_data_uris(html, tmp_path) == html


def test_missing_local_image_passes_through(tmp_path: Path):
    """Fail-soft: if the referenced file doesn't exist, leave the src
    unchanged so the paste target renders a broken-image placeholder
    (no worse than current behavior, no exception escapes copy)."""
    html = '<img src="images/does-not-exist.png">'
    out = rewrite_img_srcs_to_data_uris(html, tmp_path)
    assert out == html


def test_no_images_html_returned_unchanged(tmp_path: Path):
    html = "<p>hello <strong>world</strong></p>"
    assert rewrite_img_srcs_to_data_uris(html, tmp_path) == html


def test_multiple_images_all_inlined(tmp_path: Path):
    _write_png(tmp_path / "images" / "a.png")
    _write_png(tmp_path / "images" / "b.png")
    html = (
        '<p><img src="images/a.png" alt="a"></p>'
        '<p><img src="images/b.png" alt="b"></p>'
    )
    out = rewrite_img_srcs_to_data_uris(html, tmp_path)
    # Two distinct data: URIs in the output, in the same order.
    matches = re.findall(r'src="(data:image/[^"]+)"', out)
    assert len(matches) == 2
    assert all(s.startswith("data:image/png;base64,") for s in matches)


def test_path_traversal_blocked(tmp_path: Path):
    """A ref like 'images/../../etc/passwd' must resolve outside base_dir
    and be left as-is (NOT followed). The check uses Path.resolve() +
    is_relative_to(); the file may or may not exist outside base_dir,
    but either way the rewriter must not embed it."""
    outside = tmp_path.parent / "secret.png"
    _write_png(outside)
    try:
        html = '<img src="images/../../secret.png">'
        out = rewrite_img_srcs_to_data_uris(html, tmp_path)
        # The rewrite refused -- original src remains verbatim.
        assert out == html
        assert "data:image" not in out
    finally:
        try:
            outside.unlink()
        except OSError:
            pass


def test_single_and_double_quote_attributes(tmp_path: Path):
    _write_png(tmp_path / "images" / "q.png")
    html_dq = '<img src="images/q.png">'
    html_sq = "<img src='images/q.png'>"
    out_dq = rewrite_img_srcs_to_data_uris(html_dq, tmp_path)
    out_sq = rewrite_img_srcs_to_data_uris(html_sq, tmp_path)
    assert "data:image/png;base64," in out_dq
    assert "data:image/png;base64," in out_sq


def test_content_type_lookup():
    assert _content_type_for(Path("x.png")) == "image/png"
    assert _content_type_for(Path("x.JPG")) == "image/jpeg"
    assert _content_type_for(Path("x.jpeg")) == "image/jpeg"
    assert _content_type_for(Path("x.gif")) == "image/gif"
    assert _content_type_for(Path("x.webp")) == "image/webp"
    # Unknown extension falls back to octet-stream so the rewrite
    # doesn't bail; the paste target just won't render it.
    assert _content_type_for(Path("x.tiff")) == "application/octet-stream"


def test_absolute_url_detection():
    assert _is_absolute_url("https://example.com/a")
    assert _is_absolute_url("http://example.com/a")
    assert _is_absolute_url("data:image/png;base64,AAA")
    assert _is_absolute_url("file:///tmp/x.png")
    assert _is_absolute_url("mailto:a@b")
    assert not _is_absolute_url("images/a.png")
    assert not _is_absolute_url("./a.png")
    assert not _is_absolute_url("a.png")


def test_empty_input_returns_empty(tmp_path: Path):
    assert rewrite_img_srcs_to_data_uris("", tmp_path) == ""


# ---- HTML sanitizer ---------------------------------------------------


def test_sanitizer_removes_qrichtext_meta():
    """The qrichtext meta is the Microsoft-side tell that flips Teams +
    OWA to text/plain. Must be gone after sanitization."""
    out = sanitize_qt_html(_QT_TOHTML_FIXTURE)
    assert "qrichtext" not in out


def test_sanitizer_removes_head_style_block():
    """The head <style> block ships Qt's `::marker` unicode escapes
    that some sanitizers can't parse. Drop the whole block."""
    out = sanitize_qt_html(_QT_TOHTML_FIXTURE)
    assert "::marker" not in out
    assert "white-space: pre-wrap" not in out
    # The body tag survives (it's not inside the head style block).
    assert "<body" in out


def test_sanitizer_strips_qt_specific_css_properties():
    out = sanitize_qt_html(_QT_TOHTML_FIXTURE)
    assert "-qt-block-indent" not in out
    assert "-qt-list-indent" not in out
    assert "-qt-paragraph-type" not in out


def test_sanitizer_preserves_semantic_tags():
    """h1, ul, li, p, img, span -- all must survive so formatting renders."""
    out = sanitize_qt_html(_QT_TOHTML_FIXTURE)
    assert "<h1" in out
    assert "<ul" in out
    assert "<li" in out
    assert "<p" in out
    assert "<img" in out
    assert "<span" in out


def test_sanitizer_preserves_bold_and_italic_styles():
    """Qt encodes bold + italic as `font-weight:700` / `font-style:italic`
    inline styles, not via <strong>/<em>. The sanitizer must NOT strip
    these properties or the paste loses all emphasis."""
    out = sanitize_qt_html(_QT_TOHTML_FIXTURE)
    assert "font-weight:700" in out or "font-weight: 700" in out
    assert "font-style:italic" in out or "font-style: italic" in out


def test_sanitizer_strips_tautological_margins():
    """Qt emits `margin-top:0px; margin-bottom:0px` on every block. They
    fight the destination app's layout for no semantic reason; drop them."""
    out = sanitize_qt_html(_QT_TOHTML_FIXTURE)
    assert "margin-top:0px" not in out
    assert "margin-top: 0px" not in out


def test_sanitizer_preserves_img_src():
    """Image refs in the body must survive intact -- they go through the
    data: URI rewriter on the next step."""
    out = sanitize_qt_html(_QT_TOHTML_FIXTURE)
    assert 'src="images/x.png"' in out


def test_sanitizer_significantly_reduces_size():
    """Sanity check that the strip actually does work -- Qt's verbose
    output is 1500+ chars; the sanitized version should land near 500."""
    out = sanitize_qt_html(_QT_TOHTML_FIXTURE)
    assert len(out) < len(_QT_TOHTML_FIXTURE) / 2


def test_sanitizer_handles_empty_string():
    assert sanitize_qt_html("") == ""


def test_sanitizer_handles_html_without_qt_markers():
    """A hand-written HTML payload should pass through with minimal change
    (just whitespace normalization). No qrichtext + no -qt-* + no head
    style block to strip."""
    html = '<p>hello <strong>world</strong></p>'
    out = sanitize_qt_html(html)
    assert "<strong>world</strong>" in out
    assert "hello" in out


def test_clean_style_payload_drops_qt_only_keeps_real_css():
    cleaned = _clean_style_payload(
        " font-weight:700; -qt-block-indent:0; margin-top:0px; font-style:italic"
    )
    assert "font-weight:700" in cleaned
    assert "font-style:italic" in cleaned
    assert "-qt-block-indent" not in cleaned
    assert "margin-top" not in cleaned


def test_clean_style_payload_empty_returns_empty():
    assert _clean_style_payload("") == ""
    assert _clean_style_payload("   ") == ""
    assert _clean_style_payload("-qt-block-indent:0; margin-top:0px") == ""


def test_sanitizer_drops_style_attr_when_only_qt_props_present():
    """If a `style=""` becomes empty after stripping Qt props, drop the
    attribute entirely rather than leaving `style=""` noise."""
    html = '<li style="-qt-block-indent:0; margin-top:6px;">x</li>'
    out = sanitize_qt_html(html)
    assert 'style=""' not in out
    assert "<li>x</li>" in out


def test_sanitizer_preserves_data_uri_unchanged():
    """data: URIs already in the input must survive sanitization
    untouched -- the rewriter runs after sanitize and depends on the
    src attribute being intact."""
    html = '<p><img src="data:image/png;base64,iVBOR=" alt="x" /></p>'
    out = sanitize_qt_html(html)
    assert 'src="data:image/png;base64,iVBOR="' in out


def test_rewrite_after_sanitize_still_inlines_images(tmp_path: Path):
    """End-to-end: sanitizer then image rewriter on the same fixture."""
    # Write the PNG the fixture references so the rewriter finds it.
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "x.png").write_bytes(
        base64.b64decode(
            b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
        )
    )
    sanitized = sanitize_qt_html(_QT_TOHTML_FIXTURE)
    final = rewrite_img_srcs_to_data_uris(sanitized, tmp_path)
    assert "data:image/png;base64," in final
    # qrichtext is gone, image is inline, semantic HTML preserved.
    assert "qrichtext" not in final
    assert "<h1" in final
    assert "<ul" in final
