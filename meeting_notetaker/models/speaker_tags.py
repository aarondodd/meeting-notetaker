"""Per-session speaker tags captured by the user during recording.

The user clicks an attendee name in the right sidebar while that person
is talking. Each click captures (name, t_seconds) where `t_seconds` is
the recording-active elapsed time (i.e. WAV-aligned: pause-time is
excluded). After Stop, the diarization refiner consumes these tags to
constrain the clusterer (must-link same-name turns, cannot-link
different-name turns) and to auto-name the resulting clusters.

Storage: `<session_dir>/speaker_tags.json` -- a small JSON document
that is rewritten in full on each mutation. The file is small (a few
dozen entries per meeting at most) so atomic-replace via os.replace
is sufficient; no SQLite needed.

JSON shape:

    {
      "version": 1,
      "tags": [
        {"name": "Pat", "t_seconds": 342.1},
        {"name": "Sam", "t_seconds": 401.7},
        ...
      ]
    }
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Optional


log = logging.getLogger(__name__)

FILENAME = "speaker_tags.json"
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SpeakerTag:
    name: str
    t_seconds: float


class SpeakerTagStore:
    """Append-on-disk store for one session's speaker tags.

    The session directory is created lazily by the controller; this
    store does NOT create directories. Reads tolerate a missing file
    (returns an empty list). Writes are atomic (write to temp then
    rename) so a crash mid-write cannot corrupt the on-disk JSON.
    """

    def __init__(self, session_dir: Path) -> None:
        self.session_dir = Path(session_dir)
        self.path = self.session_dir / FILENAME

    def load(self) -> list[SpeakerTag]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.exception("speaker tag file unreadable; treating as empty: %s", self.path)
            return []
        tags_raw = data.get("tags") if isinstance(data, dict) else None
        if not isinstance(tags_raw, list):
            return []
        out: list[SpeakerTag] = []
        for item in tags_raw:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            t = item.get("t_seconds")
            if not isinstance(name, str) or not name.strip():
                continue
            if not isinstance(t, (int, float)) or t < 0:
                continue
            out.append(SpeakerTag(name=name.strip(), t_seconds=float(t)))
        return out

    def append(self, tag: SpeakerTag) -> None:
        if not tag.name.strip():
            raise ValueError("speaker tag name cannot be empty")
        if tag.t_seconds < 0:
            raise ValueError("speaker tag t_seconds cannot be negative")
        existing = self.load()
        existing.append(SpeakerTag(name=tag.name.strip(), t_seconds=float(tag.t_seconds)))
        self._save(existing)

    def remove_last_for(self, name: str) -> bool:
        """Drop the most recent tag for `name`. Returns True if one was removed."""
        target = name.strip().lower()
        if not target:
            return False
        existing = self.load()
        for i in range(len(existing) - 1, -1, -1):
            if existing[i].name.lower() == target:
                existing.pop(i)
                self._save(existing)
                return True
        return False

    def counts(self) -> dict[str, int]:
        """Number of tags per name. Names are kept in their stored case."""
        out: dict[str, int] = {}
        for t in self.load():
            out[t.name] = out.get(t.name, 0) + 1
        return out

    def _save(self, tags: Iterable[SpeakerTag]) -> None:
        payload = {
            "version": SCHEMA_VERSION,
            "tags": [asdict(t) for t in tags],
        }
        self.session_dir.mkdir(parents=True, exist_ok=True)
        # Atomic write: temp file in same dir + os.replace.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(self.session_dir),
            prefix=".speaker_tags-", suffix=".json", delete=False,
        ) as tf:
            json.dump(payload, tf, ensure_ascii=False, indent=2)
            tmp_path = tf.name
        os.replace(tmp_path, self.path)


def load_tags(session_dir: Path) -> list[SpeakerTag]:
    """Convenience: load tags for a session without constructing a store."""
    return SpeakerTagStore(session_dir).load()
