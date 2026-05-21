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
    _content_type_for,
    _is_absolute_url,
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
