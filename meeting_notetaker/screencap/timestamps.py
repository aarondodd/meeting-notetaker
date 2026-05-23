"""Map captured screenshots to recording-relative timestamps.

Each PNG in <session>/screenshots/ is named
``NNNN-YYYYMMDDTHHMMSSZ.png``: a 4-digit sequence number plus the UTC
moment of capture. The transcript player exposes recording-relative
times (0 = the moment recording started), so we need to subtract
the session's ``started_at`` from each capture's filename
timestamp to align the two clocks.

This module is the seam between screencap/capture and the playback
+ rail UIs. It carries no Qt; the helpers are pure Python so tests
can pin filename / arithmetic behavior without spinning up a
QApplication.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def screenshot_taken_at(path: Path) -> Optional[datetime]:
    """Return the UTC datetime encoded in ``path``'s filename, or None.

    Filenames look like ``0001-20260524T143200Z.png``; we parse off
    the suffix after the first ``-``. Any non-conforming file (e.g.
    one the user dropped in manually) returns None and the caller
    skips it.
    """
    stem = path.stem
    parts = stem.split("-", 1)
    if len(parts) != 2:
        return None
    ts_part = parts[1]
    if len(ts_part) != 16 or not ts_part.endswith("Z"):
        return None
    try:
        return datetime.strptime(ts_part, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_started_at(started_at: Optional[str]) -> Optional[datetime]:
    """Parse a Session.started_at ISO string (UTC, Z-suffixed) into a datetime.

    Returns None on missing / malformed input so the caller can fall
    back to hiding the rail rather than crashing.
    """
    if not started_at:
        return None
    try:
        # Sessions write "YYYY-MM-DDTHH:MM:SSZ"; replace Z with +00:00
        # so fromisoformat (which doesn't accept Z directly until 3.11
        # on every platform we care about) eats it cleanly.
        return datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError:
        return None


def offset_ms(taken_at: datetime, started_at: datetime) -> int:
    """Recording-relative offset from started_at to taken_at, in ms.

    Both inputs should be timezone-aware (UTC). A negative offset
    (capture before recording start, possible if the user captured
    during pre-recording setup) is preserved -- the rail / playback
    pane decides how to render those.
    """
    delta = taken_at - started_at
    return int(delta.total_seconds() * 1000)


def screenshot_offsets(
    paths: list[Path], started_at: Optional[str],
) -> list[tuple[Path, int]]:
    """Return [(path, offset_ms), ...] sorted by offset ascending.

    Filters out non-conforming filenames and (if started_at is None
    or unparseable) the entire list -- the rail isn't meaningful
    without a recording-start anchor.
    """
    start = parse_started_at(started_at)
    if start is None:
        return []
    out: list[tuple[Path, int]] = []
    for p in paths:
        taken = screenshot_taken_at(p)
        if taken is None:
            continue
        out.append((p, offset_ms(taken, start)))
    out.sort(key=lambda t: t[1])
    return out


def match_screenshots_to_blocks(
    screenshot_offsets: list[tuple[Path, int]],
    transcript_blocks: list[tuple[int, int]],
) -> list[tuple[Path, int]]:
    """Anchor each screenshot to the closest transcript block.

    Both inputs are sorted by their ms offset. The anchoring rule is
    "the transcript line whose start_ms is the greatest <= the
    screenshot's offset": that's the line being spoken when the
    capture happened. A screenshot taken before the first transcript
    segment anchors to block 0 (the segment that's about to start).

    Returns [(path, block_number), ...] in the same order as the
    input screenshot list. Screenshots whose offset is negative
    (captured before recording start) anchor to block 0 by the same
    rule -- they all land at the top of the rail.
    """
    if not transcript_blocks:
        return []
    out: list[tuple[Path, int]] = []
    keys = [k for k, _ in transcript_blocks]
    for path, offset in screenshot_offsets:
        import bisect  # noqa: PLC0415
        idx = bisect.bisect_right(keys, offset) - 1
        if idx < 0:
            idx = 0  # Anchor pre-segment screenshots to the first line.
        block = transcript_blocks[idx][1]
        out.append((path, block))
    return out


def current_screenshot_for_position(
    screenshot_offsets: list[tuple[Path, int]], position_ms: int,
) -> Optional[Path]:
    """Sticky-image lookup: the latest screenshot with offset <= position_ms.

    Used by the playback layout to drive the top image. Returns None
    when ``position_ms`` precedes every screenshot -- the caller
    should hide the top pane in that case so the user isn't staring
    at a blank rectangle.
    """
    if not screenshot_offsets:
        return None
    import bisect  # noqa: PLC0415
    keys = [t[1] for t in screenshot_offsets]
    idx = bisect.bisect_right(keys, position_ms) - 1
    if idx < 0:
        return None
    return screenshot_offsets[idx][0]
