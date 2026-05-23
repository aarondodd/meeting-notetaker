"""SlidesWidget playback integration.

The Slides tab grows its own player bar in v0.6.5 + drives an auto-
advance of the full-view image from the player's position. Pin both
behaviors and the click-thumbnail-to-seek path.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QColor, QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.ui.slides_widget import SlidesWidget  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _make_png(path: Path) -> None:
    img = QImage(400, 200, QImage.Format.Format_RGB32)
    img.fill(QColor(40, 120, 200))
    img.save(str(path), "PNG")


def _seed(tmp_path: Path, count: int = 3) -> list[Path]:
    paths = []
    for i in range(count):
        p = tmp_path / f"{i+1:04d}-img.png"
        _make_png(p)
        paths.append(p)
    return paths


def test_player_bar_disabled_by_default(qt_app, tmp_path):
    w = SlidesWidget()
    paths = _seed(tmp_path)
    w.set_screenshots(paths)
    assert not w._player_bar._play_btn.isEnabled()  # noqa: SLF001


def test_set_player_enabled_unlocks_bar(qt_app, tmp_path):
    w = SlidesWidget()
    paths = _seed(tmp_path)
    w.set_screenshots(paths)
    w.set_player_total_ms(60_000)
    w.set_player_enabled(True)
    assert w._player_bar._play_btn.isEnabled()  # noqa: SLF001


def test_position_advances_full_view_image(qt_app, tmp_path):
    """In full-view mode, the displayed image follows the player's
    position via the (path, offset_ms) map."""
    w = SlidesWidget()
    paths = _seed(tmp_path, count=3)
    w.set_screenshots(paths)
    # Offsets at 0s / 10s / 20s.
    w.set_screenshot_offsets([
        (paths[0], 0),
        (paths[1], 10_000),
        (paths[2], 20_000),
    ])
    w._show_full_for(0)  # noqa: SLF001 -- enter full-view at first image
    assert w._current_index == 0  # noqa: SLF001
    # Crossing the 10s threshold advances to image #2.
    w.set_player_position_ms(12_000)
    assert w._current_index == 1  # noqa: SLF001
    # Crossing 20s advances to image #3.
    w.set_player_position_ms(25_000)
    assert w._current_index == 2  # noqa: SLF001


def test_position_advances_grid_selection(qt_app, tmp_path):
    """In grid mode, the selection follows playback but no swap to
    full-view occurs -- the user keeps browsing."""
    w = SlidesWidget()
    paths = _seed(tmp_path, count=3)
    w.set_screenshots(paths)
    w.set_screenshot_offsets([
        (paths[0], 0),
        (paths[1], 10_000),
        (paths[2], 20_000),
    ])
    # Stay in grid mode (index 0); the position tick highlights but
    # doesn't switch pages.
    assert w._stack.currentIndex() == 0  # noqa: SLF001
    w.set_player_position_ms(12_000)
    assert w._stack.currentIndex() == 0  # noqa: SLF001
    assert w._list.currentRow() == 1  # noqa: SLF001


def test_click_thumbnail_seeks_player_when_audio_loaded(qt_app, tmp_path):
    w = SlidesWidget()
    paths = _seed(tmp_path, count=3)
    w.set_screenshots(paths)
    w.set_screenshot_offsets([
        (paths[0], 0), (paths[1], 10_000), (paths[2], 20_000),
    ])
    w.set_player_enabled(True)
    captured: list[int] = []
    w.seek_ms_requested.connect(captured.append)
    # Drive _on_thumb_clicked directly with the third item.
    w._on_thumb_clicked(w._list.item(2))  # noqa: SLF001
    assert captured == [20_000]


def test_click_thumbnail_no_seek_when_audio_unavailable(qt_app, tmp_path):
    """No retained audio -> the click is browse-only. No seek."""
    w = SlidesWidget()
    paths = _seed(tmp_path, count=2)
    w.set_screenshots(paths)
    w.set_screenshot_offsets([(paths[0], 0), (paths[1], 10_000)])
    # set_player_enabled(False) is the default; explicit for clarity.
    w.set_player_enabled(False)
    captured: list[int] = []
    w.seek_ms_requested.connect(captured.append)
    w._on_thumb_clicked(w._list.item(1))  # noqa: SLF001
    assert captured == []


def test_player_bar_forwards_play_pause_signals(qt_app, tmp_path):
    w = SlidesWidget()
    w.set_screenshots(_seed(tmp_path))
    w.set_player_total_ms(60_000)
    w.set_player_enabled(True)
    fires_play: list[None] = []
    fires_pause: list[None] = []
    w.play_clicked.connect(lambda: fires_play.append(None))
    w.pause_clicked.connect(lambda: fires_pause.append(None))
    w._player_bar.set_is_playing(False)  # noqa: SLF001
    w._player_bar._play_btn.click()  # noqa: SLF001
    assert fires_play == [None]
    w._player_bar.set_is_playing(True)  # noqa: SLF001
    w._player_bar._play_btn.click()  # noqa: SLF001
    assert fires_pause == [None]
