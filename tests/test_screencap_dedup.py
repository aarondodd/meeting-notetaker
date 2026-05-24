"""Perceptual-hash dedup for the auto-capture path.

dHash + Hamming distance: two visually-similar images produce nearly-
identical hashes (small distance); visually-different images produce
distances of dozens of bits out of 64. The auto-capture logic uses
this to decide whether a fresh screenshot is "new enough" to keep
or should be deduped against the most-recently-kept image.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PIL")

from meeting_notetaker.screencap.dedup import (  # noqa: E402
    dhash_image,
    dhash_path,
    hamming_distance,
    is_dedup_match,
)


def _make_image(*, size=(200, 100), fill=(40, 80, 200)):
    from PIL import Image
    img = Image.new("RGB", size, fill)
    return img


def test_dhash_image_returns_64bit_int():
    img = _make_image()
    h = dhash_image(img)
    assert isinstance(h, int)
    # 8x8 grid -> 64 bits at most.
    assert 0 <= h < (1 << 64)


def test_dhash_path_decodes_png(tmp_path):
    p = tmp_path / "x.png"
    _make_image().save(str(p), "PNG")
    h = dhash_path(p)
    assert h is not None
    assert isinstance(h, int)


def test_dhash_path_returns_none_for_missing_file(tmp_path):
    assert dhash_path(tmp_path / "does-not-exist.png") is None


def test_identical_images_have_zero_distance():
    a = _make_image()
    b = _make_image()
    assert hamming_distance(dhash_image(a), dhash_image(b)) == 0


def test_very_different_images_have_large_distance():
    from PIL import Image
    a = _make_image(fill=(0, 0, 0))
    # Solid colors actually all produce 0 hash (no horizontal diff).
    # Use a real gradient pattern to get content variation.
    b = Image.linear_gradient("L").convert("RGB")
    c = Image.radial_gradient("L").convert("RGB")
    # Both gradients should differ meaningfully.
    h_b = dhash_image(b)
    h_c = dhash_image(c)
    assert hamming_distance(h_b, h_c) > 5, (
        f"expected gradient hashes to differ by > 5 bits, got "
        f"{hamming_distance(h_b, h_c)}"
    )


def test_small_perturbations_stay_close():
    """An image with a 1-pixel cursor shift should produce a hash
    very close to the original (typical auto-capture scenario)."""
    from PIL import Image, ImageDraw
    base = Image.linear_gradient("L").convert("RGB").resize((400, 300))
    # Same image with one tiny dot at a known location -- the cursor
    # moving 5 px shouldn't change the global dHash by much.
    shifted = base.copy()
    draw = ImageDraw.Draw(shifted)
    draw.ellipse((100, 100, 110, 110), fill=(0, 0, 0))
    distance = hamming_distance(dhash_image(base), dhash_image(shifted))
    # Tiny perturbation -> small Hamming distance. 8 bits out of
    # 64 is the upper bound for cursor-sized changes against a
    # smooth gradient; real screenshare content (slide text, big
    # blocks) is even more stable.
    assert distance <= 8, (
        f"small perturbation produced distance={distance}; dHash "
        "should be robust against cursor-sized changes"
    )


def test_is_dedup_match_returns_false_for_no_baseline():
    """No previously-kept image -> always keep the first capture."""
    assert is_dedup_match(new_hash=12345, baseline_hash=None, threshold=10) is False


def test_is_dedup_match_returns_true_below_threshold():
    """Hash differs by 3 bits from baseline; threshold=10 -> dedup."""
    a = 0b0000_0000
    b = 0b0000_0111  # 3 bits set, distance = 3
    assert is_dedup_match(b, a, threshold=10) is True


def test_is_dedup_match_returns_false_above_threshold():
    """Hash differs by 12 bits; threshold=10 -> NOT a dup, keep."""
    a = 0b0000_0000
    b = 0b1111_1111_1111  # 12 bits set
    assert is_dedup_match(b, a, threshold=10) is False


def test_is_dedup_match_threshold_zero_only_exact():
    """threshold=0 -> only EXACTLY equal hashes get deduped."""
    assert is_dedup_match(42, 42, threshold=0) is True
    assert is_dedup_match(42, 43, threshold=0) is False
