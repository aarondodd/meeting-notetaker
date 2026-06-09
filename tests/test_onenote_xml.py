"""Tests for the markdown -> OneNote XML renderer (#100).

Pure-Python; mistune-based. Verifies the structural shape of the
page XML, not byte-for-byte equality (OneNote tolerates whitespace
+ attribute reordering, and pinning literal strings makes the
suite fragile against renderer tweaks).
"""
from __future__ import annotations

import base64
import re
import xml.etree.ElementTree as ET

from meeting_notetaker.integrations.onenote_xml import (
    ONENOTE_NS,
    OneNoteAttachedFile,
    OneNoteImageRef,
    build_page_xml,
    cdata,
    xml_attr,
)


_NS = {"one": ONENOTE_NS}


def _parse(xml: str) -> ET.Element:
    return ET.fromstring(xml)


# ---- envelope -----------------------------------------------------------


def test_page_envelope_carries_id_and_title():
    xml, _ = build_page_xml(
        page_id="{ABC-123}", title="Meeting notes",
        markdown_body="",
    )
    root = _parse(xml)
    assert root.tag == f"{{{ONENOTE_NS}}}Page"
    assert root.get("ID") == "{ABC-123}"
    title_t = root.find(".//one:Title/one:OE/one:T", _NS)
    assert title_t is not None
    assert title_t.text == "Meeting notes"


def test_page_xml_has_xml_declaration():
    xml, _ = build_page_xml(
        page_id="{PG-1}", title="t", markdown_body="",
    )
    assert xml.startswith('<?xml version="1.0"')


def test_page_envelope_with_empty_page_id_omits_id_attr():
    xml, _ = build_page_xml(page_id="", title="t", markdown_body="")
    root = _parse(xml)
    assert root.get("ID") is None


# ---- headings -----------------------------------------------------------


def test_h1_through_h6_get_quickstyle_index_1_through_6():
    src = "# A\n\n## B\n\n### C\n\n#### D\n\n##### E\n\n###### F\n"
    xml, stats = build_page_xml(
        page_id="x", title="t", markdown_body=src,
    )
    root = _parse(xml)
    oes = root.findall(".//one:Outline/one:OEChildren/one:OE", _NS)
    styles = [oe.get("quickStyleIndex") for oe in oes if oe.get("quickStyleIndex")]
    assert styles == ["1", "2", "3", "4", "5", "6"]
    assert stats.headings == 6


# ---- paragraphs + inline formatting -------------------------------------


def test_paragraph_emits_one_oe_with_cdata_text():
    src = "plain paragraph text\n"
    xml, stats = build_page_xml(
        page_id="x", title="t", markdown_body=src,
    )
    assert stats.paragraphs == 1
    assert "plain paragraph text" in xml


def test_bold_renders_as_inline_span_inside_cdata():
    src = "this **is** bold\n"
    xml, _ = build_page_xml(page_id="x", title="t", markdown_body=src)
    assert 'font-weight:bold' in xml
    assert "is" in xml


def test_italic_renders_as_inline_span():
    src = "this *is* italic\n"
    xml, _ = build_page_xml(page_id="x", title="t", markdown_body=src)
    assert 'font-style:italic' in xml


def test_strikethrough_renders_as_inline_span():
    src = "~~gone~~\n"
    xml, _ = build_page_xml(page_id="x", title="t", markdown_body=src)
    assert 'text-decoration:line-through' in xml


def test_codespan_renders_as_monospace_span():
    src = "use `os.path.join`\n"
    xml, _ = build_page_xml(page_id="x", title="t", markdown_body=src)
    assert "font-family:Consolas" in xml
    assert "os.path.join" in xml


def test_link_renders_as_anchor():
    src = "see [docs](https://example.com)\n"
    xml, _ = build_page_xml(page_id="x", title="t", markdown_body=src)
    assert 'href="https://example.com"' in xml
    assert "docs" in xml


# ---- lists --------------------------------------------------------------


def test_unordered_list_emits_bullet_oes():
    src = "- a\n- b\n- c\n"
    xml, _ = build_page_xml(page_id="x", title="t", markdown_body=src)
    bullets = re.findall(r'<one:Bullet[^/]*/>', xml)
    assert len(bullets) == 3
    for letter in ("a", "b", "c"):
        assert letter in xml


def test_nested_list_emits_oechildren():
    src = "- parent\n  - child\n  - child2\n- sibling\n"
    xml, _ = build_page_xml(page_id="x", title="t", markdown_body=src)
    # There's at least one OEChildren nested under an OE that already
    # has a List element.
    assert xml.count("<one:OEChildren>") >= 2


def test_ordered_list_uses_number_marker():
    src = "1. one\n2. two\n"
    xml, _ = build_page_xml(page_id="x", title="t", markdown_body=src)
    numbers = re.findall(r'<one:Number[^/]*/>', xml)
    assert len(numbers) == 2


# ---- code blocks --------------------------------------------------------


def test_code_block_renders_as_single_cell_table_with_shading():
    src = "```python\ndef hello():\n    pass\n```\n"
    xml, stats = build_page_xml(page_id="x", title="t", markdown_body=src)
    assert stats.code_blocks == 1
    assert "shadingColor=\"#F2F2F2\"" in xml
    assert "font-family:Consolas" in xml
    # Newlines preserved as <br/> inside the cell.
    assert "<br/>" in xml


