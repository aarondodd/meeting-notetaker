"""Cross-session full-text search index.

A SQLite FTS5 store kept alongside `sessions.db` at
`<app_data>/search.db`. Indexes the four content types that live in
each session directory:

* `raw.transcript.md`  -> source `transcript`
* `live_notes.md`      -> source `live_notes`
* `notes.md`           -> source `notes`
* `notes-YYYYMMDD-HHMM.md` -> source `notes_archive` (one row per
  archived file; `archive_name` carries the filename so the UI can
  show which archive a hit came from)

The index is rebuildable from scratch -- no derivative data lives only
here. If `search.db` is missing or corrupt the app re-creates it on
first use; the only cost is a one-shot indexing pass at startup.

Design notes:

* FTS5 is in CPython stdlib's SQLite as of 3.9+. No extra deps.
* The `session_text` virtual table holds the searchable rows. A
  companion non-virtual `session_index_state` table carries per-
  session bookkeeping (last_indexed_at, file mtimes) so the
  startup-time staleness scan can skip sessions that haven't moved.
* `tokenize='porter unicode61'` enables stemmed English matching
  while still tokenizing on Unicode-aware word boundaries -- good
  for "deciding"/"decision" without exploding the index size.
* Reindex pattern is delete-then-insert keyed on session_id; no
  attempt to do incremental row updates. Sessions are small enough
  that re-indexing one is sub-millisecond.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from ..utils.paths import app_data_dir


SOURCE_TRANSCRIPT = "transcript"
SOURCE_LIVE_NOTES = "live_notes"
SOURCE_NOTES = "notes"
SOURCE_NOTES_ARCHIVE = "notes_archive"
ALL_SOURCES = (
    SOURCE_TRANSCRIPT,
    SOURCE_LIVE_NOTES,
    SOURCE_NOTES,
    SOURCE_NOTES_ARCHIVE,
)


SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS session_text USING fts5(
    session_id UNINDEXED,
    source UNINDEXED,
    archive_name UNINDEXED,
    body,
    tokenize = 'porter unicode61'
);

CREATE TABLE IF NOT EXISTS session_index_state (
    session_id     TEXT PRIMARY KEY,
    indexed_at     TEXT NOT NULL,
    fingerprint    TEXT NOT NULL
);
"""


@dataclass
class SearchHit:
    """One match against the index.

    `snippet` is the FTS5 `snippet()` output -- the matching phrase is
    wrapped in `<b>...</b>` markers (configurable via the SNIPPET_*
    constants) so the UI can render highlighted context without
    re-running the matcher.

    `rank` is FTS5's `bm25()` score, lower = more relevant. Callers
    that present results by recency rather than relevance can ignore
    it.
    """
    session_id: str
    source: str
    archive_name: Optional[str]
    snippet: str
    rank: float


# Snippet rendering parameters -- centralized so the dialog can reuse
# them when generating its own marker-replacement (the UI strips the
# HTML and re-marks the match for QLabel which doesn't accept <b>
# from arbitrary user content).
SNIPPET_START_MARKER = ""   # ASCII STX -- unlikely in transcripts
SNIPPET_END_MARKER = ""     # ASCII ETX
SNIPPET_ELLIPSIS = "..."
SNIPPET_TOKEN_RADIUS = 12          # tokens of context either side of the match


def db_path() -> Path:
    """Default on-disk location for the search index.

    Co-resident with sessions.db / config.toml / speakers.db. Tests
    that set MEETING_NOTETAKER_DATA_DIR get isolated copies for free
    via app_data_dir().
    """
    return app_data_dir() / "search.db"


