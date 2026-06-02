"""Export orchestrator for the Notion + Confluence destinations (#79).

Combines:

  - image scan (collect every local image ref in the Markdown body)
  - image upload (Notion: file_uploads; Confluence: attachments)
  - Markdown -> target format conversion with an image resolver
  - page create / update
  - returns the new page's URL so the caller can open it in a browser

Pure-Python so this can be exercised offline with a stub client. The
QThread + UI wiring lives in the SessionView export wire-up commit.

Image strategy:
  - Notion: upload first (file_uploads is page-independent), then
    create page referencing the upload ids inline.
  - Confluence: attachments require a page id, so we create a
    skeleton page, upload, then update with the final storage XML.

Behavior on duplicate title (Aaron's answer #3): we always create a
new sibling under the picked parent; the caller surfaces a toast so
the user knows previous exports were not replaced.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import mistune

from .confluence_api import ConfluenceClient
from .confluence_storage import markdown_to_storage
from .notion_api import NotionClient
from .notion_blocks import markdown_to_blocks


# ---- public types ---------------------------------------------------------


@dataclass
class ExportResult:
    """Returned by the export functions for caller consumption."""
    target: str  # "notion" | "confluence"
    page_id: str
    page_url: str
    title: str
    # Pre-rendered "what happened" message for the toast / status bar.
    message: str


# ---- image scanning -------------------------------------------------------


_PARSER_OPTIONS = {
    "renderer": "ast",
    "plugins": ["strikethrough", "table", "task_lists", "url"],
}


def collect_image_refs(markdown_text: str) -> list[tuple[str, str]]:
    """Return every ``![alt](url)`` in ``markdown_text`` as (url, alt).

    Order preserved so subsequent upload + resolver maps line up
    deterministically. Both block-level images and inline images
    (rare) are surfaced.
    """
    out: list[tuple[str, str]] = []

    def walk(nodes):
        for node in nodes or []:
            if node.get("type") == "image":
                url = (node.get("attrs") or {}).get("url", "")
                alt_parts = []
                for c in node.get("children", []) or []:
                    if c.get("type") == "text":
                        alt_parts.append(c.get("raw") or "")
                alt = "".join(alt_parts)
                out.append((url, alt))
            if "children" in node:
                walk(node["children"])

    parser = mistune.create_markdown(**_PARSER_OPTIONS)
    walk(parser(markdown_text or ""))
    return out


def is_local_image_ref(url: str) -> bool:
    """True if ``url`` should be treated as a local file path.

    Local: relative paths, file:// URLs, paths without a scheme.
    Remote: http / https / data URIs (data: stays remote -- Notion +
    Confluence both accept inline data, but for v1 we leave them
    alone rather than re-uploading).
    """
    if not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme in ("http", "https", "data"):
        return False
    return True


def resolve_local_image_path(url: str, *, base_dir: Path) -> Optional[Path]:
    """Resolve a Markdown image url against ``base_dir`` (the session dir).

    Returns None when the file isn't found. ``file://`` URLs are
    decoded to absolute paths; bare relative URLs join under base_dir.
    """
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme == "file":
        candidate = Path(parsed.path)
    else:
        candidate = (base_dir / url).resolve()
    if candidate.is_file():
        return candidate
    return None


# ---- Notion export --------------------------------------------------------


def export_to_notion(
    *,
    client: NotionClient,
    parent_id: str,
    title: str,
    markdown_body: str,
    session_dir: Path,
    progress: Optional[Callable[[str], None]] = None,
) -> ExportResult:
    """Convert + upload + create. Returns ExportResult with the new
    page's URL for the caller to surface (toast + open-in-browser)."""

    def report(msg: str) -> None:
        if progress:
            progress(msg)

    # 1. Scan for local image refs, upload each, build a url->file_upload_id map.
    local_uploads: dict[str, str] = {}
    refs = collect_image_refs(markdown_body)
    if refs:
        report(f"Uploading {sum(1 for u, _ in refs if is_local_image_ref(u))} image(s)...")
    for url, _alt in refs:
        if not is_local_image_ref(url) or url in local_uploads:
            continue
        path = resolve_local_image_path(url, base_dir=session_dir)
        if path is None:
            continue  # leave the markdown ref alone; converter emits it as external
        upload_id = client.upload_image(path)
        local_uploads[url] = upload_id

    def image_resolver(url: str, alt: str) -> dict[str, Any]:
        if url in local_uploads:
            return {
                "type": "file_upload",
                "file_upload": {"id": local_uploads[url]},
            }
        if is_local_image_ref(url):
            # Couldn't upload (file missing). Surface a caption-only
            # block so the export at least notes the omission.
            return {
                "type": "external",
                "external": {"url": ""},
                "caption": [{
                    "type": "text",
                    "text": {"content": f"(image missing: {alt or url})"},
                    "plain_text": f"(image missing: {alt or url})",
                }],
            }
        return {"type": "external", "external": {"url": url}}

    # 2. Convert + create.
    report("Converting Markdown to Notion blocks...")
    children = markdown_to_blocks(markdown_body, image_resolver=image_resolver)
    report("Creating page in Notion...")
    page = client.create_page(parent_id=parent_id, title=title, children=children)

    page_id = page.get("id", "")
    page_url = page.get("url") or _notion_fallback_url(page_id)
    return ExportResult(
        target="notion",
        page_id=page_id,
        page_url=page_url,
        title=title,
        message=(
            f"Exported to Notion as a new sibling under the selected "
            f"parent. Previous exports of this session were not replaced."
        ),
    )


