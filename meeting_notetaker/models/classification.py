"""Session classification: Series, People, Topics.

Adds three orthogonal classification dimensions to each session:

* **Series** (1:N) -- a named recurring meeting. Each session has at
  most one series (or NULL = unfiled). Auto-derived from the title
  for repeat meetings ("Platform Team Sync 2026-05-24" matches an
  existing "Platform Team Sync" series).
* **People** (M:N) -- the human participants associated with a
  session. Auto-populated from the `# Attendees` bulleted list in
  My Notes; user can add/remove from the session view's chips row.
* **Topics** (M:N) -- free-form themes/technologies/projects
  discussed. Auto-extracted from the synthesis output by a
  deterministic extractor (no LLM round-trip per the issue's design
  constraint). User can accept/reject the auto-suggestions.

All three live in a sibling SQLite at `<app_data>/classification.db`
so the existing `sessions.db` schema stays untouched. A session's
series_id is stored here too rather than ALTERing sessions.sessions,
keeping migration-back-compat trivial (just delete classification.db).

The store has no foreign keys back to sessions.db -- a deleted
session leaves orphan rows that the periodic cleanup pass (called
on every list_*) sweeps lazily. Cheap and avoids cross-DB joins.
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Optional

from ..utils.paths import app_data_dir


SOURCE_ATTENDEE_LIST = "attendee_list"
SOURCE_DIARIZATION = "diarization"
SOURCE_MANUAL = "manual"
SOURCE_AUTO = "auto"


SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    id                            INTEGER PRIMARY KEY,
    name                          TEXT NOT NULL UNIQUE,
    outlook_recurring_master_id   TEXT,
    created_at                    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS people (
    id            INTEGER PRIMARY KEY,
    display_name  TEXT NOT NULL UNIQUE COLLATE NOCASE,
    email         TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topics (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_series (
    session_id  TEXT PRIMARY KEY,
    series_id   INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS session_people (
    session_id  TEXT NOT NULL,
    person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    PRIMARY KEY (session_id, person_id)
);

CREATE TABLE IF NOT EXISTS session_topics (
    session_id  TEXT NOT NULL,
    topic_id    INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    accepted    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, topic_id)
);

CREATE INDEX IF NOT EXISTS idx_session_people_person ON session_people(person_id);
CREATE INDEX IF NOT EXISTS idx_session_topics_topic ON session_topics(topic_id);
CREATE INDEX IF NOT EXISTS idx_session_series_series ON session_series(series_id);
"""


@dataclass
class Series:
    id: int
    name: str
    outlook_recurring_master_id: Optional[str] = None
    created_at: str = ""


@dataclass
class Person:
    id: int
    display_name: str
    email: Optional[str] = None
    created_at: str = ""


@dataclass
class Topic:
    id: int
    name: str
    created_at: str = ""


@dataclass
class SessionTopic:
    """A topic association for one session.

    `accepted` distinguishes user-confirmed associations from
    auto-suggestions still in the suggestion bucket. The UI surfaces
    suggestions separately from confirmed chips; this flag is the
    discriminator.
    """
    topic: Topic
    source: str            # SOURCE_AUTO | SOURCE_MANUAL
    accepted: bool


@dataclass
class SessionPerson:
    person: Person
    source: str            # SOURCE_ATTENDEE_LIST | SOURCE_DIARIZATION | SOURCE_MANUAL


@dataclass
class SessionClassification:
    """Aggregate view -- everything a session view's chips need."""
    series: Optional[Series] = None
    people: list[SessionPerson] = field(default_factory=list)
    topics: list[SessionTopic] = field(default_factory=list)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def db_path() -> Path:
    """`<app_data>/classification.db` -- co-resident with sessions /
    speakers / search."""
    return app_data_dir() / "classification.db"


