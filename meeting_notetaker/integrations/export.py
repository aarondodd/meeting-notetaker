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
from .confluence_storage import build_toc_macro, markdown_to_storage
from .notion_api import NotionClient
from .notion_blocks import build_toc_block as build_notion_toc_block
from .notion_blocks import markdown_to_blocks
from .onenote_com import OneNoteClient, OneNoteError
from .onenote_xml import (
    OneNoteAttachedFile,
    OneNoteImageRef,
    build_page_xml,
)


# ---- attachment payload ----------------------------------------------------


@dataclass
class ExportAttachment:
    """One session attachment to push to the created page."""
    path: Path
    display_name: str = ""
    mime: str = ""

    def label(self) -> str:
        return (self.display_name or self.path.name).strip() or self.path.name


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
    attachments: Optional[list[ExportAttachment]] = None,
    progress: Optional[Callable[[str], None]] = None,
    number_headings: bool = False,
    include_toc: bool = False,
    toc_max_depth: int = 3,
) -> ExportResult:
    """Convert + upload + create. Returns ExportResult with the new
    page's URL for the caller to surface (toast + open-in-browser).

    ``attachments`` are session files to push as ``file`` blocks at
    the end of the page, under a "## Attachments" heading. Uploaded
    via the same file_uploads endpoint as inline images.
    """
    attachments = attachments or []

    def report(msg: str) -> None:
        if progress:
            progress(msg)

    # #92 + #94: apply heading numbering before the block conversion
    # so the numbered headings flow through to Notion. The TOC is
    # NOT injected as a markdown list -- Notion has a native
    # `table_of_contents` block that auto-generates a clickable
    # nested list of headings server-side. Strictly better fit
    # than a markdown list (which Notion renders as static text).
    # Numbering still happens; only the markdown TOC list is skipped.
    if number_headings:
        from ..utils.markdown_outline import apply_outline  # noqa: PLC0415
        markdown_body = apply_outline(
            markdown_body, number=True, toc=False,
        )

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
    # #94: prepend Notion's native table_of_contents block when TOC
    # was requested. Auto-generated server-side from the page's
    # headings; navigable + auto-updates.
    if include_toc:
        children = [build_notion_toc_block()] + children

    # 3. Optional session attachments -- upload each, append file blocks
    #    under an Attachments heading at the bottom of the page so the
    #    saved record is self-contained.
    if attachments:
        report(f"Uploading {len(attachments)} attachment(s)...")
        attachment_blocks: list[dict[str, Any]] = []
        for att in attachments:
            if not att.path.is_file():
                continue
            try:
                upload_id = client.upload_file(att.path, mime=att.mime)
            except Exception:
                # Best effort -- one bad upload doesn't sink the export.
                continue
            attachment_blocks.append({
                "object": "block",
                "type": "file",
                "file": {
                    "type": "file_upload",
                    "file_upload": {"id": upload_id},
                    "caption": [{
                        "type": "text",
                        "text": {"content": att.label()},
                        "plain_text": att.label(),
                    }],
                    "name": att.label(),
                },
            })
        if attachment_blocks:
            children.append({
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [{
                        "type": "text",
                        "text": {"content": "Attachments"},
                        "plain_text": "Attachments",
                    }],
                },
            })
            children.extend(attachment_blocks)

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
    attachments: Optional[list[ExportAttachment]] = None,
    progress: Optional[Callable[[str], None]] = None,
    number_headings: bool = False,
    include_toc: bool = False,
    toc_max_depth: int = 3,
) -> ExportResult:
    """Two-pass when images OR attachments are present:
      1. Create a placeholder page so we have a page_id.
      2. Upload images + session attachments to that page_id via the
         multipart attachment endpoint.
      3. UPDATE the page body with final storage XML, including a
         "## Attachments" section listing the session files.

    When the body has no local images and the caller didn't request
    attachments, we collapse into a single create call for speed.
    """
    attachments = attachments or []

    def report(msg: str) -> None:
        if progress:
            progress(msg)

    # #92 + #94: heading numbering applies before the storage XML
    # conversion so the numbered headings flow through. The TOC is
    # NOT injected as a markdown list -- Confluence has a native
    # `<ac:structured-macro ac:name="toc">` macro that auto-generates
    # a clickable nested list server-side; we prepend it to the
    # storage XML after conversion. Numbering still happens; only
    # the markdown list injection is skipped for Confluence.
    if number_headings:
        from ..utils.markdown_outline import apply_outline  # noqa: PLC0415
        markdown_body = apply_outline(
            markdown_body, number=True, toc=False,
        )

    refs = collect_image_refs(markdown_body)
    local_refs = [(u, a) for u, a in refs if is_local_image_ref(u)]
    has_attachments = bool(attachments)

    # No local images + no attachments -- single-pass create.
    if not local_refs and not has_attachments:
        report("Converting Markdown to Confluence storage XML...")
        storage_xml = markdown_to_storage(markdown_body)
        # #94: native TOC macro instead of a markdown list. Confluence
        # auto-generates a clickable nested TOC from the page's
        # headings server-side. Better fit than the markdown list,
        # which renders as non-clickable bullets.
        if include_toc:
            storage_xml = build_toc_macro(max_level=toc_max_depth) + storage_xml
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
        storage_xml="<p>Uploading images and attachments...</p>",
    )
    page_id = str(placeholder.get("id", ""))

    # 2a. Upload inline image refs and build the url -> filename map.
    url_to_filename: dict[str, str] = {}
    if local_refs:
        report(f"Uploading {len(local_refs)} image(s)...")
    for url, _alt in local_refs:
        path = resolve_local_image_path(url, base_dir=session_dir)
        if path is None:
            continue
        client.upload_attachment(page_id, path)
        url_to_filename[url] = path.name

    # 2b. Upload session attachments and remember their filenames so
    #     we can list them in the storage XML below.
    uploaded_attachments: list[tuple[str, str]] = []  # (filename, display_label)
    if has_attachments:
        report(f"Uploading {len(attachments)} attachment(s)...")
    for att in attachments:
        if not att.path.is_file():
            continue
        try:
            client.upload_attachment(page_id, att.path)
        except Exception:
            # Best effort -- one bad upload doesn't sink the export.
            continue
        uploaded_attachments.append((att.path.name, att.label()))

    # 3. Build the final body via a resolver that emits ri:attachment
    #    for image refs, plus a tail "## Attachments" section listing
    #    each uploaded session file as an ac:link.
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
    if uploaded_attachments:
        storage_xml += _build_confluence_attachments_section(uploaded_attachments)
    # #94: prepend the native TOC macro after image + attachment
    # injection so the TOC appears above the body content.
    if include_toc:
        storage_xml = build_toc_macro(max_level=toc_max_depth) + storage_xml

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


