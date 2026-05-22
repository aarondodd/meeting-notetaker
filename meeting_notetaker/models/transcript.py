"""Transcript segments + on-disk I/O.

Per-session layout under <session_dir>/:

    raw.transcript.md          interleaved transcript, source-labeled, time-stamped
    live_notes.md              user's own running notes (Attendees/Agenda/Notes/Action Items)
    notes.md                   latest LLM-generated (merged) notes
    notes-YYYYMMDD-HHMM.md     archived prior notes (older first)
    metadata.json              denormalized session metadata cache
    audio/{mic,sys}.wav        raw capture (deleted after transcription unless retained)

Saved transcripts always use the stable token "Me:" for the user's mic and
"Them:" for system audio -- those labels are the source-of-truth provenance
markers. Display and synthesis-prompt code substitute the user's preferred
name in via `rewrite_user_label`; the on-disk file is never rewritten when
the user changes their preferred name.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from ..utils.images import images_subdir
from ..utils.live_notes import seed_body as live_notes_seed_body
from ..utils.paths import session_dir


MIC = "mic"
SYS = "sys"
ALL_SOURCES = (MIC, SYS)
DEFAULT_USER_LABEL = "Me"

_USER_LABEL_LINE_RE = re.compile(r"^(\[\d{2}:\d{2}:\d{2}\]) Me: ", re.MULTILINE)


@dataclass
class TranscriptSegment:
    source: str            # 'mic' or 'sys' -- physical capture channel
    text: str
    t_start: float         # seconds since session start
    t_end: float
    is_provisional: bool = False
    # Resolved speaker label, set by the diarization refinement pass for
    # sys-source segments once a cluster has been assigned. Takes precedence
    # over the source-based "Them" fallback when rendering. None for mic
    # segments and for sys segments before refinement.
    speaker_name: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TranscriptSegment":
        return cls(
            source=d["source"],
            text=d["text"],
            t_start=float(d["t_start"]),
            t_end=float(d["t_end"]),
            is_provisional=bool(d.get("is_provisional", False)),
            speaker_name=d.get("speaker_name"),
        )


def label_for(source: str, user_name: str = "") -> str:
    """Render the speaker label for a segment based on capture source.

    `user_name` is the user's preferred display name (e.g. "John Smith").
    Empty string keeps the default "Me" label so the on-disk transcript
    stays consistent across name changes. Diarization-resolved labels
    (real names from the speaker store) bypass this function entirely --
    `format_segment` checks `speaker_name` first.
    """
    if source == "mic":
        return user_name.strip() or DEFAULT_USER_LABEL
    return {"sys": "Them"}.get(source, source.upper())


def format_segment(seg: TranscriptSegment, user_name: str = "") -> str:
    h, rem = divmod(int(seg.t_start), 3600)
    m, s = divmod(rem, 60)
    if seg.speaker_name:
        label = seg.speaker_name
    else:
        label = label_for(seg.source, user_name)
    return f"[{h:02d}:{m:02d}:{s:02d}] {label}: {seg.text.strip()}"


def rewrite_user_label(text: str, user_name: str) -> str:
    """Replace `[HH:MM:SS] Me: ` line prefixes with `[HH:MM:SS] <user_name>: `.

    Used when displaying an on-disk transcript (where mic lines are always
    saved as "Me:") with the user's preferred display name. The anchor on
    the timestamp prefix avoids matching the word "Me" anywhere else in the
    transcribed text. No-op when `user_name` is empty.
    """
    name = user_name.strip()
    if not name or name == DEFAULT_USER_LABEL:
        return text
    return _USER_LABEL_LINE_RE.sub(rf"\1 {name}: ", text)


class TranscriptStore:
    """Persists committed segments to raw.transcript.md and manages notes files."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.session_dir = session_dir(session_id)
        self.transcript_path = self.session_dir / "raw.transcript.md"
        self.live_notes_path = self.session_dir / "live_notes.md"
        self.notes_path = self.session_dir / "notes.md"
        self.metadata_path = self.session_dir / "metadata.json"

    @property
    def images_dir(self) -> Path:
        """Session-local folder for images pasted/inserted into notes.

        Markdown references in live_notes.md / notes.md point at this
        directory using the relative path 'images/<filename>'. Created
        on first access so callers can pass it straight to save helpers.
        """
        return images_subdir(self.session_dir)

    # ---- transcript ----
    def append_segments(self, segments: Iterable[TranscriptSegment]) -> None:
        committed = [s for s in segments if not s.is_provisional]
        if not committed:
            return
        lines = [format_segment(s) for s in committed]
        with self.transcript_path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines))
            f.write("\n")

    def write_segments(self, segments: Iterable[TranscriptSegment]) -> None:
        """Overwrite the entire transcript with the given (committed) segments."""
        committed = sorted(
            (s for s in segments if not s.is_provisional),
            key=lambda s: s.t_start,
        )
        text = "\n".join(format_segment(s) for s in committed)
        if text:
            text += "\n"
        self.transcript_path.write_text(text, encoding="utf-8")

    def read_transcript(self) -> str:
        if not self.transcript_path.exists():
            return ""
        return self.transcript_path.read_text(encoding="utf-8")

    # ---- live notes (user-authored, in parallel with the recording) ----
    def read_live_notes(self) -> str:
        """Return live_notes.md, seeding the template if the file is missing."""
        if not self.live_notes_path.exists():
            body = live_notes_seed_body()
            self.live_notes_path.write_text(body, encoding="utf-8")
            return body
        return self.live_notes_path.read_text(encoding="utf-8")

    def save_live_notes(self, body: str) -> None:
        """Overwrite live_notes.md. No archive -- this is the user's running buffer."""
        self.live_notes_path.write_text(body, encoding="utf-8")

    # ---- notes (LLM-synthesized, paste-back) ----
    def save_notes(self, body: str, *, archive_existing: bool = True) -> Optional[Path]:
        """Write notes.md. If a prior file exists, optionally archive it.

        Returns the archive path if archiving happened, else None.
        """
        archive_path: Optional[Path] = None
        if (
            archive_existing
            and self.notes_path.exists()
            and self.notes_path.read_text(encoding="utf-8").strip()
        ):
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
            archive_path = self.session_dir / f"notes-{stamp}.md"
            counter = 1
            while archive_path.exists():
                archive_path = self.session_dir / f"notes-{stamp}-{counter}.md"
                counter += 1
            self.notes_path.rename(archive_path)
        self.notes_path.write_text(body, encoding="utf-8")
        return archive_path

    def read_notes(self) -> str:
        return self.notes_path.read_text(encoding="utf-8") if self.notes_path.exists() else ""

    def list_previous_notes(self) -> list[Path]:
        return sorted(self.session_dir.glob("notes-*.md"), reverse=True)

    def restore_previous_notes(self, archive_path: Path) -> Optional[Path]:
        """Replace notes.md with the contents of an archived version.

        The current notes.md is archived first (same mechanism as
        save_notes), so the operation is fully reversible. Raises
        FileNotFoundError if the archive doesn't exist or isn't in
        this session's directory.
        """
        archive_path = Path(archive_path)
        if archive_path.parent != self.session_dir:
            raise ValueError(
                f"archive path {archive_path} is not in session dir "
                f"{self.session_dir}"
            )
        if not archive_path.exists():
            raise FileNotFoundError(str(archive_path))
        body = archive_path.read_text(encoding="utf-8")
        return self.save_notes(body, archive_existing=True)

    def delete_previous_notes(self, archive_path: Path) -> None:
        """Remove an archived notes-*.md file. Refuses to delete files
        outside the session directory or the live notes.md itself.

        Raises FileNotFoundError if the path doesn't exist; ValueError
        if it points outside the session dir or at notes.md."""
        archive_path = Path(archive_path)
        if archive_path.parent != self.session_dir:
            raise ValueError(
                f"refusing to delete {archive_path}: not in {self.session_dir}"
            )
        if archive_path == self.notes_path:
            raise ValueError("refusing to delete live notes.md via this API")
        if not archive_path.name.startswith("notes-") or not archive_path.name.endswith(".md"):
            raise ValueError(
                f"refusing to delete {archive_path.name}: doesn't match "
                "the notes-*.md archive pattern"
            )
        archive_path.unlink()

    # ---- metadata ----
    def write_metadata(self, meta: dict) -> None:
        self.metadata_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def read_metadata(self) -> dict:
        if not self.metadata_path.exists():
            return {}
        try:
            return json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
