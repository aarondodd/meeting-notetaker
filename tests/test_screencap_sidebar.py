"""ScreencapSidebar: Capture + Insert buttons in the My Notes pane.

The buttons are disabled until SessionView calls set_armed(True) --
the user can't capture before they've drawn a region. Help label
toggles visibility so the disarmed state has a hint.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.ui.screencap_sidebar import ScreencapSidebar  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def test_buttons_disabled_by_default(qt_app):
    sidebar = ScreencapSidebar()
    assert not sidebar._capture_btn.isEnabled()  # noqa: SLF001
    assert not sidebar._insert_btn.isEnabled()  # noqa: SLF001
    # The help label is visible while disarmed so the user knows why.
    assert sidebar._help.isVisible() or sidebar._help.isHidden() is False  # noqa: SLF001


def test_set_armed_true_enables_both(qt_app):
    sidebar = ScreencapSidebar()
    sidebar.set_armed(True)
    assert sidebar._capture_btn.isEnabled()  # noqa: SLF001
    assert sidebar._insert_btn.isEnabled()  # noqa: SLF001


def test_capture_emits_signal(qt_app):
    sidebar = ScreencapSidebar()
    sidebar.set_armed(True)
    fires: list[None] = []
    sidebar.capture_clicked.connect(lambda: fires.append(None))
    sidebar._capture_btn.click()  # noqa: SLF001
    assert len(fires) == 1


def test_insert_emits_signal(qt_app):
    sidebar = ScreencapSidebar()
    sidebar.set_armed(True)
    fires: list[None] = []
    sidebar.insert_clicked.connect(lambda: fires.append(None))
    sidebar._insert_btn.click()  # noqa: SLF001
    assert len(fires) == 1


def test_set_armed_false_disables_both(qt_app):
    sidebar = ScreencapSidebar()
    sidebar.set_armed(True)
    sidebar.set_armed(False)
    assert not sidebar._capture_btn.isEnabled()  # noqa: SLF001
    assert not sidebar._insert_btn.isEnabled()  # noqa: SLF001
