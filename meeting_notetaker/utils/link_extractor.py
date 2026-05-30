"""Extract URLs from session markdown buffers for the Appendix tray (#64).

Scans both notes.md (synthesis) and live_notes.md (user buffer) for
two URL forms:

* Markdown-style links: ``[label](https://example.com)`` -- the
  label is preserved so the tray can show "Wiki: auth design" vs.
  a raw URL.
* Bare URLs: ``https://...`` / ``http://...`` outside of any
  markdown link wrapper.

Dedup is by canonical URL (case-insensitive scheme + host, exact
path). First occurrence wins for the label. Ordering preserves
first-seen across the concatenated buffers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class ExtractedLink:
    """One link surfaced in the Appendix tray.

    `label` is what the user saw in the editor:
    * markdown link -> the link's display text
    * bare URL      -> the URL itself
    """
    url: str
    label: str
    source: str  # "notes" or "live_notes"


# Markdown link with explicit label. Non-greedy on the label so
# adjacent ![alt](path) image refs don't blow it up.
_MD_LINK_RE = re.compile(r"\[([^\]\n]+?)\]\((https?://[^\s)]+)\)")

# Bare URL not wrapped in a markdown link. Matches http(s) only --
# the tray's audience is meeting attendees pasting work URLs, not
# mailto / ftp / data URIs.
_BARE_URL_RE = re.compile(
    r"(?<![\(\[])\bhttps?://[^\s<>\)\]\"'`]+",
    re.IGNORECASE,
)


def _canonical(url: str) -> str:
    """Case-fold scheme + host for dedup. Preserves the path."""
    m = re.match(r"^(https?)://([^/\s]+)(.*)$", url, re.IGNORECASE)
    if m is None:
        return url
    scheme, host, rest = m.groups()
    return f"{scheme.lower()}://{host.lower()}{rest}"


def extract_links_from_buffer(text: str, source: str) -> list[ExtractedLink]:
    """Return ExtractedLink entries (in first-seen order) found in
    ``text``. Caller is expected to dedup across buffers via
    ``merge_extracted_links``."""
    if not text:
        return []
    out: list[ExtractedLink] = []
    seen_urls: set[str] = set()
    # Track [label](url) URL spans so the bare-URL pass can skip
    # them; otherwise we'd surface the same URL twice (once from
    # the markdown link, once from the bare-URL inside the
    # parentheses).
    md_spans: list[tuple[int, int]] = []
    for m in _MD_LINK_RE.finditer(text):
        url = m.group(2).rstrip(".,;:)")
        key = _canonical(url)
        md_spans.append((m.start(2), m.end(2)))
        if key in seen_urls:
            continue
        seen_urls.add(key)
        out.append(ExtractedLink(
            url=url,
            label=(m.group(1) or "").strip() or url,
            source=source,
        ))
    for m in _BARE_URL_RE.finditer(text):
        # Drop trailing punctuation that's typical of sentence-end
        # URL captures (e.g. "see https://foo.com.").
        url = m.group(0).rstrip(".,;:!?)\"'")
        # Skip URLs that fall inside a markdown link's (url) span.
        if any(s <= m.start() < e for s, e in md_spans):
            continue
        key = _canonical(url)
        if key in seen_urls:
            continue
        seen_urls.add(key)
        out.append(ExtractedLink(
            url=url,
            label=url,
            source=source,
        ))
    return out


def merge_extracted_links(*sources: Iterable[ExtractedLink]) -> list[ExtractedLink]:
    """Concatenate ExtractedLink streams, deduping by canonical URL.

    First occurrence wins for label + source attribution. Ordering
    follows the order of arguments and then first-seen within each
    argument.
    """
    seen: set[str] = set()
    out: list[ExtractedLink] = []
    for batch in sources:
        for link in batch:
            key = _canonical(link.url)
            if key in seen:
                continue
            seen.add(key)
            out.append(link)
    return out


def extract_links(
    *,
    notes_text: str = "",
    live_notes_text: str = "",
) -> list[ExtractedLink]:
    """Convenience: scan both buffers and merge the result.

    Notes-source links rank ahead of live-notes-source links when
    the same URL appears in both, since the synthesis is the
    polished output where the user is most likely curating links."""
    return merge_extracted_links(
        extract_links_from_buffer(notes_text, "notes"),
        extract_links_from_buffer(live_notes_text, "live_notes"),
    )
