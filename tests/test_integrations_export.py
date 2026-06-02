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
