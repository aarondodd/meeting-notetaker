"""Export orchestrator tests (#79).

Drives the Notion + Confluence export flows against a fake client
so the image-upload + create-page + URL-return wiring is exercised
without network. Pure Python.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest

pytest.importorskip("mistune")

from meeting_notetaker.integrations.export import (  # noqa: E402
    collect_image_refs,
    export_to_confluence,
    export_to_notion,
    is_local_image_ref,
    resolve_local_image_path,
)


# ---- collect_image_refs ---------------------------------------------------


def test_collect_image_refs_block_paragraph():
    refs = collect_image_refs("![alt one](images/a.png)\n\n![two](https://x/b.png)")
    assert refs == [("images/a.png", "alt one"), ("https://x/b.png", "two")]


def test_collect_image_refs_inline_image_inside_paragraph():
    refs = collect_image_refs("text ![inline](foo.png) more text")
    assert refs == [("foo.png", "inline")]


def test_collect_image_refs_empty_when_none():
    assert collect_image_refs("") == []
    assert collect_image_refs("# Heading\n\nNo images here.") == []


# ---- is_local_image_ref ---------------------------------------------------


def test_is_local_image_ref_classifies_correctly():
    assert is_local_image_ref("images/foo.png") is True
    assert is_local_image_ref("./foo.png") is True
    assert is_local_image_ref("/absolute/foo.png") is True
    assert is_local_image_ref("file:///abs/foo.png") is True
    assert is_local_image_ref("https://example.com/foo.png") is False
    assert is_local_image_ref("http://example.com/foo.png") is False
    assert is_local_image_ref("data:image/png;base64,XXX") is False
    assert is_local_image_ref("") is False


def test_resolve_local_image_path_finds_existing_file(tmp_path):
    img = tmp_path / "images"
    img.mkdir()
    f = img / "foo.png"
    f.write_bytes(b"x")
    assert resolve_local_image_path("images/foo.png", base_dir=tmp_path) == f.resolve()


def test_resolve_local_image_path_returns_none_when_missing(tmp_path):
    assert resolve_local_image_path("images/missing.png", base_dir=tmp_path) is None


# ---- fake clients ---------------------------------------------------------


class _FakeNotionClient:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str]] = []
        self.last_create: Optional[dict] = None
        self._upload_counter = 0

    def upload_image(self, path: Path) -> str:
        return self.upload_file(path)

    def upload_file(self, path: Path, *, mime: str = "") -> str:
        self._upload_counter += 1
        upload_id = f"upl-{self._upload_counter}"
        self.uploads.append((str(path), upload_id))
        return upload_id

    def create_page(self, *, parent_id, title, children) -> dict[str, Any]:
        self.last_create = {
            "parent_id": parent_id, "title": title, "children": children,
        }
        return {
            "id": "new-page-id",
            "url": "https://www.notion.so/new-page-id",
        }


class _FakeConfluenceClient:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.attachments: list[tuple[str, str]] = []  # (page_id, filename)
        self.updates: list[dict[str, Any]] = []
        self._next_id = 1000

    def create_page(self, *, parent_id, space_id, title, storage_xml) -> dict[str, Any]:
        page_id = str(self._next_id)
        self._next_id += 1
        rec = {
            "parent_id": parent_id, "space_id": space_id, "title": title,
            "storage_xml": storage_xml, "id": page_id,
        }
        self.created.append(rec)
        return {
            "id": page_id,
            "_links": {"webui": f"/spaces/SPACE/pages/{page_id}"},
        }

    def upload_attachment(self, page_id, path: Path):
        self.attachments.append((str(page_id), path.name))
        return {"results": [{"id": "att-1"}]}

    def update_page(self, *, page_id, title, storage_xml):
        self.updates.append({
            "page_id": page_id, "title": title, "storage_xml": storage_xml,
        })
        return {}

    def page_url(self, page):
        webui = (page.get("_links") or {}).get("webui", "")
        return "https://example.atlassian.net/wiki" + webui


# ---- Notion export --------------------------------------------------------


def test_notion_export_no_images_single_create_call(tmp_path):
    client = _FakeNotionClient()
    result = export_to_notion(
        client=client,
        parent_id="parent-1",
        title="My sync",
        markdown_body="# Hi\n\nSome text.",
        session_dir=tmp_path,
    )
    assert client.uploads == []
    assert client.last_create["parent_id"] == "parent-1"
    assert client.last_create["title"] == "My sync"
    assert result.target == "notion"
    assert result.page_id == "new-page-id"
    assert "notion.so" in result.page_url


def test_notion_export_uploads_local_images_before_create(tmp_path):
    img = tmp_path / "images"
    img.mkdir()
    a = img / "a.png"; a.write_bytes(b"a-bytes")
    b = img / "b.png"; b.write_bytes(b"b-bytes")
    md = (
        "# Header\n\n"
        "![first](images/a.png)\n\n"
        "Paragraph.\n\n"
        "![second](images/b.png)\n"
    )
    client = _FakeNotionClient()
    export_to_notion(
        client=client,
        parent_id="parent-1",
        title="t",
        markdown_body=md,
        session_dir=tmp_path,
    )
    # Both local images uploaded.
    assert len(client.uploads) == 2
    upload_paths = sorted(p for p, _ in client.uploads)
    assert upload_paths == sorted([str(a.resolve()), str(b.resolve())])
    # The created page's children include image blocks referencing the upload ids.
    image_blocks = [
        b for b in client.last_create["children"] if b["type"] == "image"
    ]
    assert len(image_blocks) == 2
    assert image_blocks[0]["image"]["type"] == "file_upload"
    assert image_blocks[1]["image"]["type"] == "file_upload"


def test_notion_export_leaves_remote_images_external(tmp_path):
    md = "![remote](https://example.com/c.png)"
    client = _FakeNotionClient()
    export_to_notion(
        client=client, parent_id="p", title="t",
        markdown_body=md, session_dir=tmp_path,
    )
    assert client.uploads == []  # nothing uploaded
    image_block = [
        b for b in client.last_create["children"] if b["type"] == "image"
    ][0]
    assert image_block["image"]["type"] == "external"
    assert image_block["image"]["external"]["url"] == "https://example.com/c.png"


def test_notion_export_handles_missing_local_image_gracefully(tmp_path):
    """If the local image file is gone (user deleted it after writing
    the markdown), the export must NOT crash -- it should emit a
    placeholder image block instead so the rest of the page still
    lands."""
    md = "![ghost](images/missing.png)"
    client = _FakeNotionClient()
    result = export_to_notion(
        client=client, parent_id="p", title="t",
        markdown_body=md, session_dir=tmp_path,
    )
    assert client.uploads == []
    assert result.page_id == "new-page-id"


def test_notion_export_message_notes_sibling_creation(tmp_path):
    client = _FakeNotionClient()
    result = export_to_notion(
        client=client, parent_id="p", title="t",
        markdown_body="hi", session_dir=tmp_path,
    )
    assert "sibling" in result.message.lower()


def test_notion_export_uploads_attachments_and_appends_file_blocks(tmp_path):
    """When the picker checkbox is set, attachments upload via the
    file_upload endpoint + appear as file blocks under an Attachments
    heading at the end of the page."""
    from meeting_notetaker.integrations.export import ExportAttachment

    pdf = tmp_path / "agenda.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")
    docx = tmp_path / "notes.docx"
    docx.write_bytes(b"PK stub")

    client = _FakeNotionClient()
    export_to_notion(
        client=client, parent_id="p", title="t",
        markdown_body="# Body\n\nNo inline images.",
        session_dir=tmp_path,
        attachments=[
            ExportAttachment(path=pdf, display_name="Meeting agenda", mime="application/pdf"),
            ExportAttachment(path=docx, display_name="Notes", mime=""),
        ],
    )
    # Two uploads via the file upload path.
    uploaded_paths = sorted(p for p, _id in client.uploads)
    assert str(pdf) in uploaded_paths
    assert str(docx) in uploaded_paths
    # The last create_page call's children include an "Attachments"
    # heading followed by file blocks for each attachment.
    children = client.last_create["children"]
    heading_indices = [
        i for i, b in enumerate(children)
        if b.get("type") == "heading_2"
        and b["heading_2"]["rich_text"][0]["text"]["content"] == "Attachments"
    ]
    assert len(heading_indices) == 1
    after = children[heading_indices[0] + 1:]
    file_blocks = [b for b in after if b.get("type") == "file"]
    assert len(file_blocks) == 2
    # Each file block references its uploaded id.
    assert file_blocks[0]["file"]["type"] == "file_upload"
    assert file_blocks[0]["file"]["file_upload"]["id"]
    # Display name reaches the caption + name.
    captions = {b["file"]["caption"][0]["text"]["content"] for b in file_blocks}
    assert "Meeting agenda" in captions
    assert "Notes" in captions


def test_notion_export_skips_missing_attachments_silently(tmp_path):
    from meeting_notetaker.integrations.export import ExportAttachment

    client = _FakeNotionClient()
    export_to_notion(
        client=client, parent_id="p", title="t",
        markdown_body="x", session_dir=tmp_path,
        attachments=[
            ExportAttachment(path=tmp_path / "missing.pdf", display_name="ghost"),
        ],
    )
    assert client.uploads == []  # nothing uploaded
    # No Attachments heading when no successful uploads.
    children = client.last_create["children"]
    headings = [
        b for b in children
        if b.get("type") == "heading_2"
        and b["heading_2"]["rich_text"][0]["text"]["content"] == "Attachments"
    ]
    assert headings == []


# ---- Confluence export ---------------------------------------------------


def test_confluence_export_no_images_single_create(tmp_path):
    client = _FakeConfluenceClient()
    result = export_to_confluence(
        client=client,
        parent_id="9001", space_id="100",
        title="My sync",
        markdown_body="# Hi\n\nNo images here.",
        session_dir=tmp_path,
    )
    assert len(client.created) == 1
    assert client.created[0]["space_id"] == "100"
    assert "<h1>Hi</h1>" in client.created[0]["storage_xml"]
    assert client.attachments == []
    assert client.updates == []
    assert "atlassian.net/wiki" in result.page_url


def test_confluence_export_with_images_two_passes(tmp_path):
    img = tmp_path / "images"
    img.mkdir()
    (img / "a.png").write_bytes(b"a")
    md = "# Hi\n\n![one](images/a.png)\n"
    client = _FakeConfluenceClient()
    result = export_to_confluence(
        client=client,
        parent_id="9001", space_id="100",
        title="With image",
        markdown_body=md,
        session_dir=tmp_path,
    )
    # Two-pass: a placeholder create + an update.
    assert len(client.created) == 1
    assert "Uploading images" in client.created[0]["storage_xml"]
    assert len(client.attachments) == 1
    assert client.attachments[0][1] == "a.png"
    assert len(client.updates) == 1
    final_xml = client.updates[0]["storage_xml"]
    # The final XML carries ri:attachment ri:filename="a.png".
    assert '<ri:attachment ri:filename="a.png" />' in final_xml
    # The placeholder body is not what landed.
    assert "Uploading images" not in final_xml
    assert result.page_id == "1000"


def test_confluence_export_remote_image_uses_ri_url(tmp_path):
    md = "![hosted](https://example.com/x.png)"
    client = _FakeConfluenceClient()
    export_to_confluence(
        client=client,
        parent_id="9001", space_id="100",
        title="t", markdown_body=md, session_dir=tmp_path,
    )
    # Remote image -> single pass.
    assert client.attachments == []
    assert client.updates == []
    assert '<ri:url ri:value="https://example.com/x.png" />' in client.created[0]["storage_xml"]


def test_confluence_export_message_notes_sibling_creation(tmp_path):
    client = _FakeConfluenceClient()
    result = export_to_confluence(
        client=client, parent_id="p", space_id="s",
        title="t", markdown_body="hi", session_dir=tmp_path,
    )
    assert "sibling" in result.message.lower()


def test_confluence_export_attaches_session_files_and_appends_listing(tmp_path):
    """When attachments are present, the export forces a 2-pass run:
    placeholder create -> attachments upload -> body update with a
    storage-XML Attachments section linking each ri:attachment."""
    from meeting_notetaker.integrations.export import ExportAttachment

    pdf = tmp_path / "agenda.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    docx = tmp_path / "spec.docx"
    docx.write_bytes(b"PK")

    client = _FakeConfluenceClient()
    export_to_confluence(
        client=client, parent_id="9001", space_id="100",
        title="With attachments",
        markdown_body="# Body\n\nNo inline images.",
        session_dir=tmp_path,
        attachments=[
            ExportAttachment(path=pdf, display_name="Agenda"),
            ExportAttachment(path=docx, display_name="Spec"),
        ],
    )
    # Placeholder create.
    assert len(client.created) == 1
    page_id = client.created[0]["id"]
    # Each attachment uploaded to the new page.
    assert len(client.attachments) == 2
    uploaded_filenames = sorted(name for _pid, name in client.attachments)
    assert uploaded_filenames == ["agenda.pdf", "spec.docx"]
    # Body update lands and carries the attachments section.
    assert len(client.updates) == 1
    final_xml = client.updates[0]["storage_xml"]
    assert "<h2>Attachments</h2>" in final_xml
    assert '<ri:attachment ri:filename="agenda.pdf" />' in final_xml
    assert '<ri:attachment ri:filename="spec.docx" />' in final_xml
    # Display names ride along inside the link bodies.
    assert "Agenda" in final_xml
    assert "Spec" in final_xml


def test_confluence_export_no_attachments_keeps_single_pass(tmp_path):
    """Without attachments + without inline images, the single-pass
    create path stays in place (no placeholder + update churn)."""
    client = _FakeConfluenceClient()
    export_to_confluence(
        client=client, parent_id="9001", space_id="100",
        title="t", markdown_body="# Body",
        session_dir=tmp_path,
        attachments=[],
    )
    assert len(client.created) == 1
    assert client.attachments == []
    assert client.updates == []
