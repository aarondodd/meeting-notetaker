"""Transcript [HH:MM:SS] timestamp parsing + bisect-by-position lookup.

These two helpers are what bind the player's millisecond position to
a specific transcript line for highlighting + click-to-seek.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.ui.session_view import (  # noqa: E402
    _block_for_position_ms,
    _parse_transcript_timestamps,
    _start_ms_for_block,
)


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def test_parse_timestamps_picks_only_lines_with_leading_bracket(qt_app):
    text = (
        "[00:00:03] Speaker: hi.\n"
        "Status line without a timestamp.\n"
        "[00:00:11] Speaker: there.\n"
        "\n"
        "[01:02:03] Speaker: an hour later.\n"
    )
    result = _parse_transcript_timestamps(text)
    # Three timestamp lines on block_numbers 0, 2, 4.
    assert [(ms, blk) for ms, blk in result] == [
        (3_000, 0),
        (11_000, 2),
        ((1 * 3600 + 2 * 60 + 3) * 1000, 4),
    ]


def test_parse_timestamps_empty_text(qt_app):
    assert _parse_transcript_timestamps("") == []


def test_block_for_position_returns_active_block(qt_app):
    """At t=5s with segments at 0s, 11s, 22s -> first block. At 15s,
    second block."""
    timestamps = [(0, 0), (11_000, 1), (22_000, 2)]
    assert _block_for_position_ms(timestamps, 0) == 0
    assert _block_for_position_ms(timestamps, 5_000) == 0
    assert _block_for_position_ms(timestamps, 11_000) == 1
    assert _block_for_position_ms(timestamps, 15_000) == 1
    assert _block_for_position_ms(timestamps, 22_000) == 2
    assert _block_for_position_ms(timestamps, 99_999) == 2


def test_block_for_position_before_first_segment_returns_none(qt_app):
    """Position precedes every segment (transcript hasn't started yet)
    -> no highlight."""
    timestamps = [(5_000, 0), (10_000, 1)]
    assert _block_for_position_ms(timestamps, 0) is None
    assert _block_for_position_ms(timestamps, 4_999) is None


def test_block_for_position_empty_returns_none(qt_app):
    assert _block_for_position_ms([], 1_000) is None


def test_start_ms_for_block_returns_anchor(qt_app):
    timestamps = [(0, 0), (11_000, 1), (22_000, 5)]
    assert _start_ms_for_block(timestamps, 0) == 0
    assert _start_ms_for_block(timestamps, 1) == 11_000
    assert _start_ms_for_block(timestamps, 5) == 22_000


def test_start_ms_for_block_missing_returns_none(qt_app):
    timestamps = [(0, 0), (11_000, 1)]
    assert _start_ms_for_block(timestamps, 99) is None