class SearchIndex:
    """Thin SQLite-FTS5 wrapper. WAL mode + foreign keys off (FTS5
    virtual tables don't participate in foreign keys)."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or db_path()
        self._conn = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SearchIndex":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ---- indexing ----
    def index_session(
        self,
        session_id: str,
        *,
        transcript_path: Optional[Path] = None,
        live_notes_path: Optional[Path] = None,
        notes_path: Optional[Path] = None,
        notes_archive_paths: Iterable[Path] = (),
        now: Optional[datetime] = None,
    ) -> int:
        """Re-index every content type for one session. Returns the
        number of rows written.

        Delete-then-insert: any prior rows for this session_id are
        cleared first, regardless of how many files we receive this
        time. A None / missing file means "the user deleted it" --
        the index reflects that on the next pass.

        `now` is overridable for tests; production callers should let
        it default to UTC now.
        """
        when = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cur = self._conn
        cur.execute("DELETE FROM session_text WHERE session_id=?", (session_id,))
        inserted = 0
        if transcript_path and transcript_path.exists():
            body = _read_text(transcript_path)
            if body:
                cur.execute(
                    "INSERT INTO session_text (session_id, source, archive_name, body)"
                    " VALUES (?, ?, NULL, ?)",
                    (session_id, SOURCE_TRANSCRIPT, body),
                )
                inserted += 1
        if live_notes_path and live_notes_path.exists():
            body = _read_text(live_notes_path)
            if body:
                cur.execute(
                    "INSERT INTO session_text (session_id, source, archive_name, body)"
                    " VALUES (?, ?, NULL, ?)",
                    (session_id, SOURCE_LIVE_NOTES, body),
                )
                inserted += 1
        if notes_path and notes_path.exists():
            body = _read_text(notes_path)
            if body:
                cur.execute(
                    "INSERT INTO session_text (session_id, source, archive_name, body)"
                    " VALUES (?, ?, NULL, ?)",
                    (session_id, SOURCE_NOTES, body),
                )
                inserted += 1
        for archive in notes_archive_paths:
            if not archive or not archive.exists():
                continue
            body = _read_text(archive)
            if not body:
                continue
            cur.execute(
                "INSERT INTO session_text (session_id, source, archive_name, body)"
                " VALUES (?, ?, ?, ?)",
                (session_id, SOURCE_NOTES_ARCHIVE, archive.name, body),
            )
            inserted += 1
        cur.execute(
            "INSERT INTO session_index_state (session_id, indexed_at, fingerprint)"
            " VALUES (?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET"
            " indexed_at=excluded.indexed_at, fingerprint=excluded.fingerprint",
            (
                session_id,
                when,
                _fingerprint(
                    transcript_path, live_notes_path, notes_path, notes_archive_paths,
                ),
            ),
        )
        return inserted

    def remove_session(self, session_id: str) -> None:
        """Drop every row for a session. Used when the session is deleted."""
        self._conn.execute("DELETE FROM session_text WHERE session_id=?", (session_id,))
        self._conn.execute(
            "DELETE FROM session_index_state WHERE session_id=?", (session_id,),
        )

    def needs_reindex(
        self,
        session_id: str,
        *,
        transcript_path: Optional[Path] = None,
        live_notes_path: Optional[Path] = None,
        notes_path: Optional[Path] = None,
        notes_archive_paths: Iterable[Path] = (),
    ) -> bool:
        """True when the on-disk fingerprint differs from the indexed one.

        Fingerprint covers each file's existence + size + mtime. Cheap
        to compute, catches every real edit, and tolerates one file
        being created or deleted between scans.
        """
        archive_list = list(notes_archive_paths)
        current = _fingerprint(
            transcript_path, live_notes_path, notes_path, archive_list,
        )
        row = self._conn.execute(
            "SELECT fingerprint FROM session_index_state WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if row is None:
            return True
        return row["fingerprint"] != current

    def last_indexed_at(self, session_id: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT indexed_at FROM session_index_state WHERE session_id=?",
            (session_id,),
        ).fetchone()
        return row["indexed_at"] if row else None

    def indexed_session_ids(self) -> set[str]:
        rows = self._conn.execute(
            "SELECT session_id FROM session_index_state"
        ).fetchall()
        return {r["session_id"] for r in rows}

    def clear(self) -> None:
        """Wipe the whole index. Help > Debug > Rebuild search index
        calls this before re-indexing every session from scratch."""
        self._conn.execute("DELETE FROM session_text")
        self._conn.execute("DELETE FROM session_index_state")

    # ---- query ----
    def search(
        self,
        query: str,
        *,
        sources: Optional[Iterable[str]] = None,
        limit: int = 200,
    ) -> list[SearchHit]:
        """Run a query against the index.

        `query` is FTS5 MATCH syntax (phrase: `"foo bar"`, prefix:
        `mdm*`, boolean: `informatica OR mdm`). Single words are
        bare; arbitrary user input should be passed through
        `escape_fts5_query` first so a stray quote doesn't blow up
        the parser.

        `sources` filters to a subset of the four content types
        (default: all). Empty list/tuple == none == empty result.

        Returns at most `limit` hits, ordered by bm25 ascending
        (most relevant first).
        """
        if not query.strip():
            return []
        if sources is None:
            source_filter = list(ALL_SOURCES)
        else:
            source_filter = [s for s in sources if s in ALL_SOURCES]
            if not source_filter:
                return []
        placeholders = ",".join("?" * len(source_filter))
        params: list[object] = [
            SNIPPET_START_MARKER,
            SNIPPET_END_MARKER,
            SNIPPET_ELLIPSIS,
            SNIPPET_TOKEN_RADIUS,
            query,
            *source_filter,
            int(limit),
        ]
        rows = self._conn.execute(
            f"""
            SELECT session_id, source, archive_name,
                   snippet(session_text, 3, ?, ?, ?, ?) AS snip,
                   bm25(session_text) AS rank
            FROM session_text
            WHERE session_text MATCH ?
              AND source IN ({placeholders})
            ORDER BY rank
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [
            SearchHit(
                session_id=r["session_id"],
                source=r["source"],
                archive_name=r["archive_name"],
                snippet=r["snip"] or "",
                rank=float(r["rank"]),
            )
            for r in rows
        ]


