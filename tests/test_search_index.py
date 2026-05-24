"""SearchIndex (FTS5) backend tests.

Exercises the indexer + query layer without Qt. The schema lives in
the meeting_notetaker.models.search_index module; reindex helpers
that walk a session directory live in
meeting_notetaker.utils.search_indexer.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from meeting_notetaker.models.search_index import (
    ALL_SOURCES,
    SNIPPET_END_MARKER,
    SNIPPET_START_MARKER,
    SOURCE_LIVE_NOTES,
    SOURCE_NOTES,
    SOURCE_NOTES_ARCHIVE,
    SOURCE_TRANSCRIPT,
    SearchIndex,
    escape_fts5_query,
)


@pytest.fixture
def idx(tmp_path):
    db = tmp_path / "search.db"
    index = SearchIndex(db)
    yield index
    index.close()


def _make_session(tmp_path: Path, sid: str, *,
                  transcript: str = "", live_notes: str = "",
                  notes: str = "", archives: dict[str, str] | None = None,
                  ) -> dict[str, object]:
    sdir = tmp_path / sid
    sdir.mkdir(parents=True, exist_ok=True)
    transcript_path = sdir / "raw.transcript.md"
    live_notes_path = sdir / "live_notes.md"
    notes_path = sdir / "notes.md"
    if transcript:
        transcript_path.write_text(transcript, encoding="utf-8")
    if live_notes:
        live_notes_path.write_text(live_notes, encoding="utf-8")
    if notes:
        notes_path.write_text(notes, encoding="utf-8")
    archive_paths: list[Path] = []
    for name, body in (archives or {}).items():
        p = sdir / name
        p.write_text(body, encoding="utf-8")
        archive_paths.append(p)
    return {
        "transcript_path": transcript_path,
        "live_notes_path": live_notes_path,
        "notes_path": notes_path,
        "notes_archive_paths": archive_paths,
    }


def test_index_session_writes_one_row_per_present_file(idx, tmp_path):
    paths = _make_session(
        tmp_path, "s1",
        transcript="alice discussed mdm rollout",
        notes="decision to proceed with informatica",
    )
    written = idx.index_session("s1", **paths)
    assert written == 2  # transcript + notes; no live_notes, no archives


def test_index_session_skips_missing_files(idx, tmp_path):
    paths = _make_session(tmp_path, "s2", transcript="only transcript here")
    written = idx.index_session("s2", **paths)
    assert written == 1


def test_search_finds_word_in_transcript(idx, tmp_path):
    paths = _make_session(
        tmp_path, "s1",
        transcript="we agreed alice will lead mdm migration",
    )
    idx.index_session("s1", **paths)
    hits = idx.search(escape_fts5_query("mdm"))
    assert len(hits) == 1
    hit = hits[0]
    assert hit.session_id == "s1"
    assert hit.source == SOURCE_TRANSCRIPT
    assert "mdm" in hit.snippet.lower()


def test_search_returns_archived_notes_with_filename(idx, tmp_path):
    paths = _make_session(
        tmp_path, "s1",
        archives={
            "notes-20260501-1430.md": "earlier draft mentioned informatica",
            "notes-20260510-0900.md": "later draft about something else",
        },
    )
    idx.index_session("s1", **paths)
    hits = idx.search(escape_fts5_query("informatica"))
    assert len(hits) == 1
    assert hits[0].source == SOURCE_NOTES_ARCHIVE
    assert hits[0].archive_name == "notes-20260501-1430.md"


def test_search_filters_by_source(idx, tmp_path):
    paths = _make_session(
        tmp_path, "s1",
        transcript="kappa appears in transcript",
        live_notes="kappa appears in live notes",
        notes="kappa appears in synthesized notes",
    )
    idx.index_session("s1", **paths)
    all_hits = idx.search(escape_fts5_query("kappa"))
    assert len(all_hits) == 3
    only_notes = idx.search(escape_fts5_query("kappa"), sources=[SOURCE_NOTES])
    assert len(only_notes) == 1
    assert only_notes[0].source == SOURCE_NOTES


def test_search_empty_sources_returns_empty(idx, tmp_path):
    paths = _make_session(tmp_path, "s1", transcript="something")
    idx.index_session("s1", **paths)
    assert idx.search(escape_fts5_query("something"), sources=[]) == []


def test_search_empty_query_returns_empty(idx, tmp_path):
    paths = _make_session(tmp_path, "s1", transcript="anything")
    idx.index_session("s1", **paths)
    assert idx.search("") == []
    assert idx.search("   ") == []


def test_index_then_remove_session_clears_rows(idx, tmp_path):
    paths = _make_session(tmp_path, "s1", transcript="alpha beta")
    idx.index_session("s1", **paths)
    assert idx.search(escape_fts5_query("alpha"))
    idx.remove_session("s1")
    assert idx.search(escape_fts5_query("alpha")) == []


def test_reindex_replaces_prior_rows(idx, tmp_path):
    """A second index_session call must drop the previous rows so an
    edit that removed content doesn't keep showing up."""
    paths = _make_session(tmp_path, "s1", transcript="word_v1")
    idx.index_session("s1", **paths)
    assert idx.search(escape_fts5_query("word_v1"))
    paths["transcript_path"].write_text("word_v2 only", encoding="utf-8")
    idx.index_session("s1", **paths)
    assert not idx.search(escape_fts5_query("word_v1"))
    assert idx.search(escape_fts5_query("word_v2"))


