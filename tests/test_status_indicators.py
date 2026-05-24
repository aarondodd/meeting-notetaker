"""Status-bar indicator widget + dot-painter helper.

The pills replace v0.6.3's "Mic: X | Calendar: watching | ..." string
in the bottom status bar. Each pill is a colored dot + short label;
the tooltip carries the long-form text. See ui/status_indicators.py.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtGui")
pytest.importorskip("PyQt6.QtWidgets")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QPixmap  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.ui.status_indicators import (  # noqa: E402
    DOT_COLORS,
    SegmentState,
    StatusSegment,
    dot_pixmap,
)


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def test_dot_pixmap_returns_qpixmap_of_requested_size(qt_app):
    pix = dot_pixmap("green", size=12)
    assert isinstance(pix, QPixmap)
    assert pix.width() == 12
    assert pix.height() == 12


def test_dot_pixmap_uses_color_for_center_pixel(qt_app):
    """The center pixel of a filled circle should match the requested color."""
    pix = dot_pixmap("green", size=20)
    img = pix.toImage()
    center = img.pixelColor(10, 10)
    expected = DOT_COLORS["green"]
    assert center.red() == expected.red()
    assert center.green() == expected.green()
    assert center.blue() == expected.blue()


def test_dot_pixmap_corner_is_transparent(qt_app):
    """The pixmap has a transparent background -- the corner outside the
    inscribed circle stays alpha=0 so the dot composites cleanly on the
    status bar's native background."""
    pix = dot_pixmap("red", size=20)
    img = pix.toImage()
    corner = img.pixelColor(0, 0)
    assert corner.alpha() == 0


def test_dot_pixmap_unknown_color_falls_back_to_gray(qt_app):
    """Bad input doesn't raise -- it just paints gray. Keeps the call site
    forgiving when the color string is computed from config."""
    pix = dot_pixmap("bogus", size=10)
    img = pix.toImage()
    center = img.pixelColor(5, 5)
    expected = DOT_COLORS["gray"]
    assert center.red() == expected.red()
    assert center.green() == expected.green()
    assert center.blue() == expected.blue()


def test_status_segment_apply_with_payload(qt_app):
    """A pill with payload renders 'Label Value'."""
    seg = StatusSegment()
    seg.apply(SegmentState(
        color="green",
        short_label="Mic",
        payload="HyperX",
        tooltip="Microphone device: HyperX QuadCast",
    ))
    assert seg.isVisible() or seg.isHidden() is False
    # The text label is the second child; pull from the layout.
    text_label = seg._text  # noqa: SLF001
    assert text_label.text() == "Mic HyperX"
    assert seg.toolTip() == "Microphone device: HyperX QuadCast"


def test_status_segment_apply_label_only(qt_app):
    """No payload -> just the short label, no trailing space."""
    seg = StatusSegment()
    seg.apply(SegmentState(
        color="green",
        short_label="Cal",
        payload="",
        tooltip="Watching Outlook calendar.",
    ))
    assert seg._text.text() == "Cal"  # noqa: SLF001


def test_status_segment_apply_hides_when_invisible(qt_app):
    """visible=False hides the widget so a layout that always contains
    all known segments collapses out the hidden ones."""
    seg = StatusSegment()
    seg.show()  # ensure it starts visible
    seg.apply(SegmentState(visible=False))
    assert seg.isHidden()


def test_status_segment_apply_tooltip_falls_back_to_text(qt_app):
    """Empty tooltip is replaced with the visible text so hovering an
    OK pill still shows what the pill represents."""
    seg = StatusSegment()
    seg.apply(SegmentState(
        color="green",
        short_label="Spk",
        payload="5",
    ))
    assert seg.toolTip() == "Spk 5"


def test_main_window_set_status_indicators_hides_missing_keys(qt_app):
    """A key absent from the indicators dict -> that segment hides; one
    present -> that segment shows. Verifies the dict-driven contract."""
    from meeting_notetaker.ui.main_window import MainWindow

    win = MainWindow()
    try:
        win.set_status_indicators(
            version="9.9.9",
            indicators={
                "cal": SegmentState(
                    color="green", short_label="Cal",
                ),
                "syn": SegmentState(
                    color="green", short_label="Syn",
                ),
            },
        )
        assert not win._status_segments["cal"].isHidden()  # noqa: SLF001
        assert not win._status_segments["syn"].isHidden()  # noqa: SLF001
        # Segments not passed in this call must hide.
        assert win._status_segments["voice"].isHidden()  # noqa: SLF001
        assert win._status_segments["det"].isHidden()  # noqa: SLF001
        # Re-call without "syn"; it should hide now.
        win.set_status_indicators(
            version="9.9.9",
            indicators={
                "cal": SegmentState(
                    color="green", short_label="Cal",
                ),
            },
        )
        assert win._status_segments["syn"].isHidden()  # noqa: SLF001
    finally:
        win.deleteLater()


def test_main_window_set_status_indicators_renders_version(qt_app):
    from meeting_notetaker.ui.main_window import MainWindow

    win = MainWindow()
    try:
        win.set_status_indicators(version="0.6.5", indicators={})
        assert win._version_label.text() == "v0.6.5"  # noqa: SLF001
        assert "0.6.5" in win._version_label.toolTip()  # noqa: SLF001
    finally:
        win.deleteLater()


def test_synthesis_connection_state_dot_color_maps_each_state(qt_app):
    """SynthesisConnectionState pins its own dot color so app.py doesn't
    have to keep a parallel switch statement. Pin all three branches."""
    pytest.importorskip("PyQt6.QtCore")
    from meeting_notetaker.utils.chrome_process import SynthesisConnectionState

    assert SynthesisConnectionState.NOT_RUNNING.dot_color() == "yellow"
    assert SynthesisConnectionState.RUNNING_CONNECTED.dot_color() == "green"
    assert SynthesisConnectionState.RUNNING_DISCONNECTED.dot_color() == "red"