def test_code_block_escapes_special_chars():
    src = "```\nif x < 5 && y > 0:\n    pass\n```\n"
    xml, _ = build_page_xml(page_id="x", title="t", markdown_body=src)
    # & < > all escaped inside the CDATA.
    assert "&amp;" in xml
    assert "&lt;" in xml
    assert "&gt;" in xml


# ---- tables -------------------------------------------------------------


def test_table_renders_with_header_row_bolded():
    src = "| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n"
    xml, stats = build_page_xml(page_id="x", title="t", markdown_body=src)
    assert stats.tables == 1
    assert 'hasHeaderRow="true"' in xml
    # Header text bolded; body text not.
    assert 'font-weight:bold' in xml
    rows = xml.count("<one:Row>")
    assert rows == 3


def test_table_column_count_matches_widest_row():
    src = "| A | B | C |\n|---|---|---|\n| 1 | 2 | 3 |\n"
    xml, _ = build_page_xml(page_id="x", title="t", markdown_body=src)
    columns = re.findall(r'<one:Column index="(\d+)"', xml)
    assert columns == ["0", "1", "2"]


# ---- images -------------------------------------------------------------


def test_local_image_uses_resolver_and_embeds_base64():
    src = "![alt text](images/foo.png)\n"

    def resolver(url, alt):
        return OneNoteImageRef(
            url=url, alt=alt, data=b"fakepngbytes", format="png",
        )

    xml, stats = build_page_xml(
        page_id="x", title="t", markdown_body=src,
        image_resolver=resolver,
    )
    assert stats.images_embedded == 1
    assert 'format="png"' in xml
    expected_b64 = base64.b64encode(b"fakepngbytes").decode("ascii")
    assert expected_b64 in xml


def test_remote_image_left_as_hyperlink_not_embedded():
    src = "![alt](https://example.com/x.png)\n"
    xml, stats = build_page_xml(page_id="x", title="t", markdown_body=src)
    assert stats.images_embedded == 0
    assert stats.images_skipped_remote == 1
    assert "example.com" in xml


def test_resolver_returning_none_emits_visible_placeholder():
    src = "![lost](images/lost.png)\n"
    xml, stats = build_page_xml(
        page_id="x", title="t", markdown_body=src,
        image_resolver=lambda u, a: None,
    )
    assert stats.images_embedded == 0
    assert "image missing" in xml


def test_image_only_paragraph_emits_image_not_text():
    src = "![just](images/x.png)\n"

    def resolver(url, alt):
        return OneNoteImageRef(url=url, alt=alt, data=b"x", format="png")

    xml, stats = build_page_xml(
        page_id="x", title="t", markdown_body=src,
        image_resolver=resolver,
    )
    assert stats.images_embedded == 1
    assert stats.paragraphs == 0


# ---- attached files -----------------------------------------------------


def test_attached_files_section_emits_inserted_file_per_file(tmp_path):
    f1 = tmp_path / "doc.pdf"
    f1.write_bytes(b"pdf")
    f2 = tmp_path / "slides.pptx"
    f2.write_bytes(b"pptx")
    xml, stats = build_page_xml(
        page_id="x", title="t", markdown_body="",
        attached_files=[
            OneNoteAttachedFile(path=f1, label="The Doc"),
            OneNoteAttachedFile(path=f2, label="Slides"),
        ],
    )
    assert stats.attached_files == 2
    inserts = re.findall(r'<one:InsertedFile[^>]*/>', xml)
    assert len(inserts) == 2
    assert "preferredName=\"The Doc\"" in xml
    assert "preferredName=\"Slides\"" in xml
    # Each carries pathSource pointing at the on-disk file so OneNote
    # can copy it into the page on UpdatePageContent.
    assert str(f1.resolve()) in xml
    assert str(f2.resolve()) in xml


def test_attached_files_section_skips_missing_paths(tmp_path):
    missing = tmp_path / "nope.pdf"
    xml, stats = build_page_xml(
        page_id="x", title="t", markdown_body="",
        attached_files=[OneNoteAttachedFile(path=missing, label="x")],
    )
    assert stats.attached_files == 0
    assert "InsertedFile" not in xml


# ---- xml-safety helpers -------------------------------------------------


def test_cdata_escapes_close_marker():
    """A literal ']]>' inside payload must be split so it doesn't
    terminate the CDATA section early."""
    out = cdata("payload with ]]> embedded")
    assert "]]>" in out
    assert "]]]]><![CDATA[>" in out


def test_xml_attr_escapes_quote_amp_lt():
    assert xml_attr('a"b&c<d') == "a&quot;b&amp;c&lt;d"


# ---- title quoting ------------------------------------------------------


def test_title_with_special_chars_passes_through_cdata():
    """Title is CDATA-wrapped so & < > don't need explicit escapes
    but should still render correctly when round-tripped."""
    xml, _ = build_page_xml(
        page_id="x", title="A & B < C > D", markdown_body="",
    )
    root = _parse(xml)
    title_t = root.find(".//one:Title/one:OE/one:T", _NS)
    assert title_t is not None
    assert title_t.text == "A & B < C > D"
