"""Pop-out preview window for My Notes (#80 followup, v0.7.7).

Read-only Markdown preview that mirrors what the user types into the
main app's My Notes editor. Designed for screen-share during a call:
the user opens the popout, drops it onto a second monitor (or floats
it on top), and the audience watches it render live as the user
types. The main app window underneath stays the typist's workspace.

Public API used by MainApp:
    - LiveNotesPopout(parent, *, body, session_dir, always_on_top)
    - .set_body(text)              -> refresh the preview (debounced)
    - .set_session_dir(path)       -> rebind image search path
    - .set_always_on_top(on)       -> toggle the OS hint
    - .apply_fonts(preview_font)   -> push the preview font
    - .closed signal               -> emitted when the window closes
    - Window geometry serializes to bytes via saveGeometry() so
      MainApp can persist + restore via UiConfig.notes_popout_geometry.

The popout uses the same 250 ms debounce timer the in-app preview
uses; re-rendering Markdown on every keystroke is wasteful and
produces visible scroll jumps that the spec ("no live scrolling")
asks us to avoid. Aaron's pattern: type a block, glance at the
popout, type another. 250 ms is short enough to feel immediate but
long enough to coalesce a quick burst of keystrokes into one paint.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QCloseEvent
from PyQt6.QtWidgets import QMainWindow, QWidget

from .preview_with_toc import PreviewWithToc


# Same debounce the in-app preview uses (live_notes_widget.py).
# Tunable here if Aaron asks for snappier / lazier; not exposed in
# Settings because the right value is "as fast as Qt can repaint
# without making the audience seasick" and 250 ms is the standard.
_DEBOUNCE_MS = 250


class LiveNotesPopout(QMainWindow):
    """Standalone window wrapping a Markdown preview pane.

    Constructed with ``parent=None`` so the OS treats it as an
    independent top-level window (screen-sharers can target it
    directly). Qt's window-management treats it as a child of the
    process for activation / focus, but not for geometry / DPI
    inheritance.
    """

    closed = pyqtSignal()

    def __init__(
        self,
        *,
        parent: Optional[QWidget] = None,
        body: str = "",
        session_dir: Optional[Path] = None,
        always_on_top: bool = False,
        window_title: str = "My Notes Preview",
    ) -> None:
        # parent=None: this is a top-level OS window the user can
        # screenshare independently. Qt.WindowType.Window is the
        # default for QMainWindow but make it explicit.
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle(window_title)
        self.resize(720, 540)

        # Debounce timer for set_body. Same pattern as the in-app
        # preview's _preview_refresh_timer; coalesces rapid typing
        # into one paint per ~250 ms.
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._render_pending_body)
        self._pending_body: str = ""
        # Track what was last rendered so we don't repaint when
        # set_body fires with an unchanged value (no-op typing,
        # session-reselect of the same body).
        self._last_rendered: str = ""

        self._preview = PreviewWithToc(self)
        self.setCentralWidget(self._preview)

        # View menu inside the popout for the always-on-top toggle.
        # OS-level always-on-top is a window flag that has to be set
        # BEFORE show() each time it changes -- toggling at runtime
        # requires reparenting via setWindowFlag().
        view_menu = self.menuBar().addMenu("&View")
        self._aot_action = QAction("&Always on Top", self)
        self._aot_action.setCheckable(True)
        self._aot_action.setChecked(always_on_top)
        self._aot_action.toggled.connect(self._on_always_on_top_toggled)
        view_menu.addAction(self._aot_action)

        # Apply the saved always-on-top state to the window flags.
        if always_on_top:
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)

        # Seed initial state. set_body skips the debounce here so
        # the popout paints content the moment it opens, not after
        # 250 ms of blank window.
        self._session_dir = session_dir
        if session_dir is not None:
            self._preview.setSearchPaths([str(session_dir)])
        self.set_body(body, immediate=True)

    # ---- public API used by MainApp -----------------------------------

    def set_body(self, body: str, *, immediate: bool = False) -> None:
        """Update the preview body. By default coalesces consecutive
        calls within the debounce window so a burst of typing only
        repaints once. Pass ``immediate=True`` for the initial open
        + session-switch paths where the user expects instant content.
        """
        if body == self._last_rendered:
            # Cheap fast-path: avoids the debounce timer reset (and
            # the QTextDocument churn) when no actual edit happened.
            return
        self._pending_body = body
        if immediate:
            self._debounce.stop()
            self._render_pending_body()
        else:
            self._debounce.start()

    def set_session_dir(self, path: Optional[Path]) -> None:
        """Rebind the image search path. Called when the user picks
        a different session in the main window -- relative image
        refs like ``images/foo.png`` resolve under the new dir."""
        self._session_dir = path
        if path is not None:
            self._preview.setSearchPaths([str(path)])
        else:
            self._preview.setSearchPaths([])
        # Reset the last-rendered cache so a session-switch repaint
        # actually happens even if the new body is byte-identical to
        # the prior one (rare but possible -- two sessions both
        # carry the default seed body, say).
        self._last_rendered = ""

    def set_always_on_top(self, on: bool) -> None:
        """Public setter for tests / programmatic toggles. Reflects
        into the menu checkbox and applies the window flag."""
        self._aot_action.setChecked(on)
        # _on_always_on_top_toggled fires off the toggle signal and
        # handles the flag + reshow dance.

    def apply_fonts(self, preview_font) -> None:
        """Push the resolved preview font onto the inner
        MarkdownPreview. Called by MainApp on Settings save so the
        popout tracks whatever the user picked for the main preview.
        """
        try:
            inner = self._preview.preview()
        except AttributeError:
            inner = self._preview
        inner.setFont(preview_font)

    def is_always_on_top(self) -> bool:
        return bool(
            int(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        )

    # ---- internals ----------------------------------------------------

    def _render_pending_body(self) -> None:
        body = self._pending_body
        # Preserve the audience's scroll position across re-renders.
        # setMarkdown replaces the underlying QTextDocument, which
        # resets the scroll bars to zero; without this dance, every
        # keystroke in the editor would yank the popout back to the
        # top -- useless during a screenshare where the presenter has
        # scrolled three-quarters of the way down to discuss
        # something concrete. Pixel-pinning is the right behavior
        # for the common case (editing somewhere below the audience's
        # current view); the clamp keeps us in-bounds when the
        # document shrinks. (#80 followup: Aaron's report after the
        # initial popout ship.)
        try:
            inner = self._preview.preview()
        except AttributeError:
            inner = self._preview
        bar = inner.verticalScrollBar()
        prior_scroll = bar.value()
        self._preview.setMarkdown(body)
        bar.setValue(min(prior_scroll, bar.maximum()))
        self._last_rendered = body

    def _on_always_on_top_toggled(self, on: bool) -> None:
        """Window flag mutations require re-showing the widget for
        the change to take effect on most Qt platforms."""
        was_visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
        if was_visible:
            # show() after setWindowFlag re-creates the OS window
            # with the new flag set. Skip if the popout is being
            # toggled before its first show.
            self.show()

    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        """Emit the closed signal so MainApp can drop its reference
        and persist the final geometry + always-on-top state."""
        try:
            self.closed.emit()
        finally:
            super().closeEvent(event)
