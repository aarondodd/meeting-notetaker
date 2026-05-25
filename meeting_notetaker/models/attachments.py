"""Per-session attachments: arbitrary files copied into the session
folder so they travel with the meeting.

On-disk layout under `<session_dir>/`:

    attachments/
      20260525-093001-design-doc.pdf      # FS-safe name with timestamp prefix
      20260525-093015-screenshot.png
    attachments.json                       # metadata sidecar

The sidecar carries `display_name` (the user-visible label, which can
differ from the FS-safe basename after a rename), `size`, optional
`mime`, `added_at`, and `source` (`"manual"` for user-added files,
`"calendar"` for Outlook-imported, `"drop"` for drag-dropped).

Files are always COPIED (`shutil.copy2`); the source is never moved or
deleted. The store has no foreign keys back to sessions.db -- a deleted
session takes the whole `<session_dir>` with it, including
`attachments/`, so cleanup is implicit.

The sidecar is rewritten in full on every mutation. At realistic
attachment counts (dozens) this is cheap; lock-free single-writer
semantics match how every other per-session file in the app works.
"""
from __future__ import annotations

import json
import mimetypes
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from ..utils.paths import session_dir


SOURCE_MANUAL = "manual"
SOURCE_CALENDAR = "calendar"
SOURCE_DROP = "drop"
ALL_SOURCES = (SOURCE_MANUAL, SOURCE_CALENDAR, SOURCE_DROP)


SIDECAR_NAME = "attachments.json"
ATTACHMENTS_SUBDIR = "attachments"


@dataclass
class AttachmentRecord:
    """One file attached to a session.

    `id` is a uuid4 so display-name renames + on-disk renames can
    happen independently without breaking references. `stored_name`
    is the actual basename on disk (timestamp-prefixed + sanitized).
    `display_name` is what the user sees and edits.
    """
    id: str
    stored_name: str
    display_name: str
    size: int
    mime: str = ""
    added_at: str = ""
    source: str = SOURCE_MANUAL

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AttachmentRecord":
        return cls(
            id=str(data.get("id") or uuid.uuid4()),
            stored_name=str(data.get("stored_name") or ""),
            display_name=str(data.get("display_name") or data.get("stored_name") or ""),
            size=int(data.get("size") or 0),
            mime=str(data.get("mime") or ""),
            added_at=str(data.get("added_at") or ""),
            source=str(data.get("source") or SOURCE_MANUAL),
        )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Characters Windows refuses + a couple Linux-unfriendly ones.
_FS_UNSAFE_RE = __import__("re").compile(r'[\\/:*?"<>|\r\n\t]+')


