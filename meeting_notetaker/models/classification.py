"""Session classification: Series, Topics, and Contacts (formerly People).

Adds three orthogonal classification dimensions to each session:

* **Series** (1:N) -- a named recurring meeting. Each session has at
  most one series (or NULL = unfiled). Auto-derived from the title
  for repeat meetings ("Platform Team Sync 2026-05-24" matches an
  existing "Platform Team Sync" series).
* **Contacts / People** (M:N) -- the human participants associated
  with a session. Auto-populated from the `# Attendees` bulleted
  list in My Notes; UI surface in the chips bar still labels them
  "People" because that's the per-session role. The underlying
  entity is a Contact in the unified Address Book (see
  meeting_notetaker.diarization.store for the speaker-side
  contact_id link).
* **Topics** (M:N) -- free-form themes/technologies/projects
  discussed. Auto-extracted from the synthesis output by a
  deterministic extractor (no LLM round-trip per the issue's
  design constraint). User can accept/reject the auto-suggestions.

All three live in a sibling SQLite at `<app_data>/classification.db`.

## Why Contacts (vs the previous Person table)

PR #27 first cut had a `people` table where each Person was a
display_name string -- the same human surfacing as "Bob" in one
meeting and "Bob Smith" in another would create two unrelated
rows. v0.7.0 follow-up (issue #28) introduces a Contact entity
with alias matching: typing "BS" in attendees resolves to the
Bob Smith contact via a stored alias, and renaming a Contact
propagates everywhere it's referenced (sessions, speaker store).

The old people / session_people tables are dropped entirely;
classification.db schema is incompatible with prior versions
(delete the file before launching this build).
"""
from __future__ import annotations

import re
import sqlite3
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
SOURCE_CALENDAR = "calendar"
SOURCE_MERGED_FROM = "merged_from"

