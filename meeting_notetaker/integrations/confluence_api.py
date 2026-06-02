"""Confluence REST client for the experimental export feature (#79).

Wraps the endpoints the export feature needs:

- ``GET /wiki/rest/api/user/current`` -- verify the email + API token.
- ``GET /wiki/api/v2/spaces`` -- list spaces (the picker's top level).
- ``GET /wiki/api/v2/spaces/<id>/pages?root-level=true`` -- list a
  space's root pages (the picker's second level).
- ``GET /wiki/api/v2/pages/<id>/children`` -- list a page's children
  (third level + deeper).
- ``POST /wiki/api/v2/pages`` -- create a page with content_format =
  storage; body is Confluence storage XML (NOT markdown, see
  L-2026-05-17-001).
- ``POST /wiki/rest/api/content/<id>/child/attachment`` -- upload an
  image as an attachment; the storage XML references it via
  ``<ac:image><ri:attachment ri:filename="..." />``.

Confluence Cloud uses Atlassian basic auth (email + API token).
Self-hosted servers use the same shape with a different base URL.
"""
from __future__ import annotations

from base64 import b64encode
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import requests


DEFAULT_TIMEOUT = 30


class ConfluenceAPIError(RuntimeError):
    """Raised on any non-2xx response. Carries status + body."""

    def __init__(self, status: int, message: str, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass(frozen=True)
class ConfluenceNodeRef:
    """A picker row -- either a space (root) or a page.

    ``kind`` is "space" or "page". For pages, ``space_id`` carries the
    parent space so the picker can keep the space hierarchy visible
    in favorites/recents labels. ``has_children`` drives the lazy-
    expansion icon.
    """
    id: str
    title: str
    kind: str  # "space" | "page"
    space_id: Optional[str] = None
    has_children: bool = True


class ConfluenceClient:
    """Hand-rolled REST client.

    The Cloud + Server REST surface differ slightly; v2 ('/wiki/api/v2')
    is the canonical Cloud path. For server installs the same paths
    typically work; if they don't, the user supplies a server-style
    base_url that already encodes the right prefix.
    """

    def __init__(
        self,
        base_url: str,
        email: str,
        api_token: str,
        *,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._email = email.strip()
        self._token = api_token.strip()
        self._session = session or requests.Session()

    # ---- low-level helpers ---------------------------------------------

    def _auth_header(self) -> str:
        creds = f"{self._email}:{self._token}".encode("utf-8")
        return "Basic " + b64encode(creds).decode("ascii")

    def _headers(self, *, content_type: Optional[str] = "application/json") -> dict[str, str]:
        h = {
            "Authorization": self._auth_header(),
            "Accept": "application/json",
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
        extra_headers: Optional[dict[str, str]] = None,
        files: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        url = path if path.startswith("http") else f"{self._base}{path}"
        if files is not None:
            # multipart: requests sets Content-Type itself; we drop ours.
            headers = self._headers(content_type=None)
            headers["X-Atlassian-Token"] = "no-check"
            if extra_headers:
                headers.update(extra_headers)
            resp = self._session.request(
                method, url, headers=headers, files=files, data=data,
                timeout=timeout,
            )
        else:
            headers = self._headers()
            if extra_headers:
                headers.update(extra_headers)
            resp = self._session.request(
                method, url, headers=headers, json=json, params=params,
                timeout=timeout,
            )
        if resp.status_code // 100 != 2:
            raise ConfluenceAPIError(
                resp.status_code,
                f"Confluence {method} {path} returned {resp.status_code}",
                resp.text,
            )
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    # ---- verify --------------------------------------------------------

    def verify(self) -> dict[str, Any]:
        """Confirm the credentials work. Returns the current user payload."""
        return self._request("GET", "/wiki/rest/api/user/current")

    # ---- browse --------------------------------------------------------

    def list_spaces(self) -> list[ConfluenceNodeRef]:
        """Top-level picker entries: every space the user can see."""
        data = self._request("GET", "/wiki/api/v2/spaces", params={"limit": 250})
        out: list[ConfluenceNodeRef] = []
        for s in data.get("results", []):
            out.append(ConfluenceNodeRef(
                id=str(s.get("id", "")),
                title=s.get("name", "(untitled space)"),
                kind="space",
                space_id=str(s.get("id", "")),
                has_children=True,
            ))
        return out

    def list_root_pages(self, space_id: str) -> list[ConfluenceNodeRef]:
        """Root-level pages within a space."""
        params = {"space-id": space_id, "depth": "root", "limit": 250}
        data = self._request("GET", "/wiki/api/v2/pages", params=params)
        return self._page_refs(data.get("results", []), space_id=space_id)

    def list_child_pages(
        self, page_id: str, *, space_id: Optional[str] = None,
    ) -> list[ConfluenceNodeRef]:
        """Children of a specific page."""
        data = self._request(
            "GET", f"/wiki/api/v2/pages/{page_id}/children",
            params={"limit": 250},
        )
        return self._page_refs(data.get("results", []), space_id=space_id)

    def _page_refs(
        self,
        items: list[dict[str, Any]],
        *,
        space_id: Optional[str],
    ) -> list[ConfluenceNodeRef]:
        out: list[ConfluenceNodeRef] = []
        for p in items:
            out.append(ConfluenceNodeRef(
                id=str(p.get("id", "")),
                title=p.get("title", "(untitled)"),
                kind="page",
                space_id=space_id or str(p.get("spaceId", "") or ""),
                # The API doesn't always inline a has_children flag; we
                # assume yes so the picker offers the expand affordance,
                # and a follow-up call returns an empty list cleanly if
                # the page is a leaf.
                has_children=True,
            ))
        return out

    # ---- create --------------------------------------------------------

    def create_page(
        self,
        *,
        parent_id: str,
        space_id: str,
        title: str,
        storage_xml: str,
    ) -> dict[str, Any]:
        """Create a page under ``parent_id`` in ``space_id`` with the
        given storage-format body.

        Returns the created page object; the caller uses
        ``_links.base + _links.webui`` (or whatever shape Cloud
        returns) to surface a clickable URL.

        L-2026-05-17-001: storage XML, NOT markdown -- the markdown
        converter corrupts code blocks, nested lists, and multi-
        paragraph blockquotes.
        """
        body = {
            "spaceId": space_id,
            "status": "current",
            "title": title,
            "parentId": parent_id,
            "body": {
                "representation": "storage",
                "value": storage_xml,
            },
        }
        return self._request("POST", "/wiki/api/v2/pages", json=body)

    def update_page(
        self,
        *,
        page_id: str,
        title: str,
        storage_xml: str,
    ) -> dict[str, Any]:
        """Replace ``page_id``'s body with new storage XML.

        Used by the attachments path in export.py: we first create a
        placeholder, attach images to the resulting page id, then
        update the body so storage XML references the attachments by
        filename. Confluence v2 requires the current version number on
        update; we GET the page to read it, increment, and send.
        """
        current = self._request("GET", f"/wiki/api/v2/pages/{page_id}")
        version_num = (current.get("version") or {}).get("number") or 1
        body = {
            "id": str(page_id),
            "status": "current",
            "title": title,
            "body": {
                "representation": "storage",
                "value": storage_xml,
            },
            "version": {"number": int(version_num) + 1},
        }
        return self._request("PUT", f"/wiki/api/v2/pages/{page_id}", json=body)

    # ---- attachments ---------------------------------------------------

    def upload_attachment(self, page_id: str, path: Path) -> dict[str, Any]:
        """Attach a file to a page. Returns the attachment payload
        (we mostly care that the upload succeeded; the storage XML
        references the file by its filename, not its id).

        Uses the v1 REST surface because v2's attachment endpoint
        doesn't accept multipart uploads (read-only).
        """
        path = Path(path)
        if not path.is_file():
            raise ConfluenceAPIError(0, f"attachment not found: {path}", "")
        with path.open("rb") as fh:
            files = {"file": (path.name, fh, _guess_mime(path))}
            return self._request(
                "POST",
                f"/wiki/rest/api/content/{page_id}/child/attachment",
                files=files,
                # Allow re-attaching if the user re-runs the export
                # against the same parent + same image filename.
                data={"minorEdit": "true"},
            )

    # ---- URL helper ----------------------------------------------------

    def page_url(self, page_payload: dict[str, Any]) -> str:
        """Best-effort 'open in browser' URL from a v2 create-page response.

        v2 returns ``_links.webui`` as a path; combine with the
        ``_links.base`` (when present) or our configured base URL to
        produce a full URL. Falls back to the create-page id if no
        webui link is included.
        """
        links = page_payload.get("_links") or {}
        webui = links.get("webui")
        base = (links.get("base") or "").rstrip("/")
        if webui:
            if webui.startswith("http"):
                return webui
            if base:
                return f"{base}{webui}"
            return f"{self._base}{webui}"
        # Fall back to a deterministic page URL.
        page_id = page_payload.get("id")
        if page_id:
            return f"{self._base}/spaces/_/pages/{page_id}"
        return self._base


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
