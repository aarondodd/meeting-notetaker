"""Tests for the Notion + Confluence REST clients (#79).

Both clients accept an injected ``requests.Session`` so we can
substitute a fake adapter that returns canned responses, keeping
the tests offline + deterministic.
"""
from __future__ import annotations

import io
import json
from typing import Any

import pytest

requests = pytest.importorskip("requests")

from meeting_notetaker.integrations.confluence_api import (  # noqa: E402
    ConfluenceAPIError,
    ConfluenceClient,
    ConfluenceNodeRef,
)
from meeting_notetaker.integrations.notion_api import (  # noqa: E402
    NotionAPIError,
    NotionClient,
    NotionPageRef,
)


# ---- fake session ---------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int, payload: Any = None, *, text: str = "") -> None:
        self.status_code = status
        if payload is None and not text:
            self._content = b""
            self._json = None
            self.text = ""
        elif payload is not None:
            body = json.dumps(payload).encode()
            self._content = body
            self._json = payload
            self.text = body.decode()
        else:
            self._content = text.encode()
            self._json = None
            self.text = text

    @property
    def content(self) -> bytes:
        return self._content

    def json(self) -> Any:
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _FakeSession:
    """Captures every request so tests can assert on the shape sent +
    return canned responses keyed by (method, path-substring)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses: list[_FakeResponse] = []

    def queue(self, *responses: _FakeResponse) -> None:
        self.responses.extend(responses)

    def request(self, method, url, **kwargs) -> _FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError(
                f"no canned response for {method} {url}; calls: {self.calls}"
            )
        return self.responses.pop(0)

    # Compatibility with code that calls session.post directly.
    def post(self, url, **kwargs) -> _FakeResponse:
        return self.request("POST", url, **kwargs)


# ---- Notion ---------------------------------------------------------------


def test_notion_verify_hits_users_me_with_token_and_version():
    sess = _FakeSession()
    sess.queue(_FakeResponse(200, {"id": "bot-1", "name": "Test"}))
    client = NotionClient("secret_abc", session=sess)

    out = client.verify()
    assert out["id"] == "bot-1"
    call = sess.calls[0]
    assert call["method"] == "GET"
    assert call["url"].endswith("/v1/users/me")
    headers = call["headers"]
    assert headers["Authorization"] == "Bearer secret_abc"
    assert headers["Notion-Version"]  # any non-empty version


def test_notion_verify_raises_on_401():
    sess = _FakeSession()
    sess.queue(_FakeResponse(401, text="unauthorized"))
    client = NotionClient("bad_token", session=sess)
    with pytest.raises(NotionAPIError) as exc:
        client.verify()
    assert exc.value.status == 401


def test_notion_list_accessible_pages_returns_titled_refs():
    sess = _FakeSession()
    sess.queue(_FakeResponse(200, {
        "results": [
            {
                "id": "page-1",
                "properties": {
                    "title": {
                        "type": "title",
                        "title": [{"plain_text": "Meetings"}],
                    },
                },
            },
            {
                "id": "page-2",
                "properties": {
                    "title": {
                        "type": "title",
                        "title": [{"plain_text": "Weekly "}, {"plain_text": "Sync"}],
                    },
                },
            },
            {
                "id": "page-3",
                # No title property -> defaults to "(untitled)".
                "properties": {},
            },
        ],
    }))
    client = NotionClient("t", session=sess)
    pages = client.list_accessible_pages()
    assert len(pages) == 3
    assert pages[0] == NotionPageRef(id="page-1", title="Meetings", has_children=True)
    assert pages[1].title == "Weekly Sync"
    assert pages[2].title == "(untitled)"


def test_notion_list_child_pages_filters_to_child_page_blocks():
    sess = _FakeSession()
    sess.queue(_FakeResponse(200, {
        "results": [
            {"id": "p-1", "type": "child_page",
             "child_page": {"title": "First"}, "has_children": True},
            {"id": "para-1", "type": "paragraph", "paragraph": {}},
            {"id": "p-2", "type": "child_page",
             "child_page": {"title": "Second"}, "has_children": False},
        ],
        "has_more": False,
    }))
    client = NotionClient("t", session=sess)
    pages = client.list_child_pages("parent")
    assert [p.id for p in pages] == ["p-1", "p-2"]
    assert pages[0].has_children is True
    assert pages[1].has_children is False


def test_notion_list_child_pages_walks_pagination():
    sess = _FakeSession()
    sess.queue(
        _FakeResponse(200, {
            "results": [
                {"id": "p-1", "type": "child_page",
                 "child_page": {"title": "A"}, "has_children": True},
            ],
            "has_more": True,
            "next_cursor": "cur-2",
        }),
        _FakeResponse(200, {
            "results": [
                {"id": "p-2", "type": "child_page",
                 "child_page": {"title": "B"}, "has_children": True},
            ],
            "has_more": False,
        }),
    )
    client = NotionClient("t", session=sess)
    pages = client.list_child_pages("parent")
    assert [p.id for p in pages] == ["p-1", "p-2"]
    # Second call should have carried the cursor.
    assert sess.calls[1]["params"]["start_cursor"] == "cur-2"


def test_notion_create_page_posts_parent_title_and_children():
    sess = _FakeSession()
    sess.queue(_FakeResponse(200, {
        "id": "new-page", "url": "https://notion.so/new-page",
    }))
    client = NotionClient("t", session=sess)
    children = [{"type": "paragraph", "paragraph": {"rich_text": []}}]
    out = client.create_page(parent_id="parent", title="My Title", children=children)
    assert out["id"] == "new-page"
    body = sess.calls[0]["json"]
    assert body["parent"] == {"type": "page_id", "page_id": "parent"}
    assert body["properties"]["title"]["title"][0]["text"]["content"] == "My Title"
    assert body["children"] == children


def test_notion_create_page_chunks_children_past_100():
    sess = _FakeSession()
    sess.queue(
        # The initial create takes the first 100.
        _FakeResponse(200, {"id": "new-page"}),
        # Two PATCH calls for the next 100 + 50.
        _FakeResponse(200, {}),
        _FakeResponse(200, {}),
    )
    client = NotionClient("t", session=sess)
    blocks = [
        {"type": "paragraph", "paragraph": {"rich_text": []}}
        for _ in range(250)
    ]
    client.create_page(parent_id="p", title="t", children=blocks)
    # Three POST/PATCH calls.
    assert len(sess.calls) == 3
    assert len(sess.calls[0]["json"]["children"]) == 100
    assert len(sess.calls[1]["json"]["children"]) == 100
    assert len(sess.calls[2]["json"]["children"]) == 50


def test_notion_upload_image_three_step_flow(tmp_path):
    """Upload object create -> multipart send to upload_url ->
    returned file_upload id is what get plugged into image blocks."""
    img = tmp_path / "foo.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    sess = _FakeSession()
    sess.queue(
        # Step 1: create upload record.
        _FakeResponse(200, {"id": "upl-1", "upload_url": "https://api.notion/upload/xyz"}),
        # Step 2: send bytes -- session.post is what the client calls.
        _FakeResponse(200, {"id": "upl-1", "status": "uploaded"}),
    )
    client = NotionClient("t", session=sess)
    upload_id = client.upload_image(img)
    assert upload_id == "upl-1"
    # Step 1: POST /v1/file_uploads with mode=single_part.
    assert sess.calls[0]["json"] == {"mode": "single_part"}
    # Step 2: POST to the upload_url, multipart "file" field carried.
    assert sess.calls[1]["url"] == "https://api.notion/upload/xyz"
    assert "files" in sess.calls[1]


def test_notion_upload_image_missing_file_raises(tmp_path):
    client = NotionClient("t", session=_FakeSession())
    with pytest.raises(NotionAPIError) as exc:
        client.upload_image(tmp_path / "not-there.png")
    assert "image not found" in str(exc.value)


# ---- Confluence -----------------------------------------------------------


def test_confluence_verify_uses_basic_auth_header():
    sess = _FakeSession()
    sess.queue(_FakeResponse(200, {"accountId": "user-1"}))
    client = ConfluenceClient(
        "https://example.atlassian.net/wiki",
        "user@example.com",
        "ATATT-token",
        session=sess,
    )
    out = client.verify()
    assert out["accountId"] == "user-1"
    headers = sess.calls[0]["headers"]
    # Basic auth header is base64 of "email:token".
    expected = "Basic " + __import__("base64").b64encode(
        b"user@example.com:ATATT-token"
    ).decode()
    assert headers["Authorization"] == expected
    assert sess.calls[0]["url"].endswith("/wiki/rest/api/user/current")


def test_confluence_list_spaces_returns_node_refs():
    sess = _FakeSession()
    sess.queue(_FakeResponse(200, {
        "results": [
            {"id": 100, "name": "Engineering"},
            {"id": 200, "name": "Product"},
        ],
    }))
    client = ConfluenceClient("https://x/wiki", "e", "t", session=sess)
    spaces = client.list_spaces()
    assert spaces == [
        ConfluenceNodeRef(id="100", title="Engineering", kind="space", space_id="100", has_children=True),
        ConfluenceNodeRef(id="200", title="Product", kind="space", space_id="200", has_children=True),
    ]


def test_confluence_list_root_pages_passes_space_filter_and_root_depth():
    sess = _FakeSession()
    sess.queue(_FakeResponse(200, {
        "results": [
            {"id": 9001, "title": "Welcome", "spaceId": 100},
        ],
    }))
    client = ConfluenceClient("https://x/wiki", "e", "t", session=sess)
    pages = client.list_root_pages("100")
    assert pages == [
        ConfluenceNodeRef(id="9001", title="Welcome", kind="page", space_id="100", has_children=True),
    ]
    params = sess.calls[0]["params"]
    assert params["space-id"] == "100"
    assert params["depth"] == "root"


def test_confluence_create_page_posts_storage_format():
    sess = _FakeSession()
    sess.queue(_FakeResponse(200, {
        "id": "999", "_links": {"webui": "/spaces/ENG/pages/999/Title"},
    }))
    client = ConfluenceClient("https://x/wiki", "e", "t", session=sess)
    out = client.create_page(
        parent_id="9001",
        space_id="100",
        title="My Sync 2026-06-02",
        storage_xml="<p>hi</p>",
    )
    assert out["id"] == "999"
    body = sess.calls[0]["json"]
    assert body["spaceId"] == "100"
    assert body["parentId"] == "9001"
    assert body["title"] == "My Sync 2026-06-02"
    # Storage format -- NOT markdown (L-2026-05-17-001).
    assert body["body"]["representation"] == "storage"
    assert body["body"]["value"] == "<p>hi</p>"


def test_confluence_create_page_raises_on_400():
    sess = _FakeSession()
    sess.queue(_FakeResponse(400, text="bad request"))
    client = ConfluenceClient("https://x/wiki", "e", "t", session=sess)
    with pytest.raises(ConfluenceAPIError) as exc:
        client.create_page(parent_id="p", space_id="s", title="t", storage_xml="")
    assert exc.value.status == 400


def test_confluence_upload_attachment_uses_multipart_and_no_check_header(tmp_path):
    img = tmp_path / "foo.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    sess = _FakeSession()
    sess.queue(_FakeResponse(200, {"results": [{"id": "att-1"}]}))
    client = ConfluenceClient("https://x/wiki", "e", "t", session=sess)
    out = client.upload_attachment("999", img)
    assert out["results"][0]["id"] == "att-1"
    call = sess.calls[0]
    assert "files" in call
    # Atlassian requires X-Atlassian-Token: no-check for multipart uploads.
    assert call["headers"]["X-Atlassian-Token"] == "no-check"
    assert call["url"].endswith("/wiki/rest/api/content/999/child/attachment")


def test_confluence_page_url_combines_base_and_webui():
    client = ConfluenceClient("https://example.atlassian.net/wiki", "e", "t")
    url = client.page_url({"_links": {"webui": "/spaces/ENG/pages/123"}})
    assert url == "https://example.atlassian.net/wiki/spaces/ENG/pages/123"


def test_confluence_page_url_fallback_when_no_webui():
    client = ConfluenceClient("https://example.atlassian.net/wiki", "e", "t")
    url = client.page_url({"id": "555"})
    assert "/pages/555" in url