class ClassificationStore:
    """SQLite-backed CRUD over the three classification dimensions."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or db_path()
        self._conn = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ClassificationStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Series

    def get_or_create_series(
        self,
        name: str,
        *,
        outlook_recurring_master_id: Optional[str] = None,
    ) -> Series:
        """Find by exact (case-insensitive) name or create. Returns
        the canonical Series with whatever id ended up in the table."""
        name = name.strip()
        if not name:
            raise ValueError("series name cannot be empty")
        row = self._conn.execute(
            "SELECT * FROM series WHERE name = ? COLLATE NOCASE", (name,),
        ).fetchone()
        if row:
            return _row_to_series(row)
        when = utc_now_iso()
        cur = self._conn.execute(
            "INSERT INTO series (name, outlook_recurring_master_id, created_at) VALUES (?, ?, ?)",
            (name, outlook_recurring_master_id, when),
        )
        return Series(
            id=cur.lastrowid,
            name=name,
            outlook_recurring_master_id=outlook_recurring_master_id,
            created_at=when,
        )

    def find_series_by_name(self, name: str) -> Optional[Series]:
        row = self._conn.execute(
            "SELECT * FROM series WHERE name = ? COLLATE NOCASE", (name.strip(),),
        ).fetchone()
        return _row_to_series(row) if row else None

    def find_series_for_title(
        self,
        title: str,
        *,
        threshold: float = 0.8,
    ) -> Optional[Series]:
        """Fuzzy-match a session title against known series names.

        Used at session-creation time to auto-link a recurring
        meeting whose title varies session-to-session ("Platform
        Team Sync 2026-05-24" -> series "Platform Team Sync").
        Returns the best-matching series above `threshold`, or None.
        """
        normalized_title = _normalize_title(title)
        if not normalized_title:
            return None
        best_score = 0.0
        best_series: Optional[Series] = None
        for s in self.list_series():
            normalized_name = _normalize_title(s.name)
            if not normalized_name:
                continue
            score = SequenceMatcher(
                None, normalized_title, normalized_name,
            ).ratio()
            # Also accept the series-name-as-substring case (works
            # well for templated titles like "Platform Team Sync -- weekly").
            if normalized_name in normalized_title:
                score = max(score, 0.95)
            if score > best_score:
                best_score = score
                best_series = s
        if best_score >= threshold:
            return best_series
        return None

    def rename_series(self, series_id: int, new_name: str) -> None:
        new_name = new_name.strip()
        if not new_name:
            raise ValueError("new series name cannot be empty")
        self._conn.execute(
            "UPDATE series SET name = ? WHERE id = ?", (new_name, series_id),
        )

    def merge_series(self, source_id: int, target_id: int) -> None:
        """Reassign every session of `source_id` to `target_id`, then
        delete `source_id`. No-op when source == target."""
        if source_id == target_id:
            return
        self._conn.execute(
            "UPDATE session_series SET series_id = ? WHERE series_id = ?",
            (target_id, source_id),
        )
        self._conn.execute("DELETE FROM series WHERE id = ?", (source_id,))

    def delete_series(self, series_id: int) -> None:
        """Drops the series and all session_series rows for it via
        the ON DELETE CASCADE foreign key."""
        self._conn.execute("DELETE FROM series WHERE id = ?", (series_id,))

    def list_series(self) -> list[Series]:
        rows = self._conn.execute(
            "SELECT * FROM series ORDER BY name COLLATE NOCASE"
        ).fetchall()
        return [_row_to_series(r) for r in rows]

    def assign_series(self, session_id: str, series_id: Optional[int]) -> None:
        if series_id is None:
            self._conn.execute(
                "DELETE FROM session_series WHERE session_id = ?", (session_id,),
            )
            return
        self._conn.execute(
            "INSERT INTO session_series (session_id, series_id) VALUES (?, ?)"
            " ON CONFLICT(session_id) DO UPDATE SET series_id = excluded.series_id",
            (session_id, series_id),
        )

    def series_for_session(self, session_id: str) -> Optional[Series]:
        row = self._conn.execute(
            """SELECT s.* FROM series s
               JOIN session_series ss ON ss.series_id = s.id
               WHERE ss.session_id = ?""",
            (session_id,),
        ).fetchone()
        return _row_to_series(row) if row else None

    def session_ids_for_series(self, series_id: int) -> list[str]:
        rows = self._conn.execute(
            "SELECT session_id FROM session_series WHERE series_id = ? ORDER BY session_id",
            (series_id,),
        ).fetchall()
        return [r["session_id"] for r in rows]

    # ------------------------------------------------------------------
    # People

    def get_or_create_person(
        self,
        display_name: str,
        *,
        email: Optional[str] = None,
    ) -> Person:
        display_name = display_name.strip()
        if not display_name:
            raise ValueError("person display_name cannot be empty")
        row = self._conn.execute(
            "SELECT * FROM people WHERE display_name = ? COLLATE NOCASE",
            (display_name,),
        ).fetchone()
        if row:
            return _row_to_person(row)
        when = utc_now_iso()
        cur = self._conn.execute(
            "INSERT INTO people (display_name, email, created_at) VALUES (?, ?, ?)",
            (display_name, email, when),
        )
        return Person(
            id=cur.lastrowid,
            display_name=display_name,
            email=email,
            created_at=when,
        )

    def rename_person(self, person_id: int, new_name: str) -> None:
        new_name = new_name.strip()
        if not new_name:
            raise ValueError("new person name cannot be empty")
        self._conn.execute(
            "UPDATE people SET display_name = ? WHERE id = ?", (new_name, person_id),
        )

    def list_people(self) -> list[Person]:
        rows = self._conn.execute(
            "SELECT * FROM people ORDER BY display_name COLLATE NOCASE"
        ).fetchall()
        return [_row_to_person(r) for r in rows]

    def add_session_person(
        self,
        session_id: str,
        person_id: int,
        *,
        source: str = SOURCE_MANUAL,
    ) -> None:
        self._conn.execute(
            "INSERT INTO session_people (session_id, person_id, source) VALUES (?, ?, ?)"
            " ON CONFLICT(session_id, person_id) DO NOTHING",
            (session_id, person_id, source),
        )

    def remove_session_person(self, session_id: str, person_id: int) -> None:
        self._conn.execute(
            "DELETE FROM session_people WHERE session_id = ? AND person_id = ?",
            (session_id, person_id),
        )

    def people_for_session(self, session_id: str) -> list[SessionPerson]:
        rows = self._conn.execute(
            """SELECT p.*, sp.source as link_source
               FROM people p
               JOIN session_people sp ON sp.person_id = p.id
               WHERE sp.session_id = ?
               ORDER BY p.display_name COLLATE NOCASE""",
            (session_id,),
        ).fetchall()
        return [
            SessionPerson(person=_row_to_person(r), source=r["link_source"])
            for r in rows
        ]

    def session_ids_for_person(self, person_id: int) -> list[str]:
        rows = self._conn.execute(
            "SELECT session_id FROM session_people WHERE person_id = ? ORDER BY session_id",
            (person_id,),
        ).fetchall()
        return [r["session_id"] for r in rows]

    def sync_session_people(
        self,
        session_id: str,
        attendee_names: Iterable[str],
        *,
        source: str = SOURCE_ATTENDEE_LIST,
    ) -> None:
        """Replace the session's attendee-sourced people with the
        given list. Preserves manual / diarization entries untouched.

        Called after every save_live_notes -- the # Attendees list
        is the source of truth for which people are in the meeting.
        """
        names = [n.strip() for n in attendee_names if n and n.strip()]
        # Drop only rows of this source for this session; keep manual
        # / diarization-derived associations.
        self._conn.execute(
            "DELETE FROM session_people WHERE session_id = ? AND source = ?",
            (session_id, source),
        )
        for name in names:
            person = self.get_or_create_person(name)
            self.add_session_person(session_id, person.id, source=source)

    # ------------------------------------------------------------------
    # Topics

    def get_or_create_topic(self, name: str) -> Topic:
        name = name.strip()
        if not name:
            raise ValueError("topic name cannot be empty")
        row = self._conn.execute(
            "SELECT * FROM topics WHERE name = ? COLLATE NOCASE", (name,),
        ).fetchone()
        if row:
            return _row_to_topic(row)
        when = utc_now_iso()
        cur = self._conn.execute(
            "INSERT INTO topics (name, created_at) VALUES (?, ?)",
            (name, when),
        )
        return Topic(id=cur.lastrowid, name=name, created_at=when)

    def rename_topic(self, topic_id: int, new_name: str) -> None:
        new_name = new_name.strip()
        if not new_name:
            raise ValueError("new topic name cannot be empty")
        self._conn.execute(
            "UPDATE topics SET name = ? WHERE id = ?", (new_name, topic_id),
        )

    def merge_topics(self, source_id: int, target_id: int) -> None:
        """Reassign every session_topics row of source_id to target_id,
        then delete the source topic. ON CONFLICT skips duplicates
        (a session that had both topics keeps only target after the
        merge)."""
        if source_id == target_id:
            return
        self._conn.execute(
            """UPDATE OR IGNORE session_topics
               SET topic_id = ?
               WHERE topic_id = ?""",
            (target_id, source_id),
        )
        # Any rows that hit the unique conflict above stayed pointed
        # at source_id; clean them up.
        self._conn.execute(
            "DELETE FROM session_topics WHERE topic_id = ?", (source_id,),
        )
        self._conn.execute("DELETE FROM topics WHERE id = ?", (source_id,))

    def list_topics(self) -> list[Topic]:
        rows = self._conn.execute(
            "SELECT * FROM topics ORDER BY name COLLATE NOCASE"
        ).fetchall()
        return [_row_to_topic(r) for r in rows]

    def topics_for_session(
        self,
        session_id: str,
        *,
        accepted_only: bool = False,
    ) -> list[SessionTopic]:
        sql = (
            """SELECT t.*, st.source as link_source, st.accepted as accepted_flag
               FROM topics t
               JOIN session_topics st ON st.topic_id = t.id
               WHERE st.session_id = ?"""
        )
        params: list[object] = [session_id]
        if accepted_only:
            sql += " AND st.accepted = 1"
        sql += " ORDER BY t.name COLLATE NOCASE"
        rows = self._conn.execute(sql, params).fetchall()
        return [
            SessionTopic(
                topic=_row_to_topic(r),
                source=r["link_source"],
                accepted=bool(r["accepted_flag"]),
            )
            for r in rows
        ]

    def session_ids_for_topic(self, topic_id: int) -> list[str]:
        rows = self._conn.execute(
            """SELECT session_id FROM session_topics
               WHERE topic_id = ? AND accepted = 1
               ORDER BY session_id""",
            (topic_id,),
        ).fetchall()
        return [r["session_id"] for r in rows]

    def add_session_topic(
        self,
        session_id: str,
        topic_id: int,
        *,
        source: str = SOURCE_MANUAL,
        accepted: bool = True,
    ) -> None:
        self._conn.execute(
            """INSERT INTO session_topics (session_id, topic_id, source, accepted)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(session_id, topic_id) DO UPDATE SET
                 source = excluded.source,
                 accepted = MAX(accepted, excluded.accepted)""",
            (session_id, topic_id, source, int(accepted)),
        )

    def remove_session_topic(self, session_id: str, topic_id: int) -> None:
        self._conn.execute(
            "DELETE FROM session_topics WHERE session_id = ? AND topic_id = ?",
            (session_id, topic_id),
        )

    def set_topic_accepted(
        self,
        session_id: str,
        topic_id: int,
        accepted: bool,
    ) -> None:
        self._conn.execute(
            """UPDATE session_topics SET accepted = ?
               WHERE session_id = ? AND topic_id = ?""",
            (int(accepted), session_id, topic_id),
        )

    def replace_session_topic_suggestions(
        self,
        session_id: str,
        topic_names: Iterable[str],
    ) -> None:
        """Drop the session's auto / unaccepted topic rows and replace
        with the new suggestion list (also unaccepted).

        Preserves user-confirmed (accepted=1) topics, manual links,
        and any other-source links. Called after every save_notes
        with a fresh extraction.
        """
        # Step 1: drop only unaccepted auto-suggestions for this
        # session. Anything the user has accepted stays.
        self._conn.execute(
            """DELETE FROM session_topics
               WHERE session_id = ? AND source = ? AND accepted = 0""",
            (session_id, SOURCE_AUTO),
        )
        # Step 2: insert the fresh suggestion set.
        for name in topic_names:
            if not name or not name.strip():
                continue
            topic = self.get_or_create_topic(name)
            self.add_session_topic(
                session_id, topic.id, source=SOURCE_AUTO, accepted=False,
            )

    # ------------------------------------------------------------------
    # Aggregate

    def classification_for_session(self, session_id: str) -> SessionClassification:
        return SessionClassification(
            series=self.series_for_session(session_id),
            people=self.people_for_session(session_id),
            topics=self.topics_for_session(session_id),
        )

    def remove_session(self, session_id: str) -> None:
        """Drop every classification association for a session.

        Called when a session is deleted; the FK CASCADEs would
        handle session_people and session_topics IF those FKs
        pointed at sessions.sessions, but they don't (cross-DB).
        Explicit cleanup keeps the store tidy."""
        self._conn.execute(
            "DELETE FROM session_series WHERE session_id = ?", (session_id,),
        )
        self._conn.execute(
            "DELETE FROM session_people WHERE session_id = ?", (session_id,),
        )
        self._conn.execute(
            "DELETE FROM session_topics WHERE session_id = ?", (session_id,),
        )


# ----------------------------------------------------------------------
# Row -> dataclass helpers


def _row_to_series(row: sqlite3.Row) -> Series:
    return Series(
        id=row["id"],
        name=row["name"],
        outlook_recurring_master_id=row["outlook_recurring_master_id"],
        created_at=row["created_at"],
    )


def _row_to_person(row: sqlite3.Row) -> Person:
    return Person(
        id=row["id"],
        display_name=row["display_name"],
        email=row["email"],
        created_at=row["created_at"],
    )


def _row_to_topic(row: sqlite3.Row) -> Topic:
    return Topic(
        id=row["id"],
        name=row["name"],
        created_at=row["created_at"],
    )


# ----------------------------------------------------------------------
# Helpers shared with the extractor


_TITLE_NORMALIZE_RE = re.compile(r"\s+")
# Tokens stripped from titles when fuzzy-matching to a series name:
# day-of-week, ordinal dates, year suffixes, weekly/biweekly markers.
_TITLE_STRIP_TOKENS_RE = re.compile(
    r"\b(?:"
    r"\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}/\d{1,2}(?:/\d{2,4})?"
    r"|mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:r|rs|rsday)?"
    r"|fri(?:day)?|sat(?:urday)?|sun(?:day)?"
    r"|weekly|biweekly|monthly|quarterly"
    r"|\d{4}q[1-4]"
    r")\b",
    re.IGNORECASE,
)


def _normalize_title(s: str) -> str:
    """Lowercase + strip date/weekday markers + collapse whitespace.

    Lets the fuzzy series-matcher treat
    "Platform Team Sync 2026-05-24" and "Platform Team Sync -- Tuesday"
    as the same underlying series.
    """
    s = _TITLE_STRIP_TOKENS_RE.sub(" ", s.lower())
    s = _TITLE_NORMALIZE_RE.sub(" ", s).strip()
    # Drop leading / trailing punctuation noise left over after
    # the strip pass.
    return s.strip(" -:|()[]")
