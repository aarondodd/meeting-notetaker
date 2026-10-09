"""Session-list sort clicks must not crash on a fresh launch.

`_on_session_list_sort_changed` used to call `_save_split_timer.start()`,
but only the transcript/playback splitter handler created that timer.
Clicking the Date or Title header before ever dragging that splitter
raised AttributeError, which PyQt6 turned into a process abort.

The handlers run unbound against a minimal stand-in rather than a full
MainApp, which starts timers and filters that leak into later tests.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


class _AppStandIn(QObject):
    """Just the state the two persistence handlers touch."""

    def __init__(self):
        super().__init__()
        from meeting_notetaker.utils.config import Config
        self.config = Config.load()


def _bind(obj):
    from meeting_notetaker.app import MainApp
    for name in (
        "_on_session_list_sort_changed",
        "_on_transcript_playback_split_changed",
        "_schedule_config_save",
    ):
        setattr(obj, name, getattr(MainApp, name).__get__(obj))
    return obj


def test_sort_change_on_fresh_launch_persists(qt_app, isolated_data_dir):
    from meeting_notetaker.utils.config import Config
    app = _bind(_AppStandIn())
    app._on_session_list_sort_changed("title_asc")
    assert Config.load().ui.session_list_sort == "title_asc"


def test_sort_header_click_through_the_real_window(qt_app, isolated_data_dir, monkeypatch):
    from meeting_notetaker.ui.main_window import MainWindow
    app = _bind(_AppStandIn())
    window = MainWindow()
    window.session_list_sort_changed.connect(app._on_session_list_sort_changed)
    # A slot exception reaches sys.excepthook; collect instead of
    # letting PyQt abort the test process.
    raised = []
    monkeypatch.setattr(sys, "excepthook", lambda *exc: raised.append(exc))
    try:
        window._list.header().setSortIndicator(1, Qt.SortOrder.AscendingOrder)
    finally:
        window.close()
        window.deleteLater()
    assert raised == []
    assert app.config.ui.session_list_sort == "title_asc"


def test_splitter_change_schedules_a_save(qt_app, isolated_data_dir):
    app = _bind(_AppStandIn())
    app._on_transcript_playback_split_changed(42)
    try:
        assert app._save_split_timer.isActive()
    finally:
        app._save_split_timer.stop()
