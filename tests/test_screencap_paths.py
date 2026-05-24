"""session_screenshots_dir + list_screenshots helpers.

The Slides tab + the recording context-menu use list_screenshots to
discover what's saved. The capture path uses session_screenshots_dir
to anchor PNG writes. Pin the contracts.
"""
from __future__ import annotations

from meeting_notetaker.utils.paths import (
    list_screenshots,
    session_dir,
    session_screenshots_dir,
)


def test_session_screenshots_dir_creates_on_demand(isolated_data_dir):
    sid = "s-screen-create"
    path = session_screenshots_dir(sid)
    assert path.exists()
    assert path.name == "screenshots"
    assert path.parent == session_dir(sid)


def test_list_screenshots_empty_when_no_dir(isolated_data_dir):
    """list_screenshots on a session that never had captures returns []."""
    assert list_screenshots("never-captured") == []


def test_list_screenshots_returns_pngs_in_name_order(isolated_data_dir):
    sid = "s-screen-list"
    d = session_screenshots_dir(sid)
    (d / "0003-20260523T143200Z.png").write_bytes(b"png3")
    (d / "0001-20260523T143000Z.png").write_bytes(b"png1")
    (d / "0002-20260523T143100Z.png").write_bytes(b"png2")
    result = list_screenshots(sid)
    names = [p.name for p in result]
    assert names == [
        "0001-20260523T143000Z.png",
        "0002-20260523T143100Z.png",
        "0003-20260523T143200Z.png",
    ]


def test_list_screenshots_ignores_non_png(isolated_data_dir):
    """A user dropping foo.jpg or foo.txt in the dir is skipped."""
    sid = "s-screen-mixed"
    d = session_screenshots_dir(sid)
    (d / "0001-real.png").write_bytes(b"png")
    (d / "notes.txt").write_bytes(b"text")
    (d / "side.jpg").write_bytes(b"jpg")
    result = list_screenshots(sid)
    assert [p.name for p in result] == ["0001-real.png"]
