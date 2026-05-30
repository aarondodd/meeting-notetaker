"""Link extractor for the Appendix tray (#64).

Scans notes + live_notes for markdown links and bare URLs, dedupes
by canonical URL, preserves first-seen order. First-seen wins for
label + source attribution.
"""
from __future__ import annotations

from meeting_notetaker.utils.link_extractor import (
    ExtractedLink,
    extract_links,
    extract_links_from_buffer,
    merge_extracted_links,
)


def test_extracts_markdown_link_with_label():
    """[label](url) emits one entry; label is preserved."""
    text = "See [auth design](https://wiki/auth) for context."
    links = extract_links_from_buffer(text, "notes")
    assert links == [
        ExtractedLink(
            url="https://wiki/auth",
            label="auth design",
            source="notes",
        ),
    ]


def test_extracts_bare_url():
    text = "Reference: https://github.com/foo/bar (see comments)."
    links = extract_links_from_buffer(text, "notes")
    assert links == [
        ExtractedLink(
            url="https://github.com/foo/bar",
            label="https://github.com/foo/bar",
            source="notes",
        ),
    ]


def test_skips_url_inside_markdown_link_parens():
    """When a URL appears as part of [label](url), the bare-URL
    pass must not also extract it (or we'd surface it twice)."""
    text = "[label](https://example.com)"
    links = extract_links_from_buffer(text, "notes")
    assert len(links) == 1
    assert links[0].label == "label"


def test_dedupes_same_url_across_buffers():
    """When the same URL appears in both notes and live_notes,
    the notes-source entry wins (#64's choice -- synthesis is
    where the user curates)."""
    links = extract_links(
        notes_text="Wiki: [auth](https://wiki/auth)",
        live_notes_text="auth doc at https://wiki/auth",
    )
    assert len(links) == 1
    assert links[0].source == "notes"
    assert links[0].label == "auth"


def test_dedup_canonical_compares_host_case_insensitive():
    """https://Example.com/foo and https://EXAMPLE.COM/foo are the
    same URL; only one entry surfaces."""
    text = (
        "First: https://Example.com/foo\n"
        "Second: https://EXAMPLE.COM/foo\n"
    )
    links = extract_links_from_buffer(text, "notes")
    assert len(links) == 1


def test_trailing_punctuation_stripped_from_bare_url():
    """Sentence-end URLs lose their period / comma / semicolon."""
    text = "See https://wiki/auth. Also https://github.com/foo;"
    links = extract_links_from_buffer(text, "notes")
    urls = [link.url for link in links]
    assert "https://wiki/auth" in urls
    assert "https://github.com/foo" in urls


def test_no_links_returns_empty():
    assert extract_links_from_buffer("just prose, no urls", "notes") == []
    assert extract_links_from_buffer("", "notes") == []


def test_extract_links_concatenates_in_order():
    """Notes-source entries come before live_notes-source entries."""
    links = extract_links(
        notes_text="A: https://a.example",
        live_notes_text="B: https://b.example",
    )
    urls = [link.url for link in links]
    assert urls.index("https://a.example") < urls.index("https://b.example")


def test_merge_handles_empty_iterables():
    assert merge_extracted_links() == []
    assert merge_extracted_links([], []) == []


def test_image_markdown_does_not_surface_as_link():
    """![alt](images/foo.png) is local image markdown, not a URL
    we want in the tray."""
    text = "![diagram](images/auth.png)"
    links = extract_links_from_buffer(text, "notes")
    assert links == []


def test_http_url_extracted_too():
    """http://... not just https://..."""
    text = "Plaintext: http://insecure.example/path"
    links = extract_links_from_buffer(text, "notes")
    assert len(links) == 1
    assert links[0].url == "http://insecure.example/path"
