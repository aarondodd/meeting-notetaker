"""Tests for the Word export module (#94)."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from meeting_notetaker.utils.word_export import (
    WordExportStats,
    _drop_toc_block,
    _flatten_inline_text,
    _resolve_local_image,
    export_to_docx,
    is_python_docx_available,
    is_word_com_available,
)


pytest_docx = pytest.importorskip("docx")


# ---- pure helpers --------------------------------------------------------


def test_flatten_inline_text_concatenates_text_runs():
    children = [
        {"type": "text", "raw": "Hello "},
        {"type": "emphasis", "children": [
            {"type": "text", "raw": "World"},
        ]},
    ]
    assert _flatten_inline_text(children) == "Hello World"


def test_flatten_inline_text_handles_softbreak_as_space():
    children = [
        {"type": "text", "raw": "A"},
        {"type": "softbreak"},
        {"type": "text", "raw": "B"},
    ]
    assert _flatten_inline_text(children) == "A B"


def test_drop_toc_block_removes_contents_heading_and_list():
    nodes = [
        {"type": "heading", "attrs": {"level": 2},
         "children": [{"type": "text", "raw": "Contents"}]},
        {"type": "list", "attrs": {"ordered": False}, "children": []},
        {"type": "thematic_break"},
        {"type": "heading", "attrs": {"level": 1},
         "children": [{"type": "text", "raw": "Real heading"}]},
    ]
    out = _drop_toc_block(nodes)
    assert len(out) == 1
    assert out[0]["type"] == "heading"
    assert _flatten_inline_text(out[0]["children"]) == "Real heading"


def test_drop_toc_block_keeps_non_contents_lists():
    nodes = [
        {"type": "heading", "attrs": {"level": 2},
         "children": [{"type": "text", "raw": "Action Items"}]},
        {"type": "list", "attrs": {"ordered": False}, "children": []},
    ]
    out = _drop_toc_block(nodes)
    assert len(out) == 2
    # Non-Contents heading + its list should be preserved.


def test_resolve_local_image_resolves_relative_path(tmp_path):
    img = tmp_path / "images" / "foo.png"
    img.parent.mkdir()
    img.write_bytes(b"fake")
    result = _resolve_local_image("images/foo.png", base_dir=tmp_path)
    assert result == img


def test_resolve_local_image_returns_none_for_remote_url(tmp_path):
    assert _resolve_local_image("https://example.com/foo.png", base_dir=tmp_path) is None


def test_resolve_local_image_returns_none_when_missing(tmp_path):
    assert _resolve_local_image("missing.png", base_dir=tmp_path) is None


# ---- platform probes -----------------------------------------------------


def test_is_python_docx_available_returns_true_in_test_env():
    assert is_python_docx_available() is True


def test_is_word_com_available_returns_false_on_non_windows():
    # Test environment is Linux; the probe should report False
    # without raising.
    import sys
    if sys.platform.startswith("win"):
        pytest.skip("Windows host -- behaviour depends on installed Word")
    assert is_word_com_available() is False


# ---- end-to-end docx generation ------------------------------------------


def test_export_to_docx_writes_a_valid_docx(tmp_path):
    src = (
        "# Title\n\n"
        "Some body text with **bold** and *italic*.\n\n"
        "## Subhead\n\n"
        "Paragraph two.\n"
    )
    dst = tmp_path / "out.docx"
    stats = export_to_docx(src, dst, include_toc=False)
    assert stats.error is None
    assert dst.exists()
    assert stats.headings_emitted == 2
    assert stats.paragraphs_emitted >= 2
    assert stats.toc_inserted is False
    # docx is a zip with word/document.xml inside.
    with zipfile.ZipFile(dst) as zf:
        names = zf.namelist()
        assert "word/document.xml" in names


def test_export_to_docx_with_toc_inserts_toc_field(tmp_path):
    src = (
        "# A\n\nbody\n\n## B\n\nbody\n\n### C\n\nbody\n"
    )
    dst = tmp_path / "out.docx"
    stats = export_to_docx(
        src, dst, include_toc=True, toc_max_depth=3,
    )
    assert stats.error is None
    assert stats.toc_inserted is True
    # Validate the TOC field code is present in document.xml.
    with zipfile.ZipFile(dst) as zf:
        xml = zf.read("word/document.xml").decode("utf-8")
    assert 'TOC \\o "1-3" \\h \\z \\u' in xml
    # The placeholder text Word shows until the TOC is updated.
    assert "Right-click here" in xml


def test_export_to_docx_skips_existing_contents_list(tmp_path):
    """When the markdown already carries a "## Contents" + bullet list
    AND the caller asks for a native TOC, drop the bullet list to
    avoid a duplicate."""
    src = (
        "## Contents\n\n"
        "- [A](#a)\n"
        "- [B](#b)\n\n"
        "---\n\n"
        "# A\n\nbody\n\n"
        "# B\n\nbody\n"
    )
    dst = tmp_path / "out.docx"
    stats = export_to_docx(src, dst, include_toc=True)
    assert stats.error is None
    # The "Contents" heading should not survive into the rendered
    # docx -- nor should the bullet list under it.
    with zipfile.ZipFile(dst) as zf:
        xml = zf.read("word/document.xml").decode("utf-8")
    assert ">Contents<" not in xml
    # Real headings still present.
    assert ">A<" in xml
    assert ">B<" in xml


def test_export_to_docx_embeds_local_image(tmp_path):
    img = tmp_path / "images" / "shot.png"
    img.parent.mkdir()
    # Generate a real 1x1 PNG via Pillow (already a transitive dep).
    from PIL import Image  # noqa: PLC0415
    Image.new("RGB", (16, 16), color=(180, 200, 220)).save(img, "PNG")
    src = "# Title\n\n![shot](images/shot.png)\n"
    dst = tmp_path / "out.docx"
    stats = export_to_docx(src, dst, base_dir=tmp_path)
    assert stats.error is None
    assert stats.images_embedded == 1
    assert stats.images_missing == 0
    # The docx should carry the image as a part inside /word/media/.
    with zipfile.ZipFile(dst) as zf:
        media = [n for n in zf.namelist() if n.startswith("word/media/")]
    assert len(media) == 1


def test_export_to_docx_marks_missing_image(tmp_path):
    src = "# Title\n\n![missing](images/nope.png)\n"
    dst = tmp_path / "out.docx"
    stats = export_to_docx(src, dst, base_dir=tmp_path)
    assert stats.error is None
    assert stats.images_embedded == 0
    assert stats.images_missing == 1


def test_export_to_docx_emits_links_as_word_hyperlinks(tmp_path):
    src = "# Title\n\nSee [Anthropic](https://anthropic.com).\n"
    dst = tmp_path / "out.docx"
    stats = export_to_docx(src, dst)
    assert stats.error is None
    with zipfile.ZipFile(dst) as zf:
        doc_xml = zf.read("word/document.xml").decode("utf-8")
        rels_xml = zf.read("word/_rels/document.xml.rels").decode("utf-8")
    assert "<w:hyperlink" in doc_xml
    assert "Anthropic" in doc_xml
    assert "https://anthropic.com" in rels_xml


def test_export_to_docx_emits_lists(tmp_path):
    src = (
        "# Title\n\n"
        "- First\n"
        "- Second\n"
        "  - Nested\n"
        "- Third\n"
    )
    dst = tmp_path / "out.docx"
    stats = export_to_docx(src, dst)
    assert stats.error is None
    with zipfile.ZipFile(dst) as zf:
        xml = zf.read("word/document.xml").decode("utf-8")
    # python-docx maps List Bullet to pStyle. The body should still
    # surface our list text.
    assert "First" in xml
    assert "Second" in xml
    assert "Nested" in xml
    assert "Third" in xml


def test_export_to_docx_emits_code_block(tmp_path):
    src = "# Title\n\n```\ndef foo():\n    pass\n```\n"
    dst = tmp_path / "out.docx"
    stats = export_to_docx(src, dst)
    assert stats.error is None
    with zipfile.ZipFile(dst) as zf:
        xml = zf.read("word/document.xml").decode("utf-8")
    assert "def foo" in xml


def test_export_to_docx_handles_empty_input(tmp_path):
    dst = tmp_path / "out.docx"
    stats = export_to_docx("", dst)
    assert stats.error is None
    assert dst.exists()
    assert stats.headings_emitted == 0
