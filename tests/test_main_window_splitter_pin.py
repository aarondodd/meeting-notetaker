"""Outer splitter stability when SessionView's right column toggles.

The bug shape (Aaron, 2026-06-25): start/stop recording flips the
SessionView's right column (screencap + attendee sidebars) on/off, and
the outer main_splitter would redistribute -- the session-list pane on
the left would shrink to make room for the right column's new minimum
width. The user's spec is that the outer splitter must NEVER auto-resize;
only the inner session-view layout (My Notes editor + speaker-tag side
pane) is allowed to shift.

The fix pins the left pane's minimumWidth to its current width during
the toggle so Qt's layout pass can't shrink it. This test exercises that
contract at the Qt level: the splitter sizes must be identical before
and after a right-column toggle.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


def _drain_events() -> None:
    """Process pending events including the QTimer.singleShot(0) the
    fix uses to unpin the left-pane minimumWidth."""
    for _ in range(4):
        QCoreApplication.processEvents()


def test_outer_splitter_sizes_survive_right_column_show(qt_app):
    """Showing the right column must not shrink the session-list pane."""
    from meeting_notetaker.ui.main_window import MainWindow
    win = MainWindow()
    try:
        win.resize(1200, 800)
        win.show()
        _drain_events()
        win._main_splitter.setSizes([340, 860])  # noqa: SLF001
        _drain_events()
        before = list(win._main_splitter.sizes())  # noqa: SLF001
        # Toggle ON (simulates Start Recording entering a state where
        # the right column becomes visible).
        win.session_view._toggle_right_column(True)  # noqa: SLF001
        _drain_events()
        after = list(win._main_splitter.sizes())  # noqa: SLF001
        assert after == before, (
            f"outer splitter changed across right-column show: "
            f"before={before} after={after}"
        )
    finally:
        win.deleteLater()


def test_outer_splitter_sizes_survive_right_column_hide(qt_app):
    """Hiding the right column must also not change the outer split."""
    from meeting_notetaker.ui.main_window import MainWindow
    win = MainWindow()
    try:
        win.resize(1200, 800)
        win.show()
        _drain_events()
        # Start with the right column visible.
        win.session_view._toggle_right_column(True)  # noqa: SLF001
        _drain_events()
        win._main_splitter.setSizes([340, 860])  # noqa: SLF001
        _drain_events()
        before = list(win._main_splitter.sizes())  # noqa: SLF001
        # Toggle OFF (simulates Stop Recording).
        win.session_view._toggle_right_column(False)  # noqa: SLF001
        _drain_events()
        after = list(win._main_splitter.sizes())  # noqa: SLF001
        assert after == before, (
            f"outer splitter changed across right-column hide: "
            f"before={before} after={after}"
        )
    finally:
        win.deleteLater()


def test_left_min_width_restored_after_toggle(qt_app):
    """The pin must be temporary: after the toggle settles, the left
    pane's minimumWidth is back at its original (240 px) so the user
    can still drag the splitter handle leftward."""
    from meeting_notetaker.ui.main_window import MainWindow
    win = MainWindow()
    try:
        win.resize(1200, 800)
        win.show()
        _drain_events()
        original_min = win._main_splitter.widget(0).minimumWidth()  # noqa: SLF001
        win.session_view._toggle_right_column(True)  # noqa: SLF001
        _drain_events()
        after = win._main_splitter.widget(0).minimumWidth()  # noqa: SLF001
        assert after == original_min, (
            f"left pane min width not restored: original={original_min} "
            f"after={after}"
        )
    finally:
        win.deleteLater()


def test_pin_is_applied_synchronously_before_setVisible(qt_app):
    """The pin must be in place BEFORE Qt's layout pass on
    setVisible runs -- otherwise the layout has already redistributed
    the splitter by the time we lock left.minimumWidth. This is the
    fail-mode the original best-effort fix had.

    We call ``_on_right_column_will_toggle`` directly (without
    draining events) and verify that left.minimumWidth has been
    pinned to the saved left-pane width. The slot is synchronous --
    the pin must be in place by the time it returns.
    """
    from meeting_notetaker.ui.main_window import MainWindow
    win = MainWindow()
    try:
        win.resize(1200, 800)
        win.show()
        _drain_events()
        win._main_splitter.setSizes([340, 860])  # noqa: SLF001
        _drain_events()
        sizes_before = list(win._main_splitter.sizes())  # noqa: SLF001
        original_min = win._main_splitter.widget(0).minimumWidth()  # noqa: SLF001
        # Fire the slot directly; do NOT drain events yet so the
        # QTimer.singleShot(0) hasn't fired.
        win._on_right_column_will_toggle(True)  # noqa: SLF001
        pinned_min = win._main_splitter.widget(0).minimumWidth()  # noqa: SLF001
        # Now drain to let the restore timer fire.
        _drain_events()
        restored_min = win._main_splitter.widget(0).minimumWidth()  # noqa: SLF001

        assert pinned_min == sizes_before[0], (
            f"pin not applied: expected left.minimumWidth=={sizes_before[0]}"
            f", got {pinned_min}"
        )
        assert pinned_min > original_min, (
            f"pin not larger than original min ({original_min}); "
            f"the pin would not actually prevent shrinkage"
        )
        assert restored_min == original_min, (
            f"original min not restored after timer: expected "
            f"{original_min}, got {restored_min}"
        )
    finally:
        win.deleteLater()


def test_idempotent_when_no_state_change(qt_app):
    """Toggling to the current state must not mutate splitter sizes."""
    from meeting_notetaker.ui.main_window import MainWindow
    win = MainWindow()
    try:
        win.resize(1200, 800)
        win.show()
        _drain_events()
        win._main_splitter.setSizes([340, 860])  # noqa: SLF001
        _drain_events()
        before = list(win._main_splitter.sizes())  # noqa: SLF001
        # right column starts hidden; toggling to hidden is a no-op.
        win.session_view._toggle_right_column(False)  # noqa: SLF001
        _drain_events()
        after = list(win._main_splitter.sizes())  # noqa: SLF001
        assert after == before
    finally:
        win.deleteLater()