def test_needs_reindex_after_content_edit(idx, tmp_path):
    paths = _make_session(tmp_path, "s1", transcript="initial body")
    idx.index_session("s1", **paths)
    assert not idx.needs_reindex("s1", **paths)
    # Edit changes mtime+size; fingerprint differs.
    paths["transcript_path"].write_text("longer body content", encoding="utf-8")
    assert idx.needs_reindex("s1", **paths)


def test_needs_reindex_when_archive_added(idx, tmp_path):
    paths = _make_session(tmp_path, "s1", notes="body")
    idx.index_session("s1", **paths)
    assert not idx.needs_reindex("s1", **paths)
    # Adding an archive file must trigger a reindex.
    new_archive = tmp_path / "s1" / "notes-20260601-1200.md"
    new_archive.write_text("prior body", encoding="utf-8")
    paths["notes_archive_paths"] = [new_archive]
    assert idx.needs_reindex("s1", **paths)


def test_needs_reindex_when_archive_renamed(idx, tmp_path):
    paths = _make_session(
        tmp_path, "s1",
        archives={"notes-20260601-1200.md": "body"},
    )
    idx.index_session("s1", **paths)
    assert not idx.needs_reindex("s1", **paths)
    # Same content, different name -> fingerprint includes filename.
    archive = paths["notes_archive_paths"][0]
    renamed = archive.with_name("notes-20260602-1200.md")
    archive.rename(renamed)
    paths["notes_archive_paths"] = [renamed]
    assert idx.needs_reindex("s1", **paths)


def test_needs_reindex_never_indexed(idx, tmp_path):
    paths = _make_session(tmp_path, "s_never", transcript="x")
    assert idx.needs_reindex("s_never", **paths)


def test_indexed_session_ids_returns_bookkeeping_set(idx, tmp_path):
    paths_a = _make_session(tmp_path, "a", transcript="alpha")
    paths_b = _make_session(tmp_path, "b", transcript="beta")
    idx.index_session("a", **paths_a)
    idx.index_session("b", **paths_b)
    assert idx.indexed_session_ids() == {"a", "b"}


def test_clear_wipes_index(idx, tmp_path):
    paths = _make_session(tmp_path, "s1", transcript="data")
    idx.index_session("s1", **paths)
    idx.clear()
    assert idx.search(escape_fts5_query("data")) == []
    assert idx.indexed_session_ids() == set()


def test_last_indexed_at_returns_iso_string(idx, tmp_path):
    paths = _make_session(tmp_path, "s1", transcript="something")
    idx.index_session("s1", **paths)
    when = idx.last_indexed_at("s1")
    assert when and when.endswith("Z")
    assert idx.last_indexed_at("never_indexed") is None


def test_snippet_contains_marker_pair(idx, tmp_path):
    """FTS5 snippet must wrap the match with the configured markers
    so the UI dialog can re-render with bold."""
    paths = _make_session(
        tmp_path, "s1",
        transcript="we agreed alice will lead mdm migration",
    )
    idx.index_session("s1", **paths)
    hits = idx.search(escape_fts5_query("mdm"))
    assert hits
    assert SNIPPET_START_MARKER in hits[0].snippet
    assert SNIPPET_END_MARKER in hits[0].snippet


def test_search_ranked_relevance(idx, tmp_path):
    """A doc with the match in the title-ish leading text outranks
    a doc where the match only shows up once in long text."""
    paths_a = _make_session(
        tmp_path, "a",
        transcript="mdm mdm mdm rollout",
    )
    paths_b = _make_session(
        tmp_path, "b",
        transcript="some unrelated mostly content with one mdm reference",
    )
    idx.index_session("a", **paths_a)
    idx.index_session("b", **paths_b)
    hits = idx.search(escape_fts5_query("mdm"))
    assert [h.session_id for h in hits[:2]] == ["a", "b"]


def test_all_sources_constant_complete():
    assert set(ALL_SOURCES) == {
        SOURCE_TRANSCRIPT, SOURCE_LIVE_NOTES,
        SOURCE_NOTES, SOURCE_NOTES_ARCHIVE,
    }


# escape_fts5_query


def test_escape_passes_plain_token_as_prefix():
    assert escape_fts5_query("mdm") == "mdm*"


def test_escape_short_token_stays_exact():
    """Two-letter tokens don't get auto-prefixed -- otherwise typing
    'is' would match everything."""
    assert escape_fts5_query("is") == "is"


def test_escape_quoted_phrase_via_special_chars():
    """A token with a hyphen is FTS5-special -- escape by quoting."""
    out = escape_fts5_query("non-blocking")
    assert out.startswith('"non-blocking"')


def test_escape_multi_word():
    out = escape_fts5_query("informatica migration")
    assert "informatica*" in out
    assert "migration*" in out


def test_escape_explicit_wildcard_preserved():
    assert escape_fts5_query("mdm*") == "mdm*"


def test_escape_empty_returns_empty():
    assert escape_fts5_query("") == ""
    assert escape_fts5_query("   ") == ""


def test_escape_embedded_quote_safely_doubled():
    out = escape_fts5_query('foo"bar')
    assert '""' in out
