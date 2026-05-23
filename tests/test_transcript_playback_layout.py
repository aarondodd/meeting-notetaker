"""SessionView Transcript pane: idle <-> playback layout swap.

The Transcript tab carries two layouts inside a QStackedWidget. The
swap is driven by set_player_is_playing(); the same _transcript_view
QPlainTextEdit gets re-parented between the two layouts so the
highlight / scroll state survives the swap.

Pin the swap contract, the sticky-image lookup that drives the top
pane, and the "no screenshots -> stay in idle" guardrail.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QColor, QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.models.session import Session, STATE_COMPLETE  # noqa: E402
from meeting_notetaker.ui.session_view import SessionView  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _make_png(path: Path) -> None:
    img = QImage(400, 200, QImage.Format.Format_RGB32)
    img.fill(QColor(100, 150, 200))
    img.save(str(path), "PNG")


def _seed_session(view: SessionView, tmp_path: Path, *, with_screenshots: bool):
    session = Session(
        id="s1", title="Test", state=STATE_COMPLETE,
        created_at=datetime(2026, 5, 24, 14, 30, 0, tzinfo=timezone.utc).isoformat(),
        started_at="2026-05-24T14:30:00Z",
        has_transcript=True, has_audio=True,
    )
    transcript = (
        "[00:00:03] Alex: hello\n"
        "[00:00:11] Bob: hi there\n"
        "[00:00:30] Alex: continuing\n"
    )
    view.set_session(
        session, transcript=transcript, notes="", previous_notes_paths=[],
    )
    if with_screenshots:
        p1 = tmp_path / "0001-20260524T143005Z.png"
        p2 = tmp_path / "0002-20260524T143012Z.png"
        _make_png(p1)
        _make_png(p2)
        # Offsets: 5s and 12s.
        view.set_screenshot_offsets([(p1, 5_000), (p2, 12_000)])
    else:
        view.set_screenshot_offsets([])


def test_starts_in_idle_layout(qt_app, tmp_path):
    view = SessionView()
    _seed_session(view, tmp_path, with_screenshots=True)
    assert not view._is_in_playback_layout()  # noqa: SLF001


def test_set_player_is_playing_enters_playback_when_screenshots_present(
    qt_app, tmp_path,
):
    view = SessionView()
    _seed_session(view, tmp_path, with_screenshots=True)
    view.set_player_is_playing(True)
    assert view._is_in_playback_layout()  # noqa: SLF001


def test_set_player_is_playing_skips_playback_layout_when_no_screenshots(
    qt_app, tmp_path,
):
    """No screenshots -> the playback layout would be an empty pane.
    Stay in idle layout; audio still plays in MainApp."""
    view = SessionView()
    _seed_session(view, tmp_path, with_screenshots=False)
    view.set_player_is_playing(True)
    assert not view._is_in_playback_layout()  # noqa: SLF001


def test_pause_reverts_to_idle_layout(qt_app, tmp_path):
    view = SessionView()
    _seed_session(view, tmp_path, with_screenshots=True)
    view.set_player_is_playing(True)
    view.set_player_is_playing(False)
    assert not view._is_in_playback_layout()  # noqa: SLF001


def test_position_drives_playback_top_image(qt_app, tmp_path):
    """In playback layout, set_player_position_ms swaps the top
    image to the sticky-latest screenshot."""
    view = SessionView()
    _seed_session(view, tmp_path, with_screenshots=True)
    view.set_player_is_playing(True)
    # Before the first screenshot -> top pane is empty.
    view.set_player_position_ms(3_000)
    assert not view._playback_image.has_image()  # noqa: SLF001
    # After the first screenshot -> first image.
    view.set_player_position_ms(7_000)
    assert view._playback_image.has_image()  # noqa: SLF001
    first_path = view._current_playback_screenshot  # noqa: SLF001
    # After the second -> second image; current_playback_screenshot
    # updates.
    view.set_player_position_ms(20_000)
    assert view._current_playback_screenshot != first_path  # noqa: SLF001


def test_leaving_playback_clears_top_image(qt_app, tmp_path):
    view = SessionView()
    _seed_session(view, tmp_path, with_screenshots=True)
    view.set_player_is_playing(True)
    view.set_player_position_ms(8_000)
    assert view._playback_image.has_image()  # noqa: SLF001
    view.set_player_is_playing(False)
    assert not view._playback_image.has_image()  # noqa: SLF001


def test_screenshot_rail_visible_when_anchors_present(qt_app, tmp_path):
    view = SessionView()
    _seed_session(view, tmp_path, with_screenshots=True)
    assert view._screenshot_rail.isVisible() or not view._screenshot_rail.isHidden()  # noqa: SLF001


def test_screenshot_rail_hidden_without_screenshots(qt_app, tmp_path):
    view = SessionView()
    _seed_session(view, tmp_path, with_screenshots=False)
    assert view._screenshot_rail.isHidden()  # noqa: SLF001


def test_click_pins_highlight_during_lead_in(qt_app, tmp_path):
    """Clicking a line pins the highlight to that line until playback
    catches up, so the 10s seek lead-in doesn't drag the highlight
    backward to an earlier segment."""
    view = SessionView()
    _seed_session(view, tmp_path, with_screenshots=False)
    # Segments are at 3s, 11s, 30s (block_numbers 0, 1, 2).
    # User clicks line 1 (11_000 ms). Expected: pin highlight to
    # block 1; seek emits 1_000 (11_000 - 10_000).
    captured_seeks: list[tuple[str, int]] = []
    view.transcript_seek_ms_requested.connect(
        lambda sid, ms: captured_seeks.append((sid, ms))
    )
    view._on_transcript_line_clicked(1)  # noqa: SLF001
    assert captured_seeks[-1][1] == 1_000
    assert view._current_highlight_block == 1  # noqa: SLF001
    assert view._pinned_highlight_block == 1  # noqa: SLF001
    # While playback is still in the lead-in window, position-driven
    # highlight is suppressed -- the clicked line stays highlighted.
    view.set_player_position_ms(2_000)
    assert view._current_highlight_block == 1  # noqa: SLF001
    # Once playback reaches the clicked line's start, the pin
    # releases and the auto-highlight resumes.
    view.set_player_position_ms(12_000)
    assert view._pinned_highlight_block is None  # noqa: SLF001
    assert view._current_highlight_block == 1  # noqa: SLF001 (still on block 1 because position is within its range)
