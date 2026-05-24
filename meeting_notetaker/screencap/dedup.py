"""Perceptual-hash dedup for the auto-capture path.

dHash (difference hash) is a tiny perceptual fingerprint used to
decide whether two consecutive auto-captured screenshots are
"different enough" to keep. The hash is a 64-bit int; the Hamming
distance between two hashes is a robust similarity score
unaffected by minor brightness shifts, recompression artifacts,
cursor movement, or subtle UI animation.

Reference: Neal Krawetz, "Looks Like It" (2011).
We implement a tiny dHash here (no `imagehash` dependency) so the
v0.6.5 ship doesn't pull a new wheel for one helper.

How to use:

    h1 = dhash(image_path_a)
    h2 = dhash(image_path_b)
    if hamming_distance(h1, h2) > threshold:
        # Different enough -- keep the new image.
    else:
        # Too similar -- delete the new image.

A threshold of 0 means "byte-identical"; higher means "more
tolerant of differences". For meeting-screenshare content, 10
out of 64 bits catches incremental slide changes while ignoring
mouse-cursor movement.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


_HASH_SIZE = 8  # 8x8 grid -> 64-bit dHash


def dhash_path(path: Path, *, hash_size: int = _HASH_SIZE) -> Optional[int]:
    """Compute a 64-bit dHash for the image at ``path``.

    Returns None on any I/O / decode failure -- the caller should
    treat that as "couldn't deduplicate; keep the image" rather
    than dropping a screenshot.
    """
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        log.warning("Pillow unavailable; auto-capture dedup disabled")
        return None
    try:
        with Image.open(str(path)) as img:
            return dhash_image(img, hash_size=hash_size)
    except Exception:
        log.exception("dhash_path: failed to read %s", path)
        return None


def dhash_image(img, *, hash_size: int = _HASH_SIZE) -> int:
    """Compute a 64-bit dHash for a PIL Image.

    Converts to grayscale, downsamples to ``(hash_size+1)`` columns
    by ``hash_size`` rows, takes the per-row column differences,
    and packs the boolean grid into a single integer. The shape is
    one bit per pair of horizontally-adjacent pixels.
    """
    from PIL import Image  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    small = img.convert("L").resize(
        (hash_size + 1, hash_size), Image.Resampling.LANCZOS,
    )
    arr = np.asarray(small, dtype=np.int16)
    # Difference: each cell vs the cell to its right.
    diff = arr[:, 1:] > arr[:, :-1]
    # Pack into an int. Row 0 left-to-right, then row 1, etc.
    h = 0
    for v in diff.flatten():
        h = (h << 1) | int(v)
    return h


def hamming_distance(a: int, b: int) -> int:
    """Bit-count of the XOR -- number of bits that differ."""
    return (a ^ b).bit_count() if hasattr(int, "bit_count") else bin(a ^ b).count("1")


def is_dedup_match(
    new_hash: int, baseline_hash: Optional[int], threshold: int,
) -> bool:
    """Return True iff the new image is similar enough to be deduped.

    A None baseline (no prior kept image) always falls through to
    "not a dup" -- the new image is the first one.
    """
    if baseline_hash is None:
        return False
    return hamming_distance(new_hash, baseline_hash) <= threshold
