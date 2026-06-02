"""Notion REST client for the experimental export feature (#79).

Wraps just the endpoints the export feature needs:

- ``GET /v1/users/me`` -- verify the integration token works.
- ``POST /v1/search`` -- list pages the integration has been shared
  with (the picker dialog's top-level entries).
- ``GET /v1/blocks/<id>/children`` -- list a page's child blocks; the
  picker filters to ``type == "child_page"`` to surface sub-pages.
- ``POST /v1/pages`` -- create the exported page as a child of the
  picked parent, with the converted Markdown attached as ``children``.
- ``POST /v1/file_uploads`` + send + finalize -- upload a local image
  so it can be referenced as a ``type: "file_upload"`` image block.

Everything is hand-rolled against requests rather than the
``notion-client`` SDK because the SDK doesn't help with the
formatting layer (where the real complexity lives) and pulling it
in would inflate the PyInstaller bundle for marginal value.

All methods raise :class:`NotionAPIError` on non-2xx responses with
the upstream body attached so the caller can surface useful messages.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

import requests


# Notion-Version header. 2025-09-03 is the data-sources / file-uploads
# generation Aaron's workspace memory pins as the active surface; older
# versions reject the file_uploads endpoint.
NOTION_VERSION = "2025-09-03"
BASE_URL = "https://api.notion.com"
DEFAULT_TIMEOUT = 30  # seconds; uploads can be larger so callers may bump


class NotionAPIError(RuntimeError):
    """Raised on any non-2xx response. Carries the status code + body."""

    def __init__(self, status: int, message: str, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass(frozen=True)
class NotionPageRef:
    """A page the user can pick as the export parent.

    ``title`` is the pre-rendered display label (for the picker rows
    + favorites/recents). ``has_children`` drives the lazy expansion
    icon -- pages with no children render as leaves.
    """
    id: str
    title: str
    has_children: bool = True


class NotionClient:
    """Tiny REST client. Synchronous; callers wrap in a QThread when
    the call may block (verify, page-create, image-upload)."""

    def __init__(self, token: str, *, session: Optional[requests.Session] = None) -> None:
        self._token = token.strip()
        self._session = session or requests.Session()

    # ---- low-level helpers ---------------------------------------------

    def _headers(self, *, content_type: Optional[str] = "application/json") -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": NOTION_VERSION,
        }
        if content_type:
            h["Content-Type"] = content_type
        return h

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> dict[str, Any]:
        url = path if path.startswith("http") else f"{BASE_URL}{path}"
        resp = self._session.request(
            method, url, headers=self._headers(), json=json, params=params,
            timeout=timeout,
        )
        if resp.status_code // 100 != 2:
            raise NotionAPIError(
                resp.status_code,
                f"Notion {method} {path} returned {resp.status_code}",
                resp.text,
            )
        if not resp.content:
            return {}
        return resp.json()

    # ---- verify ---------------------------------------------------------

    def verify(self) -> dict[str, Any]:
        """Confirm the token works. Returns the bot user payload.

        A 401 or 403 here means the token is missing / revoked; the
        caller surfaces that as "not connected". Any other exception
        bubbles up unchanged.
        """
        return self._request("GET", "/v1/users/me")

    # ---- browse ---------------------------------------------------------

    def list_accessible_pages(self) -> list[NotionPageRef]:
        """Top-level entries for the picker: every page the integration
        has been shared with.

        Notion's ``/v1/search`` is the canonical way to enumerate this;
        the integration only sees what's been shared with it. Pages
        return as the picker's roots; from each root the picker walks
        children via :meth:`list_child_pages`.
        """
        payload = {
            "filter": {"property": "object", "value": "page"},
            "page_size": 100,
        }
        data = self._request("POST", "/v1/search", json=payload)
        return [_page_ref_from_object(o) for o in data.get("results", [])]

    def list_child_pages(self, parent_id: str) -> list[NotionPageRef]:
        """Return the ``child_page`` blocks of ``parent_id`` as the
        picker's next-level entries.

        Notion's block-children listing is paginated; we walk every
        page so the picker shows the whole sub-tree without hidden
        siblings. Block types other than ``child_page`` (paragraphs,
        lists, etc.) are filtered out -- the picker is a page tree.
        """
        out: list[NotionPageRef] = []
        cursor: Optional[str] = None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            data = self._request(
                "GET", f"/v1/blocks/{parent_id}/children", params=params,
            )
            for block in data.get("results", []):
                if block.get("type") == "child_page":
                    title = block.get("child_page", {}).get("title", "(untitled)")
                    out.append(NotionPageRef(
                        id=block["id"],
                        title=title,
                        has_children=bool(block.get("has_children", False)),
                    ))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if not cursor:
                break
        return out

    # ---- create ---------------------------------------------------------

    def create_page(
        self,
        *,
        parent_id: str,
        title: str,
        children: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Create a page under ``parent_id`` with ``title`` and the
        given ``children`` blocks.

        Returns the created page object; the picker dialog reads
        ``url`` to surface a "View in Notion" link and ``id`` for
        the favorites/recents store.

        Note on size: Notion's ``children`` array is capped at 100
        blocks per request. Longer documents need a follow-up
        ``PATCH /v1/blocks/<page_id>/children`` for the overflow; see
        :meth:`append_block_children` below.
        """
        head = children[:100]
        tail = children[100:]
        body = {
            "parent": {"type": "page_id", "page_id": parent_id},
            "properties": {
                "title": {
                    "title": [{"type": "text", "text": {"content": title}}],
                },
            },
            "children": head,
        }
        page = self._request("POST", "/v1/pages", json=body)
        if tail:
            # Notion enforces the 100-block cap per request, both on
            # page create and on block-children patch; keep chunking
            # the tail until we've appended everything.
            page_id = page.get("id")
            for chunk in _chunk(tail, 100):
                self.append_block_children(page_id, chunk)
        return page

    def append_block_children(
        self, block_id: str, children: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Append ``children`` to ``block_id``. Caller pre-chunks to 100."""
        return self._request(
            "PATCH",
            f"/v1/blocks/{block_id}/children",
            json={"children": children},
        )

    # ---- images ---------------------------------------------------------

    def upload_image(self, path: Path) -> str:
        """Upload a local image and return its ``file_upload`` id.

        Three steps per Notion's documented protocol:

        1. ``POST /v1/file_uploads`` -- creates the upload record;
           returns ``{ id, upload_url, ... }``.
        2. ``POST <upload_url>`` (multipart/form-data with the file in
           the ``file`` field) -- sends the bytes.
        3. The upload object's ``id`` can now be referenced via
           ``image`` block ``type: "file_upload"``.
        """
        path = Path(path)
        if not path.is_file():
            raise NotionAPIError(0, f"image not found: {path}", "")

        # Step 1: create the upload record.
        create = self._request(
            "POST",
            "/v1/file_uploads",
            json={"mode": "single_part"},
        )
        upload_id = create["id"]
        upload_url = create["upload_url"]

        # Step 2: send the file bytes via multipart. Notion's
        # /file_uploads send endpoint expects the body in a field
        # literally named "file"; the upload_url already encodes the
        # endpoint, so we just POST to it.
        with path.open("rb") as fh:
            files = {"file": (path.name, fh, _guess_mime(path))}
            # Authorization is still required on the send call; only
            # Content-Type changes (multipart, set by requests).
            send_headers = {
                "Authorization": f"Bearer {self._token}",
                "Notion-Version": NOTION_VERSION,
            }
            resp = self._session.post(
                upload_url, headers=send_headers, files=files,
                timeout=DEFAULT_TIMEOUT * 2,
            )
        if resp.status_code // 100 != 2:
            raise NotionAPIError(
                resp.status_code,
                f"Notion file_upload send returned {resp.status_code}",
                resp.text,
            )
        return upload_id


def _page_ref_from_object(obj: dict[str, Any]) -> NotionPageRef:
    """Convert a Notion page search-result object into a NotionPageRef.

    Search results carry ``properties.title`` as a rich-text array;
    we flatten to plain text for display. Pages with no title get a
    fallback label so the picker isn't full of blanks.
    """
    page_id = obj.get("id", "")
    props = obj.get("properties", {}) or {}
    # Pages always have a "title" property; databases may have it under
    # a different name -- we don't surface databases in the picker.
    title_prop = None
    for v in props.values():
        if isinstance(v, dict) and v.get("type") == "title":
            title_prop = v.get("title", [])
            break
    if title_prop:
        title = "".join(
            (t.get("plain_text") or "") for t in title_prop
        ).strip() or "(untitled)"
    else:
        title = "(untitled)"
    return NotionPageRef(id=page_id, title=title, has_children=True)


def _chunk(items: list[Any], size: int) -> Iterable[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _guess_mime(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".svg": "image/svg+xml",
    }.get(ext, "application/octet-stream")
