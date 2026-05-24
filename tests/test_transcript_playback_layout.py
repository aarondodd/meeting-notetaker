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


def test_set_player_position_enters_playback_when_engaged(qt_app, tmp_path):
    """Non-zero position with screenshots present -> swap to playback
    layout so the matching image is visible. Updated in v0.6.5: the
    layout swap is now driven by position, not by 'playing' state --
    this is what makes click-to-seek-while-paused show the right
    image (previously the layout stayed idle and the image stayed
    stuck)."""
    view = SessionView()
    _seed_session(view, tmp_path, with_screenshots=True)
    assert not view._is_in_playback_layout()  # noqa: SLF001 -- initial idle
    view.set_player_position_ms(8_000)
    assert view._is_in_playback_layout()  # noqa: SLF001


def test_set_player_position_skips_playback_layout_when_no_screenshots(
    qt_app, tmp_path,
):
    """No screenshots -> the playback layout would be an empty top
    pane. Stay in idle layout regardless of position."""
    view = SessionView()
    _seed_session(view, tmp_path, with_screenshots=False)
    view.set_player_position_ms(8_000)
    assert not view._is_in_playback_layout()  # noqa: SLF001


def test_pause_keeps_playback_layout(qt_app, tmp_path):
    """v0.6.5 fix: pause/stop keeps the playback layout up so the
    user still sees the current-position screenshot. Only natural
    end-of-playback (via revert_to_idle_layout) reverts."""
    view = SessionView()
    _seed_session(view, tmp_path, with_screenshots=True)
    view.set_player_position_ms(8_000)
    assert view._is_in_playback_layout()  # noqa: SLF001
    view.set_player_is_playing(False)  # user pressed Stop
    assert view._is_in_playback_layout()  # noqa: SLF001 -- stays


def test_revert_to_idle_layout_explicitly(qt_app, tmp_path):
    """Natural-end-of-playback path calls revert_to_idle_layout
    explicitly. Reverts even if position is non-zero."""
    view = SessionView()
    _seed_session(view, tmp_path, with_screenshots=True)
    view.set_player_position_ms(8_000)
    assert view._is_in_playback_layout()  # noqa: SLF001
    view.revert_to_idle_layout()
    assert not view._is_in_playback_layout()  # noqa: SLF001


def test_position_drives_playback_top_image(qt_app, tmp_path):
    """set_player_position_ms swaps the top image to the sticky-
    latest screenshot, regardless of play/pause state."""
    view = SessionView()
    _seed_session(view, tmp_path, with_screenshots=True)
    # Before the first screenshot -> top pane is empty (position
    # gate kicks in below first screenshot's offset).
    view.set_player_position_ms(3_000)
    assert not view._playback_image.has_image()  # noqa: SLF001
    # After the first screenshot -> first image; layout is now
    # playback (position > 0 + screenshots present).
    view.set_player_position_ms(7_000)
    assert view._playback_image.has_image()  # noqa: SLF001
    first_path = view._current_playback_screenshot  # noqa: SLF001
    # After the second -> second image; current_playback_screenshot
    # updates.
    view.set_player_position_ms(20_000)
    assert view._current_playback_screenshot != first_path  # noqa: SLF001


def test_revert_to_idle_clears_top_image(qt_app, tmp_path):
    view = SessionView()
    _seed_session(view, tmp_path, with_screenshots=True)
    view.set_player_position_ms(8_000)
    assert view._playback_image.has_image()  # noqa: SLF001
    view.revert_to_idle_layout()
    assert not view._playback_image.has_image()  # noqa: SLF001


def test_idle_layout_has_no_screenshot_rail(qt_app, tmp_path):
    """v0.6.5: the side-rail thumbnails were removed. Idle layout is
    just the full-width transcript editor (plus the player bar below)."""
    view = SessionView()
    _seed_session(view, tmp_path, with_screenshots=True)
    assert not hasattr(view, "_screenshot_rail")
    # The transcript_view's parent in idle is the idle page (the
    # editor sits directly inside the QVBoxLayout, not behind a
    # splitter).
    assert view._transcript_view.parent() is view._transcript_idle_page  # noqa: SLF001


def test_set_transcript_playback_split_top_pct_clamps(qt_app, tmp_path):
    view = SessionView()
    _seed_session(view, tmp_path, with_screenshots=True)
    view.set_transcript_playback_split_top_pct(5)
    assert view._playback_split_top_pct == 10  # noqa: SLF001 -- clamped low
    view.set_transcript_playback_split_top_pct(95)
    assert view._playback_split_top_pct == 90  # noqa: SLF001 -- clamped high
    view.set_transcript_playback_split_top_pct(55)
    assert view._playback_split_top_pct == 55  # noqa: SLF001


def test_playback_split_default_is_seventy_thirty(qt_app, tmp_path):
    """Default split is 70% top (screenshot) / 30% bottom (transcript)."""
    view = SessionView()
    _seed_session(view, tmp_path, with_screenshots=True)
    # Default before MainApp pushes a config value is 70.
    assert view._playback_split_top_pct == 70  # noqa: SLF001


def test_playback_split_applies_to_splitter_sizes(qt_app, tmp_path):
    """Force a known splitter height + apply pct = visible sizes adjust.

    The playback splitter has 3 widgets while in playback (image,
    editor, hidden placeholder). Only the first two carry visible
    pixels; the third stays at zero.
    """
    view = SessionView()
    _seed_session(view, tmp_path, with_screenshots=True)
    view.set_player_position_ms(8_000)  # enter playback layout
    splitter = view._transcript_playback_splitter  # noqa: SLF001
    splitter.resize(splitter.width() or 800, 1000)
    view.set_transcript_playback_split_top_pct(80)  # triggers apply
    sizes = splitter.sizes()
    assert len(sizes) >= 2
    if len(sizes) == 3:
        assert sizes[2] == 0, "placeholder should not steal pixels"
    visible_total = sizes[0] + sizes[1]
    top_ratio = sizes[0] / visible_total
    assert 0.78 <= top_ratio <= 0.82, (
        f"expected top ~80%, got {top_ratio:.2%}; sizes={sizes}"
    )


def test_playback_splitter_drag_emits_pct(qt_app, tmp_path):
    """Drag handler converts splitter sizes to top-pct and emits it.

    Qt does NOT fire splitterMoved on programmatic setSizes (only on
    mouse-driven drags), so we set the saved pct to something
    different from the test target first, then call the handler
    directly with sizes that imply 40/60.
    """
    view = SessionView()
    _seed_session(view, tmp_path, with_screenshots=True)
    view.set_player_position_ms(8_000)
    splitter = view._transcript_playback_splitter  # noqa: SLF001
    splitter.resize(splitter.width() or 800, 1000)
    # Saved pct starts at 70; setSizes([400, 600, 0]) -> drag-handler
    # computes 40, which differs from 70 -> signal fires.
    captured: list[int] = []
    view.transcript_playback_split_changed.connect(captured.append)
    if splitter.count() == 3:
        splitter.setSizes([400, 600, 0])
    else:
        splitter.setSizes([400, 600])
    view._on_playback_splitter_moved(400, 1)  # noqa: SLF001
    assert captured, "expected transcript_playback_split_changed to fire"
    assert 38 <= captured[-1] <= 42, captured


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
