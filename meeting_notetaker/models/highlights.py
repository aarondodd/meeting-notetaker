"""Per-session highlight markers + persistence.

A "highlight" is a (start_ms, end_ms, optional title) range over the
session's playback timeline. Multiple non-overlapping highlights can
accumulate; they survive close/reopen via
`<session_dir>/highlights.json`.

Used by the highlight-export pipeline (Issue #26): the user marks
sections of a meeting they care about, then exports either an MP4
slideshow or an MP3/Opus/etc audio file built from just those
sections (with title + jump interstitials separating them).

On-disk shape (sample):

```json
{
  "highlights": [
    {"start_ms": 65000, "end_ms": 92000, "title": "Decision on MDM"},
    {"start_ms": 1820000, "end_ms": 1855000, "title": ""}
  ]
}
```

Empty title = "Highlight N" by index at render time. The export
pipeline never mutates this file -- it's the source of truth for
the user's marker state and only the highlight-bar widget writes
it.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Iterable, Optional

from ..utils.paths import session_dir


HIGHLIGHTS_FILENAME = "highlights.json"


@dataclass(frozen=True)
class Highlight:
    """One (start, end, optional title) range. Frozen so list
    mutators always replace whole items -- no in-place edits sneak
    past validation."""
    start_ms: int
    end_ms: int
    title: str = ""

    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)

    def overlaps(self, other: "Highlight") -> bool:
        """Inclusive end check matches what users intuitively
        consider overlap -- a region ending exactly where the next
        begins is treated as adjacent (no overlap)."""
        return not (self.end_ms <= other.start_ms or self.start_ms >= other.end_ms)


@dataclass
class HighlightSet:
    """Mutable collection -- the on-disk JSON's `highlights` array."""
    highlights: list[Highlight] = field(default_factory=list)

    def sorted_by_start(self) -> list[Highlight]:
        """Always-current sort order for rendering + export. The
        store keeps highlights in insertion order on disk so a
        user undo+redo would preserve the user's add sequence; the
        UI + export always view them in time order."""
        return sorted(self.highlights, key=lambda h: h.start_ms)

    def total_duration_ms(self) -> int:
        return sum(h.duration_ms() for h in self.highlights)


# ----------------------------------------------------------------------
# Validation


def validate_range(
    start_ms: int,
    end_ms: int,
    *,
    total_duration_ms: Optional[int] = None,
) -> None:
    """Raise ValueError for a clearly-broken range.

    Tolerates 0-duration ranges (a user might mark and immediately
    cancel without setting end -- the bar widget enforces non-zero
    via its toggle semantic). `total_duration_ms`, if given, clamps
    end_ms to the audio length.
    """
    if start_ms < 0:
        raise ValueError(f"start_ms must be >= 0 (got {start_ms})")
    if end_ms < start_ms:
        raise ValueError(
            f"end_ms ({end_ms}) must be >= start_ms ({start_ms})"
        )
    if total_duration_ms is not None and end_ms > total_duration_ms:
        raise ValueError(
            f"end_ms ({end_ms}) exceeds total audio duration "
            f"({total_duration_ms})"
        )


def has_overlap_with_existing(
    new: Highlight,
    existing: Iterable[Highlight],
) -> bool:
    """True if `new` overlaps any item in `existing`. Used by the
    bar widget to refuse a Start/End toggle that would carve into
    an already-marked region."""
    return any(new.overlaps(h) for h in existing)


# ----------------------------------------------------------------------
# Persistence


class HighlightsStore:
    """Thin JSON-backed wrapper. Stays human-readable on disk so a
    user can sanity-check / hand-edit if they want to."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.path = session_dir(session_id) / HIGHLIGHTS_FILENAME

    def load(self) -> HighlightSet:
        if not self.path.exists():
            return HighlightSet()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt file shouldn't lose the session entirely;
            # start fresh and let the user re-mark.
            return HighlightSet()
        raw_list = data.get("highlights") if isinstance(data, dict) else None
        if not isinstance(raw_list, list):
            return HighlightSet()
        items: list[Highlight] = []
        for entry in raw_list:
            if not isinstance(entry, dict):
                continue
            try:
                start = int(entry.get("start_ms", -1))
                end = int(entry.get("end_ms", -1))
            except (TypeError, ValueError):
                continue
            if start < 0 or end < start:
                continue
            title = str(entry.get("title", "") or "")
            items.append(Highlight(start_ms=start, end_ms=end, title=title))
        return HighlightSet(highlights=items)

    def save(self, hs: HighlightSet) -> None:
        body = {"highlights": [asdict(h) for h in hs.highlights]}
        self.path.write_text(
            json.dumps(body, indent=2), encoding="utf-8",
        )

    def delete_all(self) -> None:
        if self.path.exists():
            try:
                self.path.unlink()
            except OSError:
                # Cheap recovery: empty out the file so a future
                # load() returns an empty set.
                self.save(HighlightSet())


# ----------------------------------------------------------------------
# Bar-widget mutators (intent-level, not just CRUD)


def add_highlight(
    hs: HighlightSet,
    start_ms: int,
    end_ms: int,
    *,
    title: str = "",
    total_duration_ms: Optional[int] = None,
) -> Highlight:
    """Validate + append + return the new highlight.

    Raises ValueError if the range is invalid OR overlaps any
    existing highlight. The bar widget catches this and surfaces
    "highlights cannot overlap" to the user.
    """
    validate_range(start_ms, end_ms, total_duration_ms=total_duration_ms)
    h = Highlight(start_ms=int(start_ms), end_ms=int(end_ms), title=title)
    if has_overlap_with_existing(h, hs.highlights):
        raise ValueError(
            "highlights cannot overlap an existing region"
        )
    hs.highlights.append(h)
    return h


def remove_highlight(hs: HighlightSet, highlight: Highlight) -> bool:
    """Remove the first highlight equal to `highlight`. Returns
    True when one was removed."""
    for i, h in enumerate(hs.highlights):
        if h == highlight:
            del hs.highlights[i]
            return True
    return False


def update_highlight_title(
    hs: HighlightSet,
    highlight: Highlight,
    new_title: str,
) -> Optional[Highlight]:
    """Swap the title on an existing highlight. Returns the new
    highlight (since Highlight is frozen) or None when the source
    isn't in the set."""
    for i, h in enumerate(hs.highlights):
        if h == highlight:
            updated = replace(h, title=new_title)
            hs.highlights[i] = updated
            return updated
    return None


def update_highlight_range(
    hs: HighlightSet,
    highlight: Highlight,
    start_ms: int,
    end_ms: int,
    *,
    total_duration_ms: Optional[int] = None,
) -> Optional[Highlight]:
    """Move an existing highlight's start/end. Validates against
    other highlights (excluding `highlight` itself) so a user can
    grow a region into space that wasn't reserved by anyone else."""
    validate_range(start_ms, end_ms, total_duration_ms=total_duration_ms)
    for i, h in enumerate(hs.highlights):
        if h == highlight:
            candidate = Highlight(
                start_ms=int(start_ms),
                end_ms=int(end_ms),
                title=h.title,
            )
            others = [x for j, x in enumerate(hs.highlights) if j != i]
            if has_overlap_with_existing(candidate, others):
                raise ValueError(
                    "updated range overlaps another highlight"
                )
            hs.highlights[i] = candidate
            return candidate
    return None