def sanitize_basename(name: str, *, fallback: str = "attachment") -> str:
    """Strip FS-illegal chars, collapse whitespace, fall back when empty."""
    import re
    cleaned = _FS_UNSAFE_RE.sub(" ", name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.rstrip(".").strip()
    return cleaned or fallback


class AttachmentsStore:
    """JSON-backed CRUD over <session_dir>/attachments/ + sidecar."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.session_dir = session_dir(session_id)
        self.attachments_dir = self.session_dir / ATTACHMENTS_SUBDIR
        self.sidecar_path = self.session_dir / SIDECAR_NAME

    # ---- read ----
    def list(self) -> list[AttachmentRecord]:
        """Return records in insertion order (the order they appear in
        the sidecar). Callers that want chronological can sort by
        added_at; insertion order is good for a "most recently added
        at the bottom" feel."""
        records, _stale = self._read_sidecar()
        # Filter out records whose on-disk file vanished (manual
        # rm-rf, virus scanner quarantine, etc.). The next save will
        # re-write the sidecar without them.
        return [r for r in records if (self.attachments_dir / r.stored_name).exists()]

    def get(self, attachment_id: str) -> Optional[AttachmentRecord]:
        for r in self.list():
            if r.id == attachment_id:
                return r
        return None

    def file_path(self, attachment_id: str) -> Optional[Path]:
        rec = self.get(attachment_id)
        if rec is None:
            return None
        return self.attachments_dir / rec.stored_name

    # ---- write ----
    def add_file(
        self,
        src_path: Path,
        *,
        display_name: Optional[str] = None,
        source: str = SOURCE_MANUAL,
        now: Optional[datetime] = None,
    ) -> AttachmentRecord:
        """Copy `src_path` into the attachments dir + register in the
        sidecar. The original file is never moved or deleted.

        On-disk name = timestamp prefix + sanitized basename of the
        source. Collisions append `-2`, `-3`. `display_name` defaults
        to the source's basename (without the timestamp prefix) so
        the user sees the human name in the list.
        """
        src = Path(src_path)
        if not src.exists() or not src.is_file():
            raise FileNotFoundError(f"attachment source not found: {src_path}")
        if source not in ALL_SOURCES:
            raise ValueError(f"unknown attachment source: {source!r}")

        self.attachments_dir.mkdir(parents=True, exist_ok=True)
        when = now or datetime.now(timezone.utc)
        stamp = when.strftime("%Y%m%d-%H%M%S")
        safe_basename = sanitize_basename(src.name)
        candidate = f"{stamp}-{safe_basename}"
        dst = self.attachments_dir / candidate
        counter = 2
        while dst.exists():
            stem, dot, ext = safe_basename.rpartition(".")
            if dot:
                disambig = f"{stem}-{counter}.{ext}"
            else:
                disambig = f"{safe_basename}-{counter}"
            candidate = f"{stamp}-{disambig}"
            dst = self.attachments_dir / candidate
            counter += 1

        shutil.copy2(src, dst)
        size = dst.stat().st_size
        mime, _enc = mimetypes.guess_type(str(dst))
        record = AttachmentRecord(
            id=str(uuid.uuid4()),
            stored_name=candidate,
            display_name=(display_name or src.name).strip() or src.name,
            size=size,
            mime=mime or "",
            added_at=when.strftime("%Y-%m-%dT%H:%M:%SZ"),
            source=source,
        )
        records = self.list()
        records.append(record)
        self._save(records)
        return record

    def rename(self, attachment_id: str, new_display_name: str) -> Optional[AttachmentRecord]:
        """Update the sidecar display_name only -- on-disk basename
        stays stable. Returns the updated record or None when the id
        isn't found."""
        new_display_name = (new_display_name or "").strip()
        if not new_display_name:
            raise ValueError("display name cannot be empty")
        records = self.list()
        for i, r in enumerate(records):
            if r.id == attachment_id:
                records[i] = AttachmentRecord(
                    id=r.id,
                    stored_name=r.stored_name,
                    display_name=new_display_name,
                    size=r.size,
                    mime=r.mime,
                    added_at=r.added_at,
                    source=r.source,
                )
                self._save(records)
                return records[i]
        return None

    def delete(self, attachment_id: str) -> bool:
        """Remove from sidecar + delete the on-disk file. Returns True
        when the record existed."""
        records = self.list()
        kept: list[AttachmentRecord] = []
        target: Optional[AttachmentRecord] = None
        for r in records:
            if r.id == attachment_id:
                target = r
                continue
            kept.append(r)
        if target is None:
            return False
        try:
            (self.attachments_dir / target.stored_name).unlink()
        except FileNotFoundError:
            pass  # Already gone; sidecar mismatch is the user's lookout.
        self._save(kept)
        return True

    def save_as(self, attachment_id: str, dst_path: Path) -> Optional[Path]:
        """Copy an attachment out to a user-chosen destination.
        Returns the destination Path on success, None when the id
        isn't found."""
        src = self.file_path(attachment_id)
        if src is None or not src.exists():
            return None
        dst_path = Path(dst_path)
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst_path)
        return dst_path

    # ---- sidecar I/O ----
    def _read_sidecar(self) -> tuple[list[AttachmentRecord], bool]:
        """Return (records, had_stale_entries). Stale means the
        sidecar referenced a file that no longer exists on disk -- the
        caller's `list()` returns the filtered set; future writes will
        clean it up automatically."""
        if not self.sidecar_path.exists():
            return [], False
        try:
            payload = json.loads(self.sidecar_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return [], False
        raw_list = payload.get("attachments") if isinstance(payload, dict) else None
        if not isinstance(raw_list, list):
            return [], False
        out: list[AttachmentRecord] = []
        had_stale = False
        for entry in raw_list:
            if not isinstance(entry, dict):
                continue
            try:
                rec = AttachmentRecord.from_dict(entry)
            except Exception:
                continue
            if not rec.stored_name:
                continue
            on_disk = self.attachments_dir / rec.stored_name
            if not on_disk.exists():
                had_stale = True
                # Keep the record around in raw form; the caller
                # filters out stale entries from public list() but
                # we don't rewrite the sidecar in a pure read pass.
            out.append(rec)
        return out, had_stale

    def _save(self, records: list[AttachmentRecord]) -> None:
        """Write the sidecar with the given records, filtering out any
        whose on-disk file is missing."""
        self.session_dir.mkdir(parents=True, exist_ok=True)
        live = [
            r for r in records
            if (self.attachments_dir / r.stored_name).exists()
        ]
        body = {"attachments": [r.to_dict() for r in live]}
        self.sidecar_path.write_text(
            json.dumps(body, indent=2), encoding="utf-8",
        )


def list_attachments(session_id: str) -> list[AttachmentRecord]:
    """Convenience wrapper -- opens a store, returns the list, done."""
    return AttachmentsStore(session_id).list()


def import_attachments(
    session_id: str,
    paths: Iterable[Path],
    *,
    source: str = SOURCE_MANUAL,
) -> list[AttachmentRecord]:
    """Bulk-add helper. Useful for the calendar-import path and for
    drag-drop sessions where the user drops multiple files at once.
    Returns the new records (any source-not-found errors are
    swallowed with no record appended -- the caller can compare
    len(input) vs len(output))."""
    store = AttachmentsStore(session_id)
    out: list[AttachmentRecord] = []
    for p in paths:
        try:
            rec = store.add_file(Path(p), source=source)
            out.append(rec)
        except FileNotFoundError:
            continue
    return out
