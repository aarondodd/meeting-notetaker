"""Plumbing between TranscriptStore disk layout + SearchIndex.

Two responsibilities:

* `session_file_paths(session_id)` -- enumerate the four content file
  paths the search index cares about, regardless of which actually
  exist. Used by both the per-session re-index call and the
  startup-time stale scan.
* `reindex_session(index, session_id)` -- glue that calls
  `SearchIndex.index_session` with those paths. Cheap to call after
  every save_*; only writes if `needs_reindex` flags drift.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional

from ..models.search_index import SearchIndex
from .paths import session_dir, sessions_dir


log = logging.getLogger(__name__)


def session_file_paths(session_id: str) -> dict[str, object]:
    """Return the file paths SearchIndex.index_session expects for
    one session.

    Keys are the kwarg names the index uses. Files that don't exist
    on disk are still returned as Path objects (the index checks
    existence itself). The notes-archive set is a list of every
    notes-*.md file currently in the session dir, sorted by name.
    """
    sdir: Path = session_dir(session_id)
    archives = sorted(
        sdir.glob("notes-*.md"),
        key=lambda p: p.name,
    )
    return {
        "transcript_path": sdir / "raw.transcript.md",
        "live_notes_path": sdir / "live_notes.md",
        "notes_path": sdir / "notes.md",
        "notes_archive_paths": archives,
    }


def reindex_session(
    index: SearchIndex,
    session_id: str,
    *,
    force: bool = False,
) -> bool:
    """Re-index a session if its on-disk fingerprint has drifted.

    Returns True when reindex actually ran, False when the index
    was already current. `force=True` skips the fingerprint check
    -- useful for the Rebuild Search Index action and for tests.
    """
    paths = session_file_paths(session_id)
    if not force and not index.needs_reindex(session_id, **paths):
        return False
    try:
        index.index_session(session_id, **paths)
    except Exception:
        log.exception("reindex_session: failed for %s", session_id)
        return False
    return True


def rebuild_all(
    index: SearchIndex,
    session_ids: Iterable[str],
    *,
    progress: Optional[callable] = None,
) -> int:
    """Clear the index and re-index every supplied session.

    `progress(done, total)` fires after each session so the Rebuild
    Search Index dialog can show a percentage. Returns the number of
    sessions successfully (re-)indexed.
    """
    ids = list(session_ids)
    index.clear()
    done = 0
    for i, sid in enumerate(ids, 1):
        if reindex_session(index, sid, force=True):
            done += 1
        if progress is not None:
            try:
                progress(i, len(ids))
            except Exception:
                log.exception("rebuild_all: progress callback raised")
    return done


def stale_session_ids(
    index: SearchIndex,
    session_ids: Iterable[str],
) -> list[str]:
    """Return the subset of supplied IDs whose on-disk content has
    drifted from the index (or has never been indexed)."""
    ids = list(session_ids)
    stale: list[str] = []
    for sid in ids:
        paths = session_file_paths(sid)
        if index.needs_reindex(sid, **paths):
            stale.append(sid)
    return stale


def list_session_dirs() -> list[str]:
    """Every session id with a folder on disk.

    Used by the startup scan to spot orphans -- a session_id that has
    files but no DB row (or vice versa) gets indexed/cleared accordingly.
    Returns plain IDs; caller decides what to do with them.
    """
    root = sessions_dir()
    if not root.is_dir():
        return []
    return [p.name for p in root.iterdir() if p.is_dir()]
