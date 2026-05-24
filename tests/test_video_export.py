"""Pure-Python tests for audio/video_export helpers.

The full MP4 render exercises libx264 + AAC encoders through PyAV and
isn't worth pinning in a unit test (slow, environment-dependent). What
IS worth pinning is the transcript-to-SRT conversion and the letterbox
scale, since both are pure transforms with sharp edge cases (cue end
timestamps, aspect-ratio preservation, exact output dimensions).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from meeting_notetaker.audio.video_export import (
    _ms_to_srt,
    _scale_screenshot_letterbox,
    _transcript_to_srt,
)


def test_ms_to_srt_zero():
    assert _ms_to_srt(0) == "00:00:00,000"


def test_ms_to_srt_millis():
    assert _ms_to_srt(1) == "00:00:00,001"
    assert _ms_to_srt(123) == "00:00:00,123"
    assert _ms_to_srt(999) == "00:00:00,999"


def test_ms_to_srt_seconds():
    assert _ms_to_srt(1_000) == "00:00:01,000"
    assert _ms_to_srt(59_500) == "00:00:59,500"


def test_ms_to_srt_minutes_and_hours():
    assert _ms_to_srt(60_000) == "00:01:00,000"
    assert _ms_to_srt(3_600_000) == "01:00:00,000"
    assert _ms_to_srt(3_723_456) == "01:02:03,456"


def test_transcript_to_srt_basic():
    transcript = (
        "[00:00:03] Alex: hello\n"
        "[00:00:11] Bob: hi there\n"
        "[00:00:30] Alex: continuing\n"
    )
    srt = _transcript_to_srt(transcript)
    # Three cues, indices 1/2/3, blank line terminator. First cue ends
    # at the next cue's start (8 s gap == max-cue cap, so the min ties).
    assert srt.startswith("1\n00:00:03,000 --> 00:00:11,000\nAlex: hello\n")
    assert "2\n00:00:11,000 --> 00:00:19,000\nBob: hi there\n" in srt
    # Final cue runs to start + max-cue (8 s default cap).
    assert "3\n00:00:30,000 --> 00:00:38,000\nAlex: continuing\n" in srt


def test_transcript_to_srt_caps_cue_to_max_duration():
    """A long gap to the next cue is capped at the per-cue ceiling."""
    transcript = (
        "[00:00:00] One\n"
        "[00:05:00] Two\n"  # 5-minute gap -> capped at 8 s
    )
    srt = _transcript_to_srt(transcript)
    assert "00:00:00,000 --> 00:00:08,000" in srt
    # Second cue ends at +8s (final-cue cap).
    assert "00:05:00,000 --> 00:05:08,000" in srt


def test_transcript_to_srt_uses_next_start_when_short_gap():
    """A short gap (less than max cue) ends the cue at the next start."""
    transcript = (
        "[00:00:00] One\n"
        "[00:00:02] Two\n"  # 2-second gap -> cue ends at 00:02
    )
    srt = _transcript_to_srt(transcript)
    assert "00:00:00,000 --> 00:00:02,000" in srt


def test_transcript_to_srt_skips_lines_without_timestamp():
    transcript = (
        "Status: recording finished\n"
        "\n"
        "[00:00:05] Alex: hello\n"
        "(silence)\n"
    )
    srt = _transcript_to_srt(transcript)
    # Only one numbered cue.
    assert srt.startswith("1\n")
    assert "2\n" not in srt
    assert "Alex: hello" in srt
    # Skipped lines do not appear as cue text.
    assert "Status" not in srt
    assert "(silence)" not in srt


def test_transcript_to_srt_empty():
    assert _transcript_to_srt("") == ""


def test_transcript_to_srt_handles_blank_text_after_timestamp():
    """Timestamp-only lines (no text) become empty cues, not crashes."""
    srt = _transcript_to_srt("[00:00:01]\n[00:00:05] real text\n")
    assert "00:00:01,000 --> 00:00:05,000" in srt
    assert "00:00:05,000 --> 00:00:13,000\nreal text" in srt


def test_scale_screenshot_letterbox_wider_than_target(tmp_path: Path):
    """An ultra-wide source gets letterbox bars top + bottom, never
    stretched. Output must be exactly the target 1920x1080."""
    pytest.importorskip("PIL")
    from PIL import Image  # noqa: PLC0415

    src = tmp_path / "wide.png"
    Image.new("RGB", (3840, 1080), (200, 100, 50)).save(str(src))
    out = _scale_screenshot_letterbox(src)
    assert out.shape == (1080, 1920, 3)
    # Source is 2x as wide as target aspect -> scale 0.5 -> 1920x540
    # centered vertically with 270 px black bars top + bottom.
    top_row_pixel = out[0, 960]
    middle_row_pixel = out[540, 960]
    assert tuple(top_row_pixel) == (0, 0, 0), "top should be letterbox"
    # Middle should be the source colour (allow slight LANCZOS rounding).
    assert middle_row_pixel[0] > 150


def test_scale_screenshot_letterbox_taller_than_target(tmp_path: Path):
    pytest.importorskip("PIL")
    from PIL import Image  # noqa: PLC0415

    src = tmp_path / "tall.png"
    Image.new("RGB", (1080, 2160), (50, 100, 200)).save(str(src))
    out = _scale_screenshot_letterbox(src)
    assert out.shape == (1080, 1920, 3)
    # Source is 0.5x as wide as target aspect -> scale 0.5 -> 540x1080
    # centered horizontally with ~690 px black bars left + right.
    left_pixel = out[540, 0]
    middle_pixel = out[540, 960]
    assert tuple(left_pixel) == (0, 0, 0)
    assert middle_pixel[2] > 150  # blue channel dominant


def test_scale_screenshot_letterbox_matching_aspect(tmp_path: Path):
    """A 16:9 source fills the frame edge-to-edge, no bars."""
    pytest.importorskip("PIL")
    from PIL import Image  # noqa: PLC0415

    src = tmp_path / "match.png"
    Image.new("RGB", (1280, 720), (40, 200, 80)).save(str(src))
    out = _scale_screenshot_letterbox(src)
    assert out.shape == (1080, 1920, 3)
    # Every corner should carry the source colour, not black, since
    # the source fills the canvas.
    for y, x in [(0, 0), (0, 1919), (1079, 0), (1079, 1919)]:
        assert out[y, x][1] > 100, f"({y}, {x}) should not be letterbox"
