"""Session + Folder persistence (SQLite + WAL).

Session lifecycle states:
    new        -- created but not yet started
    recording  -- actively capturing audio
    paused     -- capture suspended; can resume
    processing -- capture stopped; transcription in progress (final pass)
    complete   -- finished, transcript saved
    error      -- recording aborted; partial transcript may exist
"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..utils.paths import db_path


STATE_NEW = "new"
STATE_RECORDING = "recording"
STATE_PAUSED = "paused"
STATE_PROCESSING = "processing"
STATE_COMPLETE = "complete"
STATE_ERROR = "error"

ALL_STATES = (
    STATE_NEW,
    STATE_RECORDING,
    STATE_PAUSED,
    STATE_PROCESSING,
    STATE_COMPLETE,
    STATE_ERROR,
)

TRANSIENT_STATES = (STATE_RECORDING, STATE_PAUSED, STATE_PROCESSING)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Folder:
    id: str
    name: str
    parent_id: Optional[str] = None
    created_at: str = field(default_factory=utc_now_iso)
    sort_order: int = 0


@dataclass
class Session:
    id: str
    title: str
    folder_id: Optional[str] = None
    created_at: str = field(default_factory=utc_now_iso)
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    duration_seconds: Optional[int] = None
    state: str = STATE_NEW
    model_used: Optional[str] = None
    retain_audio: bool = False
    has_audio: bool = False
    has_transcript: bool = False
    has_notes: bool = False


SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    parent_id   TEXT REFERENCES folders(id),
    created_at  TEXT NOT NULL,
    sort_order  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    id                TEXT PRIMARY KEY,
    title             TEXT NOT NULL,
    folder_id         TEXT REFERENCES folders(id),
    created_at        TEXT NOT NULL,
    started_at        TEXT,
    ended_at          TEXT,
    duration_seconds  INTEGER,
    state             TEXT NOT NULL,
    model_used        TEXT,
    retain_audio      INTEGER DEFAULT 0,
    has_audio         INTEGER DEFAULT 0,
    has_transcript    INTEGER DEFAULT 0,
    has_notes         INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sessions_folder  ON sessions(folder_id);
CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_at DESC);
"""

_ANY = object()


class SessionStore:
    """Thin SQLite wrapper with WAL mode + foreign keys."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or db_path()
        self._conn = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SessionStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ---- folders ----
    def create_folder(self, name: str, parent_id: Optional[str] = None, sort_order: int = 0) -> Folder:
        folder = Folder(id=str(uuid.uuid4()), name=name, parent_id=parent_id, sort_order=sort_order)
        self._conn.execute(
            "INSERT INTO folders (id, name, parent_id, created_at, sort_order) VALUES (?, ?, ?, ?, ?)",
            (folder.id, folder.name, folder.parent_id, folder.created_at, folder.sort_order),
        )
        return folder

    def rename_folder(self, folder_id: str, new_name: str) -> None:
        self._conn.execute("UPDATE folders SET name=? WHERE id=?", (new_name, folder_id))

    def delete_folder(self, folder_id: str) -> None:
        self._conn.execute("UPDATE sessions SET folder_id=NULL WHERE folder_id=?", (folder_id,))
        self._conn.execute("DELETE FROM folders WHERE id=?", (folder_id,))

    def list_folders(self) -> list[Folder]:
        rows = self._conn.execute("SELECT * FROM folders ORDER BY sort_order, name").fetchall()
        return [Folder(**dict(r)) for r in rows]

    # ---- sessions ----
    def create_session(
        self,
        title: str,
        folder_id: Optional[str] = None,
        retain_audio: bool = False,
    ) -> Session:
        session = Session(
            id=str(uuid.uuid4()),
            title=title,
            folder_id=folder_id,
            retain_audio=retain_audio,
        )
        self._conn.execute(
            """
            INSERT INTO sessions (id, title, folder_id, created_at, state, retain_audio)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session.id,
                session.title,
                session.folder_id,
                session.created_at,
                session.state,
                int(session.retain_audio),
            ),
        )
        return session

    def get_session(self, session_id: str) -> Optional[Session]:
        row = self._conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return _row_to_session(row) if row else None

    def list_sessions(self, folder_id: object = _ANY) -> list[Session]:
        """List sessions. folder_id=_ANY (default) returns all; None returns unfiled only;
        a string returns that folder's sessions."""
        if folder_id is _ANY:
            rows = self._conn.execute("SELECT * FROM sessions ORDER BY created_at DESC").fetchall()
        elif folder_id is None:
            rows = self._conn.execute(
                "SELECT * FROM sessions WHERE folder_id IS NULL ORDER BY created_at DESC"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM sessions WHERE folder_id=? ORDER BY created_at DESC", (folder_id,)
            ).fetchall()
        return [_row_to_session(r) for r in rows]

    def update_session(self, session_id: str, **fields) -> None:
        if not fields:
            return
        allowed = {
            "title",
            "folder_id",
            "started_at",
            "ended_at",
            "duration_seconds",
            "state",
            "model_used",
            "retain_audio",
            "has_audio",
            "has_transcript",
            "has_notes",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown session fields: {sorted(unknown)}")
        sets = ", ".join(f"{k}=?" for k in fields)
        values: list = []
        for v in fields.values():
            if isinstance(v, bool):
                v = int(v)
            values.append(v)
        values.append(session_id)
        self._conn.execute(f"UPDATE sessions SET {sets} WHERE id=?", values)

    def delete_session(self, session_id: str) -> None:
        self._conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))

    def delete_sessions(self, session_ids: list[str]) -> int:
        """Bulk-delete. Returns the number of rows removed."""
        if not session_ids:
            return 0
        marks = ",".join("?" * len(session_ids))
        cur = self._conn.execute(f"DELETE FROM sessions WHERE id IN ({marks})", session_ids)
        return cur.rowcount or 0

    def find_orphans(self) -> list[Session]:
        """Sessions stuck in transient states -- crash-recovery candidates."""
        marks = ",".join("?" * len(TRANSIENT_STATES))
        rows = self._conn.execute(
            f"SELECT * FROM sessions WHERE state IN ({marks})",
            TRANSIENT_STATES,
        ).fetchall()
        return [_row_to_session(r) for r in rows]


def _row_to_session(row: sqlite3.Row) -> Session:
    return Session(
        id=row["id"],
        title=row["title"],
        folder_id=row["folder_id"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        duration_seconds=row["duration_seconds"],
        state=row["state"],
        model_used=row["model_used"],
        retain_audio=bool(row["retain_audio"]),
        has_audio=bool(row["has_audio"]),
        has_transcript=bool(row["has_transcript"]),
        has_notes=bool(row["has_notes"]),
    )
