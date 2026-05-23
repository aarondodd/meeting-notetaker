"""Player bar: play/pause toggle + scrubber + time readout."""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.ui.transcript_player_bar import (  # noqa: E402
    TranscriptPlayerBar,
    _fmt_ms,
)


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def test_disabled_by_default(qt_app):
    bar = TranscriptPlayerBar()
    assert not bar._play_btn.isEnabled()  # noqa: SLF001
    assert not bar._slider.isEnabled()  # noqa: SLF001


def test_set_enabled_state_unlocks_play_and_slider(qt_app):
    bar = TranscriptPlayerBar()
    bar.set_total_ms(60_000)
    bar.set_enabled_state(True)
    assert bar._play_btn.isEnabled()  # noqa: SLF001
    assert bar._slider.isEnabled()  # noqa: SLF001


def test_play_button_label_flips_with_is_playing(qt_app):
    bar = TranscriptPlayerBar()
    bar.set_is_playing(False)
    assert bar._play_btn.text() == "Play"  # noqa: SLF001
    bar.set_is_playing(True)
    assert bar._play_btn.text() == "Pause"  # noqa: SLF001
    bar.set_is_playing(False)
    assert bar._play_btn.text() == "Play"  # noqa: SLF001


def test_play_click_emits_play_when_not_playing(qt_app):
    bar = TranscriptPlayerBar()
    bar.set_enabled_state(True)
    fires_play: list[None] = []
    fires_pause: list[None] = []
    bar.play_clicked.connect(lambda: fires_play.append(None))
    bar.pause_clicked.connect(lambda: fires_pause.append(None))
    bar.set_is_playing(False)
    bar._play_btn.click()  # noqa: SLF001
    assert len(fires_play) == 1
    assert len(fires_pause) == 0


def test_play_click_emits_pause_when_playing(qt_app):
    bar = TranscriptPlayerBar()
    bar.set_enabled_state(True)
    fires_pause: list[None] = []
    bar.pause_clicked.connect(lambda: fires_pause.append(None))
    bar.set_is_playing(True)
    bar._play_btn.click()  # noqa: SLF001
    assert len(fires_pause) == 1


def test_set_position_ms_updates_slider_and_label(qt_app):
    bar = TranscriptPlayerBar()
    bar.set_total_ms(120_000)  # 2 min
    bar.set_position_ms(45_000)  # 45 sec
    assert bar._slider.value() == 45_000  # noqa: SLF001
    assert "0:45" in bar._time_label.text()  # noqa: SLF001
    assert "2:00" in bar._time_label.text()  # noqa: SLF001


def test_set_position_does_not_emit_seek(qt_app):
    """The bar suppresses its own valueChanged echo while we
    programmatically move the slider. Otherwise every position
    tick would re-emit seek_ms_requested and the player would
    flutter."""
    bar = TranscriptPlayerBar()
    bar.set_total_ms(60_000)
    bar.set_enabled_state(True)
    captured: list[int] = []
    bar.seek_ms_requested.connect(captured.append)
    bar.set_position_ms(30_000)
    bar.set_position_ms(45_000)
    assert captured == []


def test_fmt_ms_short_and_long_forms():
    assert _fmt_ms(0) == "0:00"
    assert _fmt_ms(5_500) == "0:05"
    assert _fmt_ms(125_000) == "2:05"
    assert _fmt_ms(3_600_000) == "1:00:00"
    assert _fmt_ms(3_900_000) == "1:05:00"
