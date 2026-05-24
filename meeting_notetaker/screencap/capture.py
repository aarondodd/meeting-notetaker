"""Snapshot a screen region to PNG.

mss handles the platform-specific screen grab; on Windows that's the
GDI BitBlt path (fast, ~5 ms per capture). The grab returns raw BGRA
bytes which we wrap in a PIL Image (no extra copy) and write as PNG
into the session's screenshots/ directory.

Filenames carry a 4-digit sequence number plus a UTC ISO timestamp:

    screenshots/0001-20260523T143200Z.png
    screenshots/0002-20260523T143245Z.png

Sort by filename and you get the chronological capture order. The
sequence number is recomputed from the on-disk directory each call,
so a deletion mid-session doesn't recycle numbers (that would break
the chronological ordering for any inserted-into-notes ref pointing
at the older file).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def capture_region_to_file(
    region: tuple[int, int, int, int],
    dst_dir: Path,
    *,
    now: Optional[datetime] = None,
) -> Optional[Path]:
    """Capture screen rect (left, top, width, height) and save a PNG.

    Returns the saved path, or None on capture failure. The caller is
    expected to surface the error to the user; we just log it here.

    Imports mss lazily so test environments without a display can
    still import the module to test the filename / sequence logic
    without the grab itself.
    """
    dst_dir.mkdir(parents=True, exist_ok=True)
    left, top, width, height = region
    if width <= 0 or height <= 0:
        log.warning(
            "capture_region: invalid region size (%dx%d); skipping",
            width, height,
        )
        return None
    out_path = _next_screenshot_path(dst_dir, now=now)
    try:
        # Lazy import + lazy MSS instance: importing mss on Linux without
        # a display would error if we did it at module import time.
        import mss  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415

        with mss.MSS() as sct:
            raw = sct.grab({
                "left": left, "top": top,
                "width": width, "height": height,
            })
        # mss returns BGRA; PIL.Image.frombytes can read it directly.
        img = Image.frombytes("RGB", raw.size, raw.rgb)
        img.save(str(out_path), format="PNG", optimize=False)
        return out_path
    except Exception:
        log.exception("capture_region: grab failed for %s", out_path.name)
        # Clean up a half-written file if one was produced.
        if out_path.exists():
            try:
                out_path.unlink()
            except OSError:
                pass
        return None


def _next_screenshot_path(dst_dir: Path, *, now: Optional[datetime] = None) -> Path:
    """Compute the next sequenced PNG name in dst_dir.

    Pulled out so tests can pin the timestamp without subclassing the
    mss capture path. Returns dst_dir / "{seq:04d}-{ts}Z.png" where
    seq is one higher than the largest existing seq prefix in dst_dir.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%dT%H%M%S")
    next_seq = _next_sequence(dst_dir)
    return dst_dir / f"{next_seq:04d}-{ts}Z.png"


def _next_sequence(dst_dir: Path) -> int:
    """Scan existing screenshot filenames and return seq+1.

    Tolerates non-conforming filenames (returns 0 for those instead of
    erroring) so a user dropping their own .png in the dir doesn't
    crash the counter.
    """
    if not dst_dir.is_dir():
        return 1
    highest = 0
    for entry in dst_dir.iterdir():
        if entry.suffix.lower() != ".png":
            continue
        seq = _parse_seq(entry.stem)
        if seq > highest:
            highest = seq
    return highest + 1


def _parse_seq(stem: str) -> int:
    """Pull the leading integer from 'NNNN-YYYYMMDDTHHMMSSZ' or return 0."""
    prefix = stem.split("-", 1)[0]
    try:
        return int(prefix)
    except ValueError:
        return 0
