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


# ---- table rendering -----------------------------------------------------


def test_export_to_docx_renders_table(tmp_path):
    """Markdown tables (the appendix Attendees / Topics / Links
    sections rely on these) must render as Word tables, not vanish.
    """
    src = (
        "# Title\n\n"
        "| Name | Email |\n"
        "|------|-------|\n"
        "| Alice | a@b.com |\n"
        "| Bob | b@c.com |\n"
    )
    dst = tmp_path / "out.docx"
    stats = export_to_docx(src, dst)
    assert stats.error is None
    with zipfile.ZipFile(dst) as zf:
        xml = zf.read("word/document.xml").decode("utf-8")
    # The docx wraps a table in a <w:tbl> element.
    assert "<w:tbl" in xml
    # Header + body cells are visible.
    for cell in ("Name", "Email", "Alice", "a@b.com", "Bob", "b@c.com"):
        assert cell in xml, f"missing cell content {cell!r} in docx"


def test_export_to_docx_table_with_inline_formatting(tmp_path):
    """Bold / italic / code spans inside table cells survive into
    the docx as runs with the matching formatting -- the appendix
    rendering relies on this for emphasized cells."""
    src = (
        "| Field | Value |\n"
        "|-------|-------|\n"
        "| Status | **Active** |\n"
        "| Code | `tracking_id` |\n"
    )
    dst = tmp_path / "out.docx"
    stats = export_to_docx(src, dst)
    assert stats.error is None
    with zipfile.ZipFile(dst) as zf:
        xml = zf.read("word/document.xml").decode("utf-8")
    assert "<w:tbl" in xml
    assert "Active" in xml
    assert "tracking_id" in xml


# ---- title-block placement -----------------------------------------------


def test_export_to_docx_word_title_appears_before_toc(tmp_path):
    """When ``title`` is supplied, the doc opens with a Title-styled
    paragraph, then Word's native TOC field beneath it. The body's
    leading H1 (if any) is unchanged and renders below the TOC."""
    src = "# Section A\n\nbody.\n\n# Section B\n\nmore body.\n"
    dst = tmp_path / "out.docx"
    stats = export_to_docx(
        src, dst,
        title="Daily standup -- Synthesis -- 2026-06-08",
        include_toc=True, toc_max_depth=3,
    )
    assert stats.error is None
    assert stats.toc_inserted is True
    assert stats.title_emitted is True
    with zipfile.ZipFile(dst) as zf:
        xml = zf.read("word/document.xml").decode("utf-8")
    title_pos = xml.find("Daily standup")
    toc_pos = xml.find('TOC \\o "1-3"')
    section_pos = xml.find("Section A")
    assert title_pos > 0, "Word Title not rendered"
    assert toc_pos > 0, "TOC field not rendered"
    assert section_pos > 0, "body heading not rendered"
    assert title_pos < toc_pos < section_pos, (
        f"expected order title < TOC < body; got "
        f"title={title_pos} toc={toc_pos} section={section_pos}"
    )
    # Title style is "Title" -- verify via pStyle.
    assert 'w:val="Title"' in xml


def test_export_to_docx_no_title_means_no_title_paragraph(tmp_path):
    """When ``title`` is empty, don't emit a Title paragraph -- the
    doc starts with the TOC (if requested) and then the body."""
    src = "Just a paragraph.\n\n# Heading\n\nbody.\n"
    dst = tmp_path / "out.docx"
    stats = export_to_docx(src, dst, title="", include_toc=True)
    assert stats.error is None
    assert stats.title_emitted is False
    with zipfile.ZipFile(dst) as zf:
        xml = zf.read("word/document.xml").decode("utf-8")
    toc_pos = xml.find('TOC \\o ')
    para_pos = xml.find("Just a paragraph")
    assert toc_pos > 0
    assert para_pos > 0
    assert toc_pos < para_pos


def test_default_export_document_title_matches_filename_components():
    """The Word document Title uses the same building blocks as
    default_export_filename so the file and the doc identify
    themselves the same way."""
    from datetime import datetime
    from meeting_notetaker.utils.export import (
        default_export_document_title,
        default_export_filename,
    )
    now = datetime(2026, 6, 8, 12, 0, 0)
    title = default_export_document_title(
        "Daily standup", "Synthesis", now=now,
    )
    fname = default_export_filename(
        "Daily standup", "Synthesis", ".docx", now=now,
    )
    assert title == "Daily standup -- Synthesis -- 2026-06-08"
    # The filename sanitizes; the document title does not, but the
    # human-readable shape lines up for ASCII inputs.
    assert fname.startswith("Daily standup -- Synthesis -- 2026-06-08")