def _read_text(path: Path) -> str:
    """Safe text read. Returns empty string on any I/O error; the
    indexer treats empty body as "skip this row" so a transient FS
    hiccup doesn't poison the index with a zero-content match."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _file_fingerprint(path: Optional[Path]) -> str:
    """Single-file fingerprint: '<size>:<mtime_ns>' or 'missing'."""
    if path is None or not path.exists():
        return "missing"
    try:
        st = path.stat()
    except OSError:
        return "missing"
    return f"{st.st_size}:{st.st_mtime_ns}"


def _fingerprint(
    transcript_path: Optional[Path],
    live_notes_path: Optional[Path],
    notes_path: Optional[Path],
    notes_archive_paths: Iterable[Path],
) -> str:
    """Aggregate fingerprint across all four content types.

    Archive paths are joined name|fingerprint so a rename or a delete
    is detected (a rename changes the name string, which forces a
    reindex even if the size+mtime are unchanged).
    """
    parts = [
        f"t={_file_fingerprint(transcript_path)}",
        f"l={_file_fingerprint(live_notes_path)}",
        f"n={_file_fingerprint(notes_path)}",
    ]
    archives = sorted(
        (p for p in notes_archive_paths if p is not None),
        key=lambda p: p.name,
    )
    arch_parts = [f"{p.name}={_file_fingerprint(p)}" for p in archives]
    parts.append("a=[" + ",".join(arch_parts) + "]")
    return "|".join(parts)


_FTS5_SPECIAL_CHARS_RE = re.compile(r'[^\w\s*]', re.UNICODE)


def escape_fts5_query(query: str) -> str:
    """Make an arbitrary user-typed string safe to pass as a MATCH expression.

    Strategy: split on whitespace, quote every token that contains
    FTS5-special characters, leave bare alphanumeric tokens
    unquoted. Bare tokens get prefix-matched ("mdm" matches
    "mdmctl", "mdms") which is almost always what the user
    intended. Quoted tokens get phrase-matched ("mdm" exact).

    Empty / whitespace-only input returns an empty string.
    """
    tokens = query.split()
    out: list[str] = []
    for tok in tokens:
        if not tok:
            continue
        # Trailing wildcards are passed through verbatim so a user
        # typing "mdm*" gets explicit prefix match.
        wildcard = tok.endswith("*")
        core = tok[:-1] if wildcard else tok
        if _FTS5_SPECIAL_CHARS_RE.search(core):
            # Embedded quote -> double it for FTS5 string escaping.
            safe = core.replace('"', '""')
            out.append(f'"{safe}"' + ("*" if wildcard else ""))
        else:
            # Bare tokens get a prefix wildcard for an "as the user
            # types" feel (matches "decision" when the user typed
            # "decid"). Two-char tokens get exact match to avoid
            # avalanche.
            if wildcard or len(core) <= 2:
                out.append(core + ("*" if wildcard else ""))
            else:
                out.append(core + "*")
    return " ".join(out)