ALIAS_KIND_NAME = "name"
ALIAS_KIND_SHORT = "short"
ALIAS_KIND_INITIAL = "initial"
ALIAS_KIND_EMAIL = "email"
ALL_ALIAS_KINDS = (
    ALIAS_KIND_NAME, ALIAS_KIND_SHORT,
    ALIAS_KIND_INITIAL, ALIAS_KIND_EMAIL,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    id                            INTEGER PRIMARY KEY,
    name                          TEXT NOT NULL UNIQUE,
    outlook_recurring_master_id   TEXT,
    created_at                    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contacts (
    id            INTEGER PRIMARY KEY,
    display_name  TEXT NOT NULL,
    notes         TEXT DEFAULT '',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contact_aliases (
    id          INTEGER PRIMARY KEY,
    contact_id  INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    alias       TEXT NOT NULL,
    kind        TEXT NOT NULL,
    source      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- Per-contact uniqueness on (alias, kind) only -- the SAME alias
-- string can belong to multiple Contacts. "BS" might be a short
-- alias for both Bob Smith AND Brenda Saunders; the resolver
-- handles that ambiguity by creating a new Contact + surfacing the
-- candidates via suggested merges, so the user resolves
-- affirmatively in the Address Book rather than the system silently
-- picking the wrong one.
CREATE UNIQUE INDEX IF NOT EXISTS uq_contact_alias_kind
    ON contact_aliases(contact_id, alias COLLATE NOCASE, kind);
CREATE INDEX IF NOT EXISTS idx_aliases_lookup
    ON contact_aliases(alias COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_aliases_contact
    ON contact_aliases(contact_id);

CREATE TABLE IF NOT EXISTS topics (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_series (
    session_id  TEXT PRIMARY KEY,
    series_id   INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS session_contacts (
    session_id  TEXT NOT NULL,
    contact_id  INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    PRIMARY KEY (session_id, contact_id)
);

CREATE TABLE IF NOT EXISTS session_topics (
    session_id  TEXT NOT NULL,
    topic_id    INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    accepted    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, topic_id)
);

CREATE INDEX IF NOT EXISTS idx_session_contacts_contact
    ON session_contacts(contact_id);
CREATE INDEX IF NOT EXISTS idx_session_topics_topic
    ON session_topics(topic_id);
CREATE INDEX IF NOT EXISTS idx_session_series_series
    ON session_series(series_id);
"""


@dataclass
class Series:
    id: int
    name: str
    outlook_recurring_master_id: Optional[str] = None
    created_at: str = ""


@dataclass
class Contact:
    id: int
    display_name: str
    notes: str = ""
    created_at: str = ""


@dataclass
class ContactAlias:
    id: int
    contact_id: int
    alias: str
    kind: str
    source: str
    created_at: str = ""


@dataclass
class Topic:
    id: int
    name: str
    created_at: str = ""


@dataclass
class SessionTopic:
    topic: Topic
    source: str
    accepted: bool


@dataclass
class SessionContact:
    """Per-session attendee record.

    `source` says how the link was established (attendee_list /
    diarization / manual / calendar / merged_from); the underlying
    Contact is the master identity.
    """
    contact: Contact
    source: str


@dataclass
class SessionClassification:
    """Aggregate view -- everything a session view's chips need."""
    series: Optional[Series] = None
    contacts: list[SessionContact] = field(default_factory=list)
    topics: list[SessionTopic] = field(default_factory=list)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def db_path() -> Path:
    return app_data_dir() / "classification.db"


class ClassificationStore:
    """SQLite-backed CRUD over the four classification dimensions
    (Series, Contacts, Topics, plus the session_* link tables)."""

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

    # ==================================================================
    # Series
    # ==================================================================

    def get_or_create_series(
        self,
        name: str,
        *,
        outlook_recurring_master_id: Optional[str] = None,
    ) -> Series:
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
        if source_id == target_id:
            return
        self._conn.execute(
            "UPDATE session_series SET series_id = ? WHERE series_id = ?",
            (target_id, source_id),
        )
        self._conn.execute("DELETE FROM series WHERE id = ?", (source_id,))

    def delete_series(self, series_id: int) -> None:
        self._conn.execute("DELETE FROM series WHERE id = ?", (series_id,))

    def list_series(self) -> list[Series]:
        rows = self._conn.execute(
            "SELECT * FROM series ORDER BY name COLLATE NOCASE"
        ).fetchall()
        return [_row_to_series(r) for r in rows]

    def list_series_in_use(self) -> list[Series]:
        rows = self._conn.execute(
            """SELECT s.* FROM series s
               WHERE EXISTS (
                   SELECT 1 FROM session_series ss
                   WHERE ss.series_id = s.id
               )
               ORDER BY s.name COLLATE NOCASE"""
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

    # ==================================================================
    # Contacts + aliases
    # ==================================================================

    def create_contact(
        self,
        display_name: str,
        *,
        notes: str = "",
        initial_alias_source: str = SOURCE_MANUAL,
    ) -> Contact:
        """Create a Contact and seed it with one `name`-kind alias
        for the display name (so `find_by_alias` finds it).

        Use `get_or_create_contact_by_name` when you want
        find-or-create semantics; this is the raw create.
        """
        display_name = (display_name or "").strip()
        if not display_name:
            raise ValueError("contact display_name cannot be empty")
        when = utc_now_iso()
        cur = self._conn.execute(
            "INSERT INTO contacts (display_name, notes, created_at) VALUES (?, ?, ?)",
            (display_name, notes, when),
        )
        contact_id = cur.lastrowid
        self._insert_alias_row(
            contact_id, display_name, ALIAS_KIND_NAME,
            initial_alias_source, when,
        )
        return Contact(
            id=contact_id, display_name=display_name,
            notes=notes, created_at=when,
        )

    def get_contact(self, contact_id: int) -> Optional[Contact]:
        row = self._conn.execute(
            "SELECT * FROM contacts WHERE id = ?", (contact_id,),
        ).fetchone()
        return _row_to_contact(row) if row else None

    def find_contact_by_alias(
        self,
        alias: str,
        *,
        kind: Optional[str] = None,
    ) -> Optional[Contact]:
        """Single-result alias lookup. None when zero or multiple
        Contacts match (the caller uses find_contacts_by_alias to
        get the list for a disambiguation prompt)."""
        matches = self.find_contacts_by_alias(alias, kind=kind)
        return matches[0] if len(matches) == 1 else None

    def find_contacts_by_alias(
        self,
        alias: str,
        *,
        kind: Optional[str] = None,
    ) -> list[Contact]:
        """All Contacts whose aliases include `alias` (case-insensitive).

        Pass `kind` to constrain ('email', 'name', etc.); None
        searches every alias kind.
        """
        alias = (alias or "").strip()
        if not alias:
            return []
        if kind:
            sql = (
                "SELECT DISTINCT c.* FROM contacts c "
                "JOIN contact_aliases a ON a.contact_id = c.id "
                "WHERE a.alias = ? COLLATE NOCASE AND a.kind = ? "
                "ORDER BY c.display_name COLLATE NOCASE"
            )
            params = (alias, kind)
        else:
            sql = (
                "SELECT DISTINCT c.* FROM contacts c "
                "JOIN contact_aliases a ON a.contact_id = c.id "
                "WHERE a.alias = ? COLLATE NOCASE "
                "ORDER BY c.display_name COLLATE NOCASE"
            )
            params = (alias,)
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_contact(r) for r in rows]

    def get_or_create_contact_by_name(
        self,
        name: str,
        *,
        source: str = SOURCE_MANUAL,
    ) -> Contact:
        """Find by exact alias hit OR by display_name; create if
        neither matches. Used by callers that have a single name
        with no ambiguity worry (manual chip-bar add, calendar
        seed where the email lookup already filtered)."""
        name = (name or "").strip()
        if not name:
            raise ValueError("contact name cannot be empty")
        existing = self.find_contacts_by_alias(name)
        if existing:
            # Prefer exact display_name match if any, else the first.
            for c in existing:
                if c.display_name.casefold() == name.casefold():
                    return c
            return existing[0]
        return self.create_contact(name, initial_alias_source=source)

    def rename_contact(self, contact_id: int, new_name: str) -> None:
        """Update the Contact's display_name AND ensure the new name
        is a `name`-kind alias so future lookups by the new name
        succeed.

        The old display_name remains as an alias by design -- the
        old form might still appear in transcripts/notes and we
        want those mentions to keep resolving. Caller can remove
        the old name explicitly via remove_alias if they really
        want to.
        """
        new_name = (new_name or "").strip()
        if not new_name:
            raise ValueError("new contact name cannot be empty")
        self._conn.execute(
            "UPDATE contacts SET display_name = ? WHERE id = ?",
            (new_name, contact_id),
        )
        # Ensure the new name is also reachable as a name-alias.
        self.add_alias(contact_id, new_name, kind=ALIAS_KIND_NAME, source=SOURCE_MANUAL)

    def update_contact_notes(self, contact_id: int, notes: str) -> None:
        self._conn.execute(
            "UPDATE contacts SET notes = ? WHERE id = ?", (notes, contact_id),
        )

    def merge_contacts(self, source_id: int, target_id: int) -> None:
        """Reassign every alias + session link from source -> target,
        merge notes, drop source. PK collisions on session_contacts
        are merged (target's source kept; source's row deleted).

        After the merge, an alias rows record the prior identity by
        adding the source's display_name as a 'merged_from'-source
        alias on the target. The user can clean these up via
        remove_alias if desired.
        """
        if source_id == target_id:
            return
        source = self.get_contact(source_id)
        target = self.get_contact(target_id)
        if source is None or target is None:
            return
        # Drop conflicting session_contacts rows on source side first
        # so the UPDATE doesn't trip the PK.
        self._conn.execute(
            """DELETE FROM session_contacts
               WHERE contact_id = ?
                 AND session_id IN (
                     SELECT session_id FROM session_contacts WHERE contact_id = ?
                 )""",
            (source_id, target_id),
        )
        self._conn.execute(
            "UPDATE session_contacts SET contact_id = ? WHERE contact_id = ?",
            (target_id, source_id),
        )
        # Move aliases. Collisions go silently -- (alias, kind) is
        # globally unique. The source's display_name lands as a
        # merged_from alias if it isn't already an alias of target.
        when = utc_now_iso()
        if not self._alias_exists(target_id, source.display_name, ALIAS_KIND_NAME):
            try:
                self._insert_alias_row(
                    target_id, source.display_name, ALIAS_KIND_NAME,
                    SOURCE_MERGED_FROM, when,
                )
            except sqlite3.IntegrityError:
                pass
        # Aliases that don't collide come along.
        self._conn.execute(
            """UPDATE OR IGNORE contact_aliases
               SET contact_id = ?
               WHERE contact_id = ?""",
            (target_id, source_id),
        )
        # Notes: append if target lacks them.
        if source.notes and not target.notes:
            self.update_contact_notes(target_id, source.notes)
        elif source.notes and source.notes not in target.notes:
            merged_notes = (target.notes + "\n\n" + source.notes).strip()
            self.update_contact_notes(target_id, merged_notes)
        # CASCADE drops residual source alias rows.
        self._conn.execute("DELETE FROM contacts WHERE id = ?", (source_id,))

    def delete_contact(self, contact_id: int) -> None:
        """CASCADE removes the Contact's aliases + session_contacts rows."""
        self._conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))

    def list_contacts(self) -> list[Contact]:
        rows = self._conn.execute(
            "SELECT * FROM contacts ORDER BY display_name COLLATE NOCASE"
        ).fetchall()
        return [_row_to_contact(r) for r in rows]

    def list_contacts_in_use(self) -> list[Contact]:
        rows = self._conn.execute(
            """SELECT c.* FROM contacts c
               WHERE EXISTS (
                   SELECT 1 FROM session_contacts sc
                   WHERE sc.contact_id = c.id
               )
               ORDER BY c.display_name COLLATE NOCASE"""
        ).fetchall()
        return [_row_to_contact(r) for r in rows]

    def list_orphan_contacts(self) -> list[Contact]:
        """Contacts with zero session_contacts rows. Drives the
        Address Book "delete orphans" affordance."""
        rows = self._conn.execute(
            """SELECT c.* FROM contacts c
               WHERE NOT EXISTS (
                   SELECT 1 FROM session_contacts sc
                   WHERE sc.contact_id = c.id
               )
               ORDER BY c.display_name COLLATE NOCASE"""
        ).fetchall()
        return [_row_to_contact(r) for r in rows]

    def session_count_for_contact(self, contact_id: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM session_contacts WHERE contact_id = ?",
            (contact_id,),
        ).fetchone()
        return int(row["n"]) if row else 0

    # ---- aliases ----
    def list_aliases(self, contact_id: int) -> list[ContactAlias]:
        rows = self._conn.execute(
            """SELECT * FROM contact_aliases WHERE contact_id = ?
               ORDER BY kind, alias COLLATE NOCASE""",
            (contact_id,),
        ).fetchall()
        return [_row_to_alias(r) for r in rows]

    def add_alias(
        self,
        contact_id: int,
        alias: str,
        *,
        kind: str = ALIAS_KIND_NAME,
        source: str = SOURCE_MANUAL,
    ) -> Optional[ContactAlias]:
        """Insert an alias if THIS contact doesn't already have it.

        (alias, kind) uniqueness is per-contact, not global -- "BS"
        as a short alias can belong to multiple Contacts. Callers
        who care about cross-contact collisions check
        find_contacts_by_alias first; the resolver explicitly wants
        to allow shared aliases so it can detect ambiguity at
        match time.

        Returns the new row on insert, None when this contact
        already had the alias (silent dedupe).
        """
        alias = (alias or "").strip()
        if not alias:
            return None
        if kind not in ALL_ALIAS_KINDS:
            raise ValueError(f"unknown alias kind: {kind!r}")
        existing = self._conn.execute(
            "SELECT 1 FROM contact_aliases WHERE contact_id = ? "
            "AND alias = ? COLLATE NOCASE AND kind = ?",
            (contact_id, alias, kind),
        ).fetchone()
        if existing is not None:
            return None  # same contact already has it; no-op
        when = utc_now_iso()
        return self._insert_alias_row(contact_id, alias, kind, source, when)

    def _insert_alias_row(
        self, contact_id: int, alias: str, kind: str, source: str, when: str,
    ) -> ContactAlias:
        cur = self._conn.execute(
            "INSERT INTO contact_aliases (contact_id, alias, kind, source, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (contact_id, alias, kind, source, when),
        )
        return ContactAlias(
            id=cur.lastrowid, contact_id=contact_id,
            alias=alias, kind=kind, source=source, created_at=when,
        )

    def _alias_exists(self, contact_id: int, alias: str, kind: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM contact_aliases WHERE contact_id = ? "
            "AND alias = ? COLLATE NOCASE AND kind = ?",
            (contact_id, alias, kind),
        ).fetchone()
        return row is not None

    def remove_alias(self, alias_id: int) -> None:
        self._conn.execute("DELETE FROM contact_aliases WHERE id = ?", (alias_id,))

    def remove_alias_by_text(
        self,
        contact_id: int,
        alias: str,
        *,
        kind: Optional[str] = None,
    ) -> int:
        if kind:
            cur = self._conn.execute(
                "DELETE FROM contact_aliases WHERE contact_id = ? "
                "AND alias = ? COLLATE NOCASE AND kind = ?",
                (contact_id, alias, kind),
            )
        else:
            cur = self._conn.execute(
                "DELETE FROM contact_aliases WHERE contact_id = ? "
                "AND alias = ? COLLATE NOCASE",
                (contact_id, alias),
            )
        return cur.rowcount or 0

    # ---- session-level links (the "People" association on a session) ----
    def add_session_contact(
        self,
        session_id: str,
        contact_id: int,
        *,
        source: str = SOURCE_MANUAL,
    ) -> None:
        self._conn.execute(
            "INSERT INTO session_contacts (session_id, contact_id, source) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(session_id, contact_id) DO NOTHING",
            (session_id, contact_id, source),
        )

    def remove_session_contact(self, session_id: str, contact_id: int) -> None:
        self._conn.execute(
            "DELETE FROM session_contacts WHERE session_id = ? AND contact_id = ?",
            (session_id, contact_id),
        )

    def contacts_for_session(self, session_id: str) -> list[SessionContact]:
        rows = self._conn.execute(
            """SELECT c.*, sc.source as link_source
               FROM contacts c
               JOIN session_contacts sc ON sc.contact_id = c.id
               WHERE sc.session_id = ?
               ORDER BY c.display_name COLLATE NOCASE""",
            (session_id,),
        ).fetchall()
        return [
            SessionContact(contact=_row_to_contact(r), source=r["link_source"])
            for r in rows
        ]

    def session_ids_for_contact(self, contact_id: int) -> list[str]:
        rows = self._conn.execute(
            "SELECT session_id FROM session_contacts WHERE contact_id = ? ORDER BY session_id",
            (contact_id,),
        ).fetchall()
        return [r["session_id"] for r in rows]

    def replace_session_attendee_contacts(
        self,
        session_id: str,
        contact_ids: Iterable[int],
    ) -> None:
        """Replace the session's attendee_list-sourced links with the
        given Contact ids. Preserves manual / diarization / merged
        links untouched -- only attendee_list-source rows are
        rewritten."""
        ids = list(contact_ids)
        self._conn.execute(
            "DELETE FROM session_contacts WHERE session_id = ? AND source = ?",
            (session_id, SOURCE_ATTENDEE_LIST),
        )
        for cid in ids:
            self.add_session_contact(session_id, cid, source=SOURCE_ATTENDEE_LIST)

    # ==================================================================
    # Topics  --  unchanged shape; merge_topics fixed for accepted-loss
    # ==================================================================

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
        """Move every session_topics row of source to target.

        Conflict-safe: when both source + target are already linked
        to the same session, the merged row's `accepted` becomes the
        MAX of the two (so a user's prior Accept is never silently
        lost), and the source ('manual' beats 'auto') wins ties.
        This was the bug in the first cut where UPDATE OR IGNORE
        would drop the source row -- losing accepted=True if the
        source was accepted and the target was a suggestion.
        """
        if source_id == target_id:
            return
        # Find collisions (sessions linked to BOTH).
        collisions = self._conn.execute(
            """SELECT a.session_id,
                      a.source   AS src_source,
                      a.accepted AS src_accepted,
                      b.source   AS dst_source,
                      b.accepted AS dst_accepted
               FROM session_topics a
               JOIN session_topics b
                 ON a.session_id = b.session_id
               WHERE a.topic_id = ? AND b.topic_id = ?""",
            (source_id, target_id),
        ).fetchall()
        for row in collisions:
            new_accepted = max(int(row["src_accepted"]), int(row["dst_accepted"]))
            new_source = (
                SOURCE_MANUAL
                if SOURCE_MANUAL in (row["src_source"], row["dst_source"])
                else row["dst_source"]
            )
            self._conn.execute(
                "UPDATE session_topics SET accepted = ?, source = ? "
                "WHERE session_id = ? AND topic_id = ?",
                (new_accepted, new_source, row["session_id"], target_id),
            )
            self._conn.execute(
                "DELETE FROM session_topics "
                "WHERE session_id = ? AND topic_id = ?",
                (row["session_id"], source_id),
            )
        # The rest move cleanly (no collision left after the above).
        self._conn.execute(
            "UPDATE session_topics SET topic_id = ? WHERE topic_id = ?",
            (target_id, source_id),
        )
        self._conn.execute("DELETE FROM topics WHERE id = ?", (source_id,))

    def delete_topic(self, topic_id: int) -> None:
        """CASCADE removes the topic's session_topics rows."""
        self._conn.execute("DELETE FROM topics WHERE id = ?", (topic_id,))

    def list_topics(self) -> list[Topic]:
        rows = self._conn.execute(
            "SELECT * FROM topics ORDER BY name COLLATE NOCASE"
        ).fetchall()
        return [_row_to_topic(r) for r in rows]

    def list_topics_in_use(self) -> list[Topic]:
        rows = self._conn.execute(
            """SELECT t.* FROM topics t
               WHERE EXISTS (
                   SELECT 1 FROM session_topics st
                   WHERE st.topic_id = t.id AND st.accepted = 1
               )
               ORDER BY t.name COLLATE NOCASE"""
        ).fetchall()
        return [_row_to_topic(r) for r in rows]

    def list_orphan_topics(self) -> list[Topic]:
        """Topics with zero session_topics rows (accepted OR not).
        Drives bulk-delete in Manage Topics."""
        rows = self._conn.execute(
            """SELECT t.* FROM topics t
               WHERE NOT EXISTS (
                   SELECT 1 FROM session_topics st
                   WHERE st.topic_id = t.id
               )
               ORDER BY t.name COLLATE NOCASE"""
        ).fetchall()
        return [_row_to_topic(r) for r in rows]

    def session_count_for_topic(self, topic_id: int, *, accepted_only: bool = True) -> int:
        if accepted_only:
            sql = (
                "SELECT COUNT(*) AS n FROM session_topics "
                "WHERE topic_id = ? AND accepted = 1"
            )
        else:
            sql = "SELECT COUNT(*) AS n FROM session_topics WHERE topic_id = ?"
        row = self._conn.execute(sql, (topic_id,)).fetchone()
        return int(row["n"]) if row else 0

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
        self, session_id: str, topic_id: int, accepted: bool,
    ) -> None:
        self._conn.execute(
            "UPDATE session_topics SET accepted = ? WHERE session_id = ? AND topic_id = ?",
            (int(accepted), session_id, topic_id),
        )

    def replace_session_topic_suggestions(
        self, session_id: str, topic_names: Iterable[str],
    ) -> None:
        self._conn.execute(
            "DELETE FROM session_topics WHERE session_id = ? AND source = ? AND accepted = 0",
            (session_id, SOURCE_AUTO),
        )
        for name in topic_names:
            if not name or not name.strip():
                continue
            topic = self.get_or_create_topic(name)
            self.add_session_topic(
                session_id, topic.id, source=SOURCE_AUTO, accepted=False,
            )

    # ==================================================================
    # Aggregate + cleanup
    # ==================================================================

    def classification_for_session(self, session_id: str) -> SessionClassification:
        return SessionClassification(
            series=self.series_for_session(session_id),
            contacts=self.contacts_for_session(session_id),
            topics=self.topics_for_session(session_id),
        )

    def remove_session(self, session_id: str) -> None:
        """Drop every classification association for a session.

        Called when a session is deleted from sessions.db. The
        Contacts / Topics / Series themselves are kept (orphans are
        managed via Address Book / Manage Classification UIs).
        """
        self._conn.execute(
            "DELETE FROM session_series WHERE session_id = ?", (session_id,),
        )
        self._conn.execute(
            "DELETE FROM session_contacts WHERE session_id = ?", (session_id,),
        )
        self._conn.execute(
            "DELETE FROM session_topics WHERE session_id = ?", (session_id,),
        )

    def delete_orphan_contacts(self) -> int:
        ids = [c.id for c in self.list_orphan_contacts()]
        for cid in ids:
            self.delete_contact(cid)
        return len(ids)

    def delete_orphan_topics(self) -> int:
        ids = [t.id for t in self.list_orphan_topics()]
        for tid in ids:
            self.delete_topic(tid)
        return len(ids)


# ----------------------------------------------------------------------
# Row -> dataclass helpers


def _row_to_series(row: sqlite3.Row) -> Series:
    return Series(
        id=row["id"],
        name=row["name"],
        outlook_recurring_master_id=row["outlook_recurring_master_id"],
        created_at=row["created_at"],
    )


def _row_to_contact(row: sqlite3.Row) -> Contact:
    return Contact(
        id=row["id"],
        display_name=row["display_name"],
        notes=row["notes"] if "notes" in row.keys() else "",
        created_at=row["created_at"],
    )


def _row_to_alias(row: sqlite3.Row) -> ContactAlias:
    return ContactAlias(
        id=row["id"],
        contact_id=row["contact_id"],
        alias=row["alias"],
        kind=row["kind"],
        source=row["source"],
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
    s = _TITLE_STRIP_TOKENS_RE.sub(" ", s.lower())
    s = _TITLE_NORMALIZE_RE.sub(" ", s).strip()
    return s.strip(" -:|()[]")