def _notion_fallback_url(page_id: str) -> str:
    """Approximate URL when the create response doesn't carry one."""
    if not page_id:
        return "https://www.notion.so/"
    return "https://www.notion.so/" + page_id.replace("-", "")


# ---- Confluence export ----------------------------------------------------


def export_to_confluence(
    *,
    client: ConfluenceClient,
    parent_id: str,
    space_id: str,
    title: str,
    markdown_body: str,
    session_dir: Path,
    progress: Optional[Callable[[str], None]] = None,
) -> ExportResult:
    """Two-pass when images are present:
      1. Create a placeholder page so we have a page_id.
      2. Upload images as attachments to that page_id.
      3. UPDATE the page body with the final storage XML referencing
         attachments by filename.

    When the body has no local images, we collapse into a single
    create call.
    """

    def report(msg: str) -> None:
        if progress:
            progress(msg)

    refs = collect_image_refs(markdown_body)
    local_refs = [(u, a) for u, a in refs if is_local_image_ref(u)]

    # No local images -- single-pass create.
    if not local_refs:
        report("Converting Markdown to Confluence storage XML...")
        storage_xml = markdown_to_storage(markdown_body)
        report("Creating page in Confluence...")
        page = client.create_page(
            parent_id=parent_id, space_id=space_id, title=title,
            storage_xml=storage_xml,
        )
        return ExportResult(
            target="confluence",
            page_id=str(page.get("id", "")),
            page_url=client.page_url(page),
            title=title,
            message=(
                "Exported to Confluence as a new sibling under the selected "
                "parent. Previous exports of this session were not replaced."
            ),
        )

    # Two-pass with attachments.
    report("Creating placeholder page in Confluence...")
    placeholder = client.create_page(
        parent_id=parent_id, space_id=space_id, title=title,
        storage_xml="<p>Uploading images...</p>",
    )
    page_id = str(placeholder.get("id", ""))

    # 2. Upload attachments and build the url -> filename map.
    url_to_filename: dict[str, str] = {}
    report(f"Uploading {len(local_refs)} image attachment(s)...")
    for url, _alt in local_refs:
        path = resolve_local_image_path(url, base_dir=session_dir)
        if path is None:
            continue
        client.upload_attachment(page_id, path)
        url_to_filename[url] = path.name

    # 3. Build the final body via a resolver that emits ri:attachment.
    def image_resolver(url: str, alt: str) -> str:
        if url in url_to_filename:
            alt_attr = f' ac:alt="{_xml_attr(alt)}"' if alt else ""
            return (
                f'<ac:image{alt_attr}>'
                f'<ri:attachment ri:filename="{_xml_attr(url_to_filename[url])}" />'
                f'</ac:image>'
            )
        if is_local_image_ref(url):
            return f'<p><em>(image missing: {_xml_text(alt or url)})</em></p>'
        # Remote URL.
        alt_attr = f' ac:alt="{_xml_attr(alt)}"' if alt else ""
        return (
            f'<ac:image{alt_attr}>'
            f'<ri:url ri:value="{_xml_attr(url)}" />'
            f'</ac:image>'
        )

    report("Converting Markdown + attachments to storage XML...")
    storage_xml = markdown_to_storage(markdown_body, image_resolver=image_resolver)

    report("Updating page body in Confluence...")
    client.update_page(page_id=page_id, title=title, storage_xml=storage_xml)
    # Re-fetch the page URL from the updated payload would require an
    # extra GET; the placeholder response usually carries the same
    # links, which suffice for the user-facing "View in Confluence" link.
    return ExportResult(
        target="confluence",
        page_id=page_id,
        page_url=client.page_url(placeholder),
        title=title,
        message=(
            "Exported to Confluence as a new sibling under the selected "
            "parent. Previous exports of this session were not replaced."
        ),
    )


# ---- xml escaping helpers (mirrors confluence_storage but tiny) -----------


def _xml_text(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _xml_attr(s: str) -> str:
    return _xml_text(s).replace('"', "&quot;").replace("'", "&apos;")
