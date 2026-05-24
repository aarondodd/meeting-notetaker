"""Within-tab incremental find bar.

Standard `Ctrl+F` UX: slim bar at the bottom of a text tab. Type to
filter; Enter / Shift+Enter navigates next / previous; case-sensitive
and whole-word toggles; Esc closes. The bar attaches to any
QTextEdit / QTextBrowser via `attach()`.

Implementation: defers to the widget's built-in `find()` which uses
QTextDocument's own forward / reverse cursor search. On miss the bar
flashes its background red and shows a "no matches" label; on hit it
selects the match in the host text widget so the existing styling
shows the match in context.

The bar is hidden by default; the consuming view (SessionView's tab
container) calls `show_for(widget)` to focus + bind. Ctrl+F is wired
upstream -- this widget doesn't grab the shortcut itself so the
parent can scope it to its active tab.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QKeyEvent, QTextDocument, QTextCursor
from PyQt6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QToolButton,
    QWidget,
)


class FindBar(QWidget):
    """Inline find bar wired to one text widget at a time."""

    # Emitted when the user closes the bar (Esc or the X button). The
    # parent view typically restores focus to the host text widget.
    closed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._host: Optional[QWidget] = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(6)

        self._label = QLabel("Find:", self)
        layout.addWidget(self._label)

        self._input = QLineEdit(self)
        self._input.setPlaceholderText("Search this tab...")
        self._input.textChanged.connect(self._on_text_changed)
        self._input.returnPressed.connect(self._find_next)
        layout.addWidget(self._input, 1)

        self._prev_btn = QToolButton(self)
        self._prev_btn.setText("▲")  # up triangle
        self._prev_btn.setToolTip("Previous match (Shift+Enter)")
        self._prev_btn.clicked.connect(self._find_previous)
        layout.addWidget(self._prev_btn)

        self._next_btn = QToolButton(self)
        self._next_btn.setText("▼")  # down triangle
        self._next_btn.setToolTip("Next match (Enter)")
        self._next_btn.clicked.connect(self._find_next)
        layout.addWidget(self._next_btn)

        self._case_check = QCheckBox("Aa", self)
        self._case_check.setToolTip("Match case")
        self._case_check.stateChanged.connect(self._on_options_changed)
        layout.addWidget(self._case_check)

        self._word_check = QCheckBox("W", self)
        self._word_check.setToolTip("Whole words only")
        self._word_check.stateChanged.connect(self._on_options_changed)
        layout.addWidget(self._word_check)

        self._status = QLabel("", self)
        self._status.setMinimumWidth(80)
        layout.addWidget(self._status)

        self._close_btn = QPushButton("X", self)
        self._close_btn.setFlat(True)
        self._close_btn.setFixedWidth(24)
        self._close_btn.setToolTip("Close find bar (Esc)")
        self._close_btn.clicked.connect(self.hide_bar)
        layout.addWidget(self._close_btn)

        self.hide()  # parent shows on Ctrl+F

        # Debounce: while the user types, don't search after every
        # keystroke -- 80ms feels instant but lets fast typing complete
        # before each pass on long transcripts.
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(80)
        self._debounce.timeout.connect(self._search_current_query)

    # ---- public API ----
    def attach(self, host: QWidget) -> None:
        """Bind the bar to a QTextEdit / QTextBrowser. Prior binding
        (if any) is dropped silently -- the bar always operates on
        the most recently-attached widget."""
        self._host = host

    def show_for(self, host: QWidget) -> None:
        """Attach + reveal + focus the input. Pre-fills the input from
        the host's selection so a user with text highlighted can hit
        Ctrl+F and find the same thing they were looking at."""
        self.attach(host)
        selection = ""
        try:
            cursor = host.textCursor()
            if cursor.hasSelection():
                selection = cursor.selectedText()
        except AttributeError:
            pass
        if selection and " " not in selection:
            # QTextCursor.selectedText uses paragraph-separator U+2029
            # for line breaks; skip multi-line selections.
            self._input.setText(selection)
            self._input.selectAll()
        self.show()
        self._input.setFocus(Qt.FocusReason.OtherFocusReason)
        # Re-run search whenever the bar opens (the host text may
        # have changed since the last time it closed).
        self._search_current_query()

    def hide_bar(self) -> None:
        """Hide + restore focus to the host so typing continues in
        the document, not the find input."""
        self.hide()
        if self._host is not None:
            try:
                self._host.setFocus(Qt.FocusReason.OtherFocusReason)
            except RuntimeError:
                pass
        self.closed.emit()

    # ---- key events ----
    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt API)
        if event.key() == Qt.Key.Key_Escape:
            self.hide_bar()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self._find_previous()
            else:
                self._find_next()
            return
        super().keyPressEvent(event)

    # ---- internals ----
    def _on_text_changed(self, _text: str) -> None:
        self._debounce.start()

    def _on_options_changed(self, _state: int) -> None:
        self._search_current_query()

    def _build_flags(self, *, backward: bool = False) -> QTextDocument.FindFlag:
        flags = QTextDocument.FindFlag(0)
        if self._case_check.isChecked():
            flags |= QTextDocument.FindFlag.FindCaseSensitively
        if self._word_check.isChecked():
            flags |= QTextDocument.FindFlag.FindWholeWords
        if backward:
            flags |= QTextDocument.FindFlag.FindBackward
        return flags

    def _search_current_query(self) -> None:
        """Run a fresh forward find starting from the document top.

        Distinct from _find_next which advances from the current
        cursor -- this exists so options changes + initial open both
        land on the first match instead of staying wherever the
        cursor happened to be.
        """
        if self._host is None or not self._input.text():
            self._status.setText("")
            return
        cursor = self._host.textCursor()
        cursor.setPosition(0)
        self._host.setTextCursor(cursor)
        self._find_next(reset_status=True)

    def _find_next(self, *, reset_status: bool = False) -> bool:
        return self._do_find(backward=False, reset_status=reset_status)

    def _find_previous(self, *, reset_status: bool = False) -> bool:
        return self._do_find(backward=True, reset_status=reset_status)

    def _do_find(self, *, backward: bool, reset_status: bool) -> bool:
        if self._host is None:
            return False
        text = self._input.text()
        if not text:
            self._status.setText("")
            return False
        flags = self._build_flags(backward=backward)
        found = self._host.find(text, flags)
        if not found:
            # Wrap-around: move the cursor to the document boundary
            # and try once more so successive Enters loop instead of
            # dead-ending at the last match.
            cursor = self._host.textCursor()
            cursor.movePosition(
                QTextCursor.MoveOperation.End if backward else QTextCursor.MoveOperation.Start
            )
            self._host.setTextCursor(cursor)
            found = self._host.find(text, flags)
        if found:
            self._status.setText("")
        else:
            self._status.setText("No matches")
        return found
