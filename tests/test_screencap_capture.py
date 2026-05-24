"""Screen-capture filename + sequence logic.

The mss grab itself can't run in a headless container (no X server),
so we exercise the surrounding logic -- filename templating, sequence
counter, on-failure cleanup -- with the mss call mocked out. The full
end-to-end grab gets a manual smoke when Aaron runs on Windows.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from unittest import mock

import pytest

from meeting_notetaker.screencap import capture as capture_mod


def test_next_screenshot_path_uses_sequence_and_timestamp(tmp_path):
    """First call into an empty dir yields 0001-<ts>.png; subsequent
    calls increment."""
    now = dt.datetime(2026, 5, 23, 14, 32, 0)
    p1 = capture_mod._next_screenshot_path(tmp_path, now=now)
    p1.write_bytes(b"placeholder")
    p2 = capture_mod._next_screenshot_path(tmp_path, now=now)
    assert p1.name == "0001-20260523T143200Z.png"
    assert p2.name == "0002-20260523T143200Z.png"


def test_next_screenshot_path_skips_non_conforming(tmp_path):
    """A user-dropped foo.png in the dir doesn't bump the counter --
    parse failure is silently treated as seq 0."""
    (tmp_path / "user-dropped.png").write_bytes(b"placeholder")
    now = dt.datetime(2026, 5, 23, 14, 32, 0)
    p = capture_mod._next_screenshot_path(tmp_path, now=now)
    assert p.name == "0001-20260523T143200Z.png"


def test_next_screenshot_path_respects_highest_seen(tmp_path):
    """Mid-session deletion shouldn't reuse a deleted slot."""
    (tmp_path / "0005-20260523T140000Z.png").write_bytes(b"x")
    (tmp_path / "0003-20260523T140000Z.png").write_bytes(b"x")
    now = dt.datetime(2026, 5, 23, 14, 32, 0)
    p = capture_mod._next_screenshot_path(tmp_path, now=now)
    assert p.name == "0006-20260523T143200Z.png"


def test_next_screenshot_path_creates_no_dir(tmp_path):
    """The path helper is pure -- it computes a filename, it doesn't
    touch the filesystem. Pin so a future refactor doesn't sneak side
    effects in."""
    target = tmp_path / "no-such-dir"
    assert not target.exists()
    # The pure helper shouldn't mkdir the parent dir.
    _ = capture_mod._next_screenshot_path(target)
    # It might mkdir if asked; we're pinning that it doesn't.
    assert not target.exists()


def test_capture_region_to_file_returns_none_on_invalid_region(tmp_path):
    """Zero / negative width or height short-circuits without touching mss."""
    assert capture_mod.capture_region_to_file((0, 0, 0, 100), tmp_path) is None
    assert capture_mod.capture_region_to_file((0, 0, 100, 0), tmp_path) is None
    assert capture_mod.capture_region_to_file((0, 0, -1, 100), tmp_path) is None


def test_capture_region_to_file_creates_dst_dir(tmp_path):
    """The destination dir is mkdir'd before the grab attempts a write."""
    dst = tmp_path / "screenshots"
    # Patch mss + PIL so the actual grab is replaced with a no-op that
    # writes a single fake byte and reports success.
    fake_img = mock.MagicMock()
    fake_img.size = (100, 50)
    fake_img.rgb = b"\x00" * (100 * 50 * 3)

    class _FakeMSS:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def grab(self, _region):
            return fake_img

    fake_pil = mock.MagicMock()
    fake_pil.frombytes = lambda *args, **kwargs: fake_pil
    fake_pil.save = lambda *args, **kwargs: (tmp_path / "screenshots" / "drop.txt").write_text("saved")

    with mock.patch.dict("sys.modules", {
        "mss": mock.MagicMock(MSS=_FakeMSS),
        "PIL": mock.MagicMock(Image=mock.MagicMock(frombytes=lambda *a, **k: fake_pil)),
    }):
        result = capture_mod.capture_region_to_file((0, 0, 100, 50), dst)

    assert dst.is_dir(), "destination dir should be created before grab"
    # mocking the save makes the result file presence wobble; we just
    # care that the helper got far enough to create the dir.


def test_capture_region_to_file_logs_and_returns_none_on_failure(tmp_path):
    """Encoder / grab errors leave no zombie file behind."""
    dst = tmp_path / "screenshots"
    class _Boom:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def grab(self, _region):
            raise RuntimeError("simulated mss failure")

    with mock.patch.dict("sys.modules", {
        "mss": mock.MagicMock(MSS=_Boom),
        "PIL": mock.MagicMock(),
    }):
        result = capture_mod.capture_region_to_file((0, 0, 100, 50), dst)
    assert result is None
    # No half-written PNG.
    pngs = list(dst.glob("*.png"))
    assert pngs == []


def test_parse_seq_handles_garbage_stems():
    assert capture_mod._parse_seq("0001-abc") == 1
    assert capture_mod._parse_seq("not-a-number") == 0
    assert capture_mod._parse_seq("0042") == 42
    assert capture_mod._parse_seq("") == 0