def _build_confluence_attachments_section(
    uploaded: list[tuple[str, str]],
) -> str:
    """Storage-XML "## Attachments" section linking each uploaded file."""
    items: list[str] = []
    for filename, label in uploaded:
        items.append(
            "<li><p>"
            f'<ac:link><ri:attachment ri:filename="{_xml_attr(filename)}" />'
            f"<ac:plain-text-link-body><![CDATA[{label}]]></ac:plain-text-link-body>"
            "</ac:link>"
            "</p></li>"
        )
    return "<h2>Attachments</h2><ul>" + "".join(items) + "</ul>"


# ---- OneNote export -------------------------------------------------------


def export_to_onenote(
    *,
    client: OneNoteClient,
    section_id: str,
    title: str,
    markdown_body: str,
    session_dir: Path,
    attachments: Optional[list[ExportAttachment]] = None,
    progress: Optional[Callable[[str], None]] = None,
    number_headings: bool = False,
    open_after_save: bool = True,
) -> ExportResult:
    """Render the markdown body to OneNote XML + push it to a freshly-
    created page under ``section_id`` via the desktop COM client.

    No TOC: OneNote has no native TOC primitive (issue #100). Heading
    numbering still applies; ``include_toc`` would have no useful
    target here so the param is omitted.
    """
    attachments = attachments or []

    def report(msg: str) -> None:
        if progress:
            progress(msg)

    if number_headings:
        from ..utils.markdown_outline import apply_outline  # noqa: PLC0415
        markdown_body = apply_outline(
            markdown_body, number=True, toc=False,
        )

    # 1. Build an image resolver that returns the file bytes for any
    #    local image; remote images stay in the XML as a hyperlink.
    def image_resolver(url: str, alt: str) -> Optional[OneNoteImageRef]:
        if not is_local_image_ref(url):
            return None
        path = resolve_local_image_path(url, base_dir=session_dir)
        if path is None:
            return None
        fmt = _guess_image_format(path)
        try:
            data = path.read_bytes()
        except OSError:
            return None
        return OneNoteImageRef(url=url, alt=alt, data=data, format=fmt)

    # 2. Resolve session attachments to disk paths the XML can embed.
    attached_files: list[OneNoteAttachedFile] = []
    for att in attachments:
        if not att.path.is_file():
            continue
        attached_files.append(OneNoteAttachedFile(
            path=att.path,
            label=att.label(),
        ))

    # 3. Create the empty page first so we have an id for the XML.
    report("Creating page in OneNote...")
    page_id = client.create_page(section_id=section_id)

    # 4. Generate the XML body + push it.
    report("Rendering page content...")
    page_xml, stats = build_page_xml(
        page_id=page_id, title=title,
        markdown_body=markdown_body,
        image_resolver=image_resolver,
        attached_files=attached_files,
    )

    report("Saving content to OneNote...")
    try:
        client.update_page_content(page_xml=page_xml)
    except OneNoteError as exc:
        raise OneNoteError(
            f"OneNote rejected the page content for section "
            f"{section_id!r}. The page was created but is empty. "
            f"Underlying error: {exc}",
        ) from exc

    # 5. Best-effort link + post-save navigation.
    page_url = client.get_page_url(page_id=page_id) or ""
    if open_after_save:
        report("Opening in OneNote...")
        client.navigate_to(page_id=page_id)

    return ExportResult(
        target="onenote",
        page_id=page_id,
        page_url=page_url,
        title=title,
        message=(
            f"Saved to OneNote: {stats.paragraphs} paragraph(s), "
            f"{stats.headings} heading(s)"
            + (f", {stats.images_embedded} image(s)" if stats.images_embedded else "")
            + (f", {stats.tables} table(s)" if stats.tables else "")
            + (f", {stats.attached_files} attachment(s)" if stats.attached_files else "")
            + "."
        ),
    )


def _guess_image_format(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        return "jpeg"
    if suffix == ".gif":
        return "gif"
    if suffix == ".bmp":
        return "bmp"
    return "png"


# ---- xml escaping helpers (mirrors confluence_storage but tiny) -----------


def _xml_text(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _xml_attr(s: str) -> str:
    return _xml_text(s).replace('"', "&quot;").replace("'", "&apos;")
