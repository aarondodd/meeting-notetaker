"""Transcript segments + on-disk I/O.

Per-session layout under <session_dir>/:

    raw.transcript.md          interleaved transcript, source-labeled, time-stamped
    notes.md                   latest LLM-generated notes
    notes-YYYYMMDD-HHMM.md     archived prior notes (older first)
    metadata.json              denormalized session metadata cache
    audio/{mic,sys}.wav        raw capture (deleted after transcription unless retained)
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from ..utils.paths import session_dir


MIC = "mic"
SYS = "sys"
ALL_SOURCES = (MIC, SYS)


@dataclass
class TranscriptSegment:
    source: str            # 'mic' or 'sys'
    text: str
    t_start: float         # seconds since session start
    t_end: float
    is_provisional: bool = False

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
        )


def label_for(source: str) -> str:
    return {"mic": "Me", "sys": "Them"}.get(source, source.upper())


def format_segment(seg: TranscriptSegment) -> str:
    h, rem = divmod(int(seg.t_start), 3600)
    m, s = divmod(rem, 60)
    return f"[{h:02d}:{m:02d}:{s:02d}] {label_for(seg.source)}: {seg.text.strip()}"


class TranscriptStore:
    """Persists committed segments to raw.transcript.md and manages notes files."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.session_dir = session_dir(session_id)
        self.transcript_path = self.session_dir / "raw.transcript.md"
        self.notes_path = self.session_dir / "notes.md"
        self.metadata_path = self.session_dir / "metadata.json"

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

    # ---- notes ----
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
