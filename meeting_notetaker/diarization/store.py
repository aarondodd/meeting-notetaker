"""Persistent identity store: known speaker name -> embedding centroid.

Lives at `%APPDATA%/MeetingNotetaker/speakers.db` (sqlite). Plain schema:

    speakers(
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        embedding BLOB NOT NULL,            -- numpy float32 array, raw bytes
        embedding_dim INTEGER NOT NULL,
        sample_count INTEGER NOT NULL,
        created_at TEXT NOT NULL,           -- ISO-8601 UTC
        last_seen_at TEXT NOT NULL,
        notes TEXT NOT NULL DEFAULT ''
    )

The centroid is updated as a weighted running average each time the user
confirms a new sample for an existing speaker, so the stored vector self-
improves over meetings. `sample_count` tracks the running weight and is
also surfaced in the Settings UI as a rough confidence indicator.

The store is intentionally a single-table sqlite file rather than a JSON
dump because: (a) atomic transactions matter when the UI is mutating
during a meeting-end refinement, and (b) we may add per-meeting attendance
history later without breaking forward compat.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import numpy as np


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class SpeakerRecord:
    id: int
    name: str
    embedding: np.ndarray
    sample_count: int
    created_at: str
    last_seen_at: str
    notes: str = ""


@dataclass(frozen=True)
class MatchResult:
    speaker: SpeakerRecord
    similarity: float


class SpeakerStore:
    """Lightweight wrapper around speakers.db.

    Instances are cheap to construct; the sqlite connection is opened on
    first use and reused (sqlite3 is thread-safe for distinct connections,
    so callers can construct a per-thread instance if they need to mutate
    from a worker thread).
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    # ---- connection / schema ----

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS speakers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    embedding BLOB NOT NULL,
                    embedding_dim INTEGER NOT NULL,
                    sample_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.commit()
            self._conn = conn
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "SpeakerStore":
        self._connect()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ---- CRUD ----

    def list_all(self) -> list[SpeakerRecord]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM speakers ORDER BY last_seen_at DESC, name ASC"
        ).fetchall()
        return [self._record_from_row(r) for r in rows]

    def get_by_name(self, name: str) -> Optional[SpeakerRecord]:
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM speakers WHERE name = ?",
            (name,),
        ).fetchone()
        return self._record_from_row(row) if row else None

    def upsert(
        self,
        name: str,
        embedding: np.ndarray,
        *,
        sample_count: int = 1,
        notes: str = "",
    ) -> SpeakerRecord:
        """Insert or update a speaker. If `name` exists, the existing
        centroid is replaced with `embedding` and `sample_count` is set
        (not added). For incremental updates use `add_sample` instead.
        """
        emb = np.asarray(embedding, dtype=np.float32).reshape(-1)
        now = _now_iso()
        conn = self._connect()
        row = conn.execute(
            "SELECT id, created_at FROM speakers WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            cur = conn.execute(
                """
                INSERT INTO speakers
                    (name, embedding, embedding_dim, sample_count,
                     created_at, last_seen_at, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (name, emb.tobytes(), emb.size, sample_count, now, now, notes),
            )
            conn.commit()
            speaker_id = int(cur.lastrowid)
            created = now
        else:
            speaker_id = int(row["id"])
            created = row["created_at"]
            conn.execute(
                """
                UPDATE speakers
                SET embedding = ?, embedding_dim = ?, sample_count = ?,
                    last_seen_at = ?, notes = ?
                WHERE id = ?
                """,
                (emb.tobytes(), emb.size, sample_count, now, notes, speaker_id),
            )
            conn.commit()
        return SpeakerRecord(
            id=speaker_id,
            name=name,
            embedding=emb,
            sample_count=sample_count,
            created_at=created,
            last_seen_at=now,
            notes=notes,
        )

    def add_sample(self, name: str, embedding: np.ndarray) -> SpeakerRecord:
        """Add an embedding to an existing speaker (running-average update).

        Creates the speaker if it doesn't exist. Updates last_seen_at.
        Returns the new record.
        """
        emb = np.asarray(embedding, dtype=np.float32).reshape(-1)
        existing = self.get_by_name(name)
        if existing is None:
            return self.upsert(name, emb, sample_count=1)
        new_count = existing.sample_count + 1
        # Weighted average so the new sample contributes 1/new_count.
        new_centroid = (
            existing.embedding * existing.sample_count + emb
        ) / new_count
        return self.upsert(
            name,
            new_centroid,
            sample_count=new_count,
            notes=existing.notes,
        )

    def rename(self, old_name: str, new_name: str) -> bool:
        conn = self._connect()
        cur = conn.execute(
            "UPDATE speakers SET name = ? WHERE name = ?",
            (new_name, old_name),
        )
        conn.commit()
        return cur.rowcount > 0

    def forget(self, name: str) -> bool:
        conn = self._connect()
        cur = conn.execute("DELETE FROM speakers WHERE name = ?", (name,))
        conn.commit()
        return cur.rowcount > 0

    def forget_all(self) -> int:
        conn = self._connect()
        cur = conn.execute("DELETE FROM speakers")
        conn.commit()
        return cur.rowcount

    # ---- matching ----

    def match(
        self,
        embedding: np.ndarray,
        *,
        threshold: float = 0.75,
    ) -> Optional[MatchResult]:
        """Find the closest known speaker by cosine similarity.

        Returns the best match if its similarity is at or above
        `threshold`, otherwise None.
        """
        from .cluster import cosine_similarity  # local import: avoids cycle at import time

        emb = np.asarray(embedding, dtype=np.float32).reshape(-1)
        best: Optional[MatchResult] = None
        for record in self.list_all():
            sim = cosine_similarity(emb, record.embedding)
            if best is None or sim > best.similarity:
                best = MatchResult(speaker=record, similarity=sim)
        if best is None or best.similarity < threshold:
            return None
        return best

    # ---- helpers ----

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> SpeakerRecord:
        emb_bytes = row["embedding"]
        emb = np.frombuffer(emb_bytes, dtype=np.float32).reshape(-1).copy()
        if emb.size != int(row["embedding_dim"]):
            # Defensive -- shouldn't happen, but better to fail loudly than
            # quietly compare against truncated vectors.
            raise ValueError(
                f"speaker '{row['name']}' embedding length mismatch "
                f"(stored {row['embedding_dim']}, decoded {emb.size})"
            )
        return SpeakerRecord(
            id=int(row["id"]),
            name=row["name"],
            embedding=emb,
            sample_count=int(row["sample_count"]),
            created_at=row["created_at"],
            last_seen_at=row["last_seen_at"],
            notes=row["notes"] or "",
        )


def open_speaker_store() -> SpeakerStore:
    """Open the default speakers.db under the app data dir."""
    from ..utils.paths import app_data_dir
    return SpeakerStore(app_data_dir() / "speakers.db")
