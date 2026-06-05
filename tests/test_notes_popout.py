"""LiveNotesPopout tests (#80 followup, v0.7.7).

The popout is a standalone QMainWindow that mirrors the My Notes
preview into a screen-share-friendly window. These tests cover:

  - construction with body + session dir + always-on-top
  - debounced body updates (set_body without immediate=True does
    NOT render synchronously; with immediate=True it does)
  - fast-path: set_body with an unchanged body is a no-op
  - apply_fonts pushes to the inner preview
  - closed signal fires on closeEvent
  - always-on-top toggle reflects into the window flag
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QFont  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.ui.notes_popout import LiveNotesPopout  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


def test_popout_constructs_with_body_and_paints_immediately(qt_app):
    po = LiveNotesPopout(body="# Hello\n\nWorld")
    try:
        # set_body with immediate=True in __init__ -> body lands now,
        # without waiting for the debounce.
        assert "Hello" in po._preview.preview().toPlainText()  # noqa: SLF001
        assert po._last_rendered.startswith("# Hello")  # noqa: SLF001
    finally:
        po.close()


def test_popout_debounce_defers_render(qt_app):
    """set_body without immediate=True schedules a paint via the
    250 ms debounce timer; the preview shouldn't repaint until that
    fires."""
    po = LiveNotesPopout(body="# Initial")
    try:
        assert po._last_rendered == "# Initial"  # noqa: SLF001
        po.set_body("# Deferred")
        # Last-rendered shouldn't have flipped yet; the body is
        # pending in the debounce timer.
        assert po._last_rendered == "# Initial"  # noqa: SLF001
        # Flush the debounce directly so the test doesn't wait.
        po._render_pending_body()  # noqa: SLF001
        assert po._last_rendered == "# Deferred"  # noqa: SLF001
    finally:
        po.close()


def test_popout_set_body_immediate_renders_now(qt_app):
    po = LiveNotesPopout(body="# Initial")
    try:
        po.set_body("# Now", immediate=True)
        assert po._last_rendered == "# Now"  # noqa: SLF001
    finally:
        po.close()


def test_popout_unchanged_body_is_a_noop(qt_app):
    """Repeated set_body calls with the same string must not start
    the debounce timer or churn the QTextDocument -- this is the
    cheap fast-path for no-op editor changes (e.g. moving the cursor
    fires textChanged in some Qt builds)."""
    po = LiveNotesPopout(body="# Stable")
    try:
        # Force a debounce reset so we can detect whether the second
        # call started it.
        po._debounce.stop()  # noqa: SLF001
        po.set_body("# Stable")
        assert po._debounce.isActive() is False  # noqa: SLF001
    finally:
        po.close()


def test_popout_apply_fonts_pushes_to_inner_preview(qt_app):
    po = LiveNotesPopout(body="body")
    try:
        font = QFont("Arial")
        font.setPointSize(15)
        po.apply_fonts(font)
        inner = po._preview.preview()  # noqa: SLF001
        assert inner.font().family() == "Arial"
        assert inner.font().pointSize() == 15
    finally:
        po.close()


def test_popout_always_on_top_flag_reflects_constructor_arg(qt_app):
    po = LiveNotesPopout(body="b", always_on_top=True)
    try:
        assert po.is_always_on_top() is True
        # Menu action also reflects state.
        assert po._aot_action.isChecked() is True  # noqa: SLF001
    finally:
        po.close()


def test_popout_set_always_on_top_toggles_window_flag(qt_app):
    po = LiveNotesPopout(body="b", always_on_top=False)
    try:
        assert po.is_always_on_top() is False
        po.set_always_on_top(True)
        assert po.is_always_on_top() is True
        po.set_always_on_top(False)
        assert po.is_always_on_top() is False
    finally:
        po.close()


def test_popout_closed_signal_fires(qt_app):
    po = LiveNotesPopout(body="b")
    fired = []
    po.closed.connect(lambda: fired.append(True))
    po.show()
    qt_app.processEvents()
    po.close()
    qt_app.processEvents()
    assert fired == [True]


def test_popout_set_session_dir_rebinds_search_path(qt_app, tmp_path):
    po = LiveNotesPopout(body="b", session_dir=tmp_path)
    try:
        new_path = tmp_path / "other"
        new_path.mkdir()
        po.set_session_dir(new_path)
        # Reset the last-rendered cache so a same-body repaint
        # would actually fire (semantics for session-switch).
        assert po._last_rendered == ""  # noqa: SLF001
        po.set_body("b", immediate=True)
        assert po._last_rendered == "b"  # noqa: SLF001
    finally:
        po.close()


def test_popout_preserves_scroll_position_across_rerender(qt_app):
    """Regression for Aaron's screenshare bug: every keystroke in
    the editor was re-running setMarkdown on the popout's preview,
    which reset the QTextBrowser scroll bars to zero. With the
    audience watching a section 3/4 of the way down the page, that
    yanked them back to the top on every edit.

    The fix captures the vertical scroll position before
    setMarkdown and restores it after, so edits that don't affect
    the upper portion of the document leave the visible region
    stable for the audience."""
    long_body = "\n\n".join(
        f"## Section {i}\n\nParagraph body for section {i}. " * 4
        for i in range(20)
    )
    po = LiveNotesPopout(body=long_body)
    try:
        po.show()
        qt_app.processEvents()
        inner = po._preview.preview()  # noqa: SLF001
        # Force a layout so the scroll bar has a meaningful max.
        # QTextBrowser computes its document layout lazily; nudge
        # it by reading the maximum and asserting it's nonzero.
        qt_app.processEvents()
        bar = inner.verticalScrollBar()
        assert bar.maximum() > 0, (
            "test premise: long body must produce a scrollable preview"
        )
        # Scroll to roughly 3/4 of the way down.
        target = (bar.maximum() * 3) // 4
        bar.setValue(target)
        qt_app.processEvents()
        assert bar.value() == target

        # Now trigger a re-render (Aaron edits a paragraph). The
        # body changes slightly but the document length is similar
        # -- the audience should not get yanked to the top.
        new_body = long_body.replace(
            "Paragraph body for section 5",
            "EDITED body for section 5",
        )
        po.set_body(new_body, immediate=True)
        qt_app.processEvents()
        # Scroll position survives within bar.maximum() bounds.
        # Tolerance: the new document may have a slightly different
        # max if section 5's paragraph grew/shrank; we just confirm
        # the popout did NOT reset to 0.
        assert bar.value() > 0, (
            "scroll position reset to top -- screenshare regression"
        )
        # And we didn't end up past the new bottom.
        assert bar.value() <= bar.maximum()
    finally:
        po.close()


def test_popout_clamps_scroll_when_body_shrinks(qt_app):
    """If the new body is much shorter than the old, the prior
    scroll position would point past the end. Clamp to the new
    maximum so we land at the actual bottom rather than off-page."""
    long_body = "\n\n".join(
        f"## Section {i}\n\nParagraph " * 6 for i in range(20)
    )
    po = LiveNotesPopout(body=long_body)
    try:
        po.show()
        qt_app.processEvents()
        inner = po._preview.preview()  # noqa: SLF001
        bar = inner.verticalScrollBar()
        # Scroll to the bottom.
        bar.setValue(bar.maximum())
        qt_app.processEvents()
        prior_max = bar.maximum()
        assert prior_max > 0

        # Shrink the body dramatically.
        short_body = "Just one short line.\n"
        po.set_body(short_body, immediate=True)
        qt_app.processEvents()
        # New max is much smaller; we should be clamped to it,
        # not stranded at the prior (now-invalid) offset.
        assert bar.value() <= bar.maximum()
        assert bar.maximum() < prior_max
    finally:
        po.close()


def test_popout_is_a_top_level_window(qt_app):
    """The popout must be a real OS window so screen-share targets
    work. parent=None + Qt.Window flag combined."""
    po = LiveNotesPopout(body="b")
    try:
        # No Qt parent: standalone top-level.
        assert po.parent() is None
        # Window-type flag set.
        flags = po.windowFlags()
        assert bool(int(flags & Qt.WindowType.Window))
    finally:
        po.close()
