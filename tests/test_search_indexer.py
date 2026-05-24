"""search_indexer helpers -- session-dir walking + reindex wrapper."""
from __future__ import annotations

import pytest

from meeting_notetaker.models.search_index import SearchIndex, escape_fts5_query
from meeting_notetaker.utils.search_indexer import (
    list_session_dirs,
    rebuild_all,
    reindex_session,
    session_file_paths,
    stale_session_ids,
)


@pytest.fixture
def index(tmp_path):
    db = tmp_path / "search.db"
    index = SearchIndex(db)
    yield index
    index.close()


def _seed_session(session_id: str, *,
                  transcript: str = "", live_notes: str = "",
                  notes: str = "",
                  archives: dict[str, str] | None = None) -> None:
    """Create the on-disk session layout the indexer expects via the
    isolated_data_dir conftest fixture."""
    from meeting_notetaker.utils.paths import session_dir
    sdir = session_dir(session_id)
    if transcript:
        (sdir / "raw.transcript.md").write_text(transcript, encoding="utf-8")
    if live_notes:
        (sdir / "live_notes.md").write_text(live_notes, encoding="utf-8")
    if notes:
        (sdir / "notes.md").write_text(notes, encoding="utf-8")
    for name, body in (archives or {}).items():
        (sdir / name).write_text(body, encoding="utf-8")


def test_session_file_paths_returns_all_four_kinds():
    _seed_session(
        "s1",
        transcript="t", live_notes="ln", notes="n",
        archives={
            "notes-20260601-1200.md": "a1",
            "notes-20260602-0900.md": "a2",
        },
    )
    paths = session_file_paths("s1")
    assert paths["transcript_path"].name == "raw.transcript.md"
    assert paths["live_notes_path"].name == "live_notes.md"
    assert paths["notes_path"].name == "notes.md"
    archives = list(paths["notes_archive_paths"])
    assert [p.name for p in archives] == [
        "notes-20260601-1200.md",
        "notes-20260602-0900.md",
    ]


def test_session_file_paths_handles_missing_files():
    _seed_session("empty_s")  # creates directory but no files
    paths = session_file_paths("empty_s")
    # Files don't exist yet but paths are still returned -- the index
    # short-circuits on .exists() per-file.
    assert not paths["transcript_path"].exists()
    assert paths["notes_archive_paths"] == []


def test_reindex_session_writes_then_no_op_until_change(index):
    _seed_session("s1", transcript="alpha beta")
    assert reindex_session(index, "s1") is True
    assert reindex_session(index, "s1") is False
    # An edit reactivates it.
    from meeting_notetaker.utils.paths import session_dir
    (session_dir("s1") / "raw.transcript.md").write_text(
        "alpha gamma", encoding="utf-8",
    )
    assert reindex_session(index, "s1") is True


def test_reindex_session_force_runs_even_when_clean(index):
    _seed_session("s1", transcript="alpha")
    reindex_session(index, "s1")
    assert reindex_session(index, "s1", force=True) is True


def test_stale_session_ids_lists_pending(index):
    _seed_session("a", transcript="a-body")
    _seed_session("b", transcript="b-body")
    assert sorted(stale_session_ids(index, ["a", "b"])) == ["a", "b"]
    reindex_session(index, "a")
    assert stale_session_ids(index, ["a", "b"]) == ["b"]


def test_rebuild_all_clears_then_reindexes(index):
    _seed_session("a", transcript="apple")
    _seed_session("b", transcript="banana")
    # First pass: index from cold.
    done = rebuild_all(index, ["a", "b"])
    assert done == 2
    assert len(index.search(escape_fts5_query("apple"))) == 1
    # Delete b's transcript file, then rebuild_all -- the deletion
    # has to show up as `b` no longer matching its prior word.
    from meeting_notetaker.utils.paths import session_dir
    (session_dir("b") / "raw.transcript.md").unlink()
    done2 = rebuild_all(index, ["a", "b"])
    # Both sessions get walked + their bookkeeping rows refreshed
    # (force=True), so the operation-count stays at 2.
    assert done2 == 2
    # But `b` has no remaining searchable content -- the banana row
    # was dropped during the clear and never re-inserted.
    assert len(index.search(escape_fts5_query("banana"))) == 0
    assert len(index.search(escape_fts5_query("apple"))) == 1


def test_rebuild_all_progress_callback_fires_per_session(index):
    _seed_session("a", transcript="x")
    _seed_session("b", transcript="y")
    calls: list[tuple[int, int]] = []
    rebuild_all(index, ["a", "b"], progress=lambda d, t: calls.append((d, t)))
    assert calls == [(1, 2), (2, 2)]


def test_list_session_dirs_returns_session_ids_on_disk():
    _seed_session("aaa", transcript="x")
    _seed_session("bbb", notes="y")
    ids = list_session_dirs()
    assert "aaa" in ids
    assert "bbb" in ids
