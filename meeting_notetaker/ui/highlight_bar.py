"""Highlight markers UI strip + Start/End toggle button + Clear All.

The strip sits below the playback scrubber and renders the user's
current highlight set as shaded `QRect`s over the same time axis.
Right-clicking a highlight shows Title / Edit range / Delete; the
toggle button captures the player's current position on click.

The widget owns no persistence -- HighlightsStore + the mutators
in models/highlights.py do that. The widget signals upward
(`highlights_changed`) and the parent saves.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QPoint, QRect, Qt, pyqtSignal
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
)
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..models.highlights import (
    Highlight,
    HighlightSet,
    add_highlight,
    remove_highlight,
    update_highlight_range,
    update_highlight_title,
)


_BAR_HEIGHT = 18
_HIGHLIGHT_FILL = QColor(64, 156, 240, 170)         # blueish
_SUGGESTION_OUTLINE = QColor(40, 100, 180, 220)
_BAR_BG = QColor(60, 60, 60, 60)


class _MarkerStrip(QWidget):
    """Just the painted strip; the parent HighlightBar wraps it with
    the toggle button + Clear All controls."""

    region_right_clicked = pyqtSignal(int)   # highlight index in current_set order

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(_BAR_HEIGHT)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed,
        )
        self._total_ms: int = 0
        self._highlights: list[Highlight] = []
        self._pending_start_ms: Optional[int] = None
        self.setMouseTracking(True)

    def set_state(
        self,
        total_ms: int,
        highlights: list[Highlight],
        pending_start_ms: Optional[int] = None,
    ) -> None:
        self._total_ms = max(0, total_ms)
        self._highlights = list(highlights)
        self._pending_start_ms = pending_start_ms
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 (Qt API)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        rect = self.rect()
        painter.fillRect(rect, _BAR_BG)
        if self._total_ms <= 0:
            painter.end()
            return
        for h in self._highlights:
            r = self._range_to_rect(h.start_ms, h.end_ms)
            if r.width() < 1:
                r = QRect(r.x(), r.y(), 1, r.height())
            painter.fillRect(r, QBrush(_HIGHLIGHT_FILL))
        if self._pending_start_ms is not None:
            r = self._range_to_rect(self._pending_start_ms, self._pending_start_ms + 1)
            r = QRect(r.x() - 1, r.y(), 3, r.height())
            painter.fillRect(r, QBrush(_SUGGESTION_OUTLINE))
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.RightButton:
            super().mousePressEvent(event)
            return
        # Translate the click x into a ms position and find the
        # first highlight under that ms.
        if self._total_ms <= 0 or not self._highlights:
            return
        x = event.position().x()
        ms_at_click = self._pixel_to_ms(x)
        for idx, h in enumerate(self._highlights):
            if h.start_ms <= ms_at_click <= h.end_ms:
                self.region_right_clicked.emit(idx)
                return

    # Helpers -------------------------------------------------------
    def _range_to_rect(self, start_ms: int, end_ms: int) -> QRect:
        if self._total_ms <= 0:
            return QRect(0, 0, 0, 0)
        w = self.width()
        x_start = int(start_ms * w / self._total_ms)
        x_end = int(end_ms * w / self._total_ms)
        return QRect(x_start, 0, max(1, x_end - x_start), self.height())

    def _pixel_to_ms(self, x: float) -> int:
        if self.width() <= 0 or self._total_ms <= 0:
            return 0
        return int(x * self._total_ms / self.width())


class HighlightBar(QWidget):
    """Full highlight bar: strip + toggle button + Clear All.

    Public API:
      * set_session_state(session_id, total_ms, hs)
      * set_player_position(pos_ms)  -- driven by the player tick
      * highlights_changed -> Signal(HighlightSet)   parent persists
    """

    highlights_changed = pyqtSignal(object)   # HighlightSet

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._session_id: str = ""
        self._total_ms: int = 0
        self._player_pos_ms: int = 0
        self._pending_start_ms: Optional[int] = None
        self._set = HighlightSet()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 0, 4, 4)
        outer.setSpacing(2)

        self._strip = _MarkerStrip(self)
        self._strip.region_right_clicked.connect(self._on_region_right_clicked)
        outer.addWidget(self._strip)

        controls = QHBoxLayout()
        controls.setSpacing(6)
        self._toggle_btn = QPushButton("Start highlight", self)
        self._toggle_btn.clicked.connect(self._on_toggle)
        controls.addWidget(self._toggle_btn)
        self._clear_btn = QPushButton("Clear all", self)
        self._clear_btn.clicked.connect(self._on_clear_all)
        controls.addWidget(self._clear_btn)
        self._status = QLabel("", self)
        self._status.setStyleSheet("color: gray;")
        controls.addWidget(self._status)
        controls.addStretch(1)
        outer.addLayout(controls)
        self.set_session_state("", 0, HighlightSet())

    # ---- public API ----
    def set_session_state(
        self,
        session_id: str,
        total_ms: int,
        hs: HighlightSet,
    ) -> None:
        """Reset to a fresh session's state. Drops any pending
        Start/End toggle from the previous session."""
        self._session_id = session_id
        self._total_ms = max(0, int(total_ms))
        self._set = hs
        self._pending_start_ms = None
        self._refresh()

    def set_player_position(self, pos_ms: int) -> None:
        self._player_pos_ms = max(0, int(pos_ms))

    def set_total_ms(self, total_ms: int) -> None:
        self._total_ms = max(0, int(total_ms))
        self._refresh()

    # ---- internal ----
    def _refresh(self) -> None:
        enabled = bool(self._session_id) and self._total_ms > 0
        for w in (self._toggle_btn, self._clear_btn):
            w.setEnabled(enabled)
        self._toggle_btn.setText(
            "End highlight" if self._pending_start_ms is not None
            else "Start highlight"
        )
        ordered = self._set.sorted_by_start()
        self._strip.set_state(self._total_ms, ordered, self._pending_start_ms)
        count = len(ordered)
        if count:
            total_s = self._set.total_duration_ms() // 1000
            self._status.setText(f"{count} highlight(s), {total_s}s total")
        else:
            self._status.setText("")

    def _on_toggle(self) -> None:
        if not self._session_id:
            return
        if self._pending_start_ms is None:
            self._pending_start_ms = self._player_pos_ms
            self._refresh()
            return
        # Lock in the end.
        start = min(self._pending_start_ms, self._player_pos_ms)
        end = max(self._pending_start_ms, self._player_pos_ms)
        self._pending_start_ms = None
        if end <= start:
            # Zero-duration toggle -- silently cancel; the user can
            # click Start again to retry.
            self._refresh()
            return
        try:
            add_highlight(
                self._set, start, end,
                total_duration_ms=self._total_ms,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Highlight", str(exc))
            self._refresh()
            return
        self.highlights_changed.emit(self._set)
        self._refresh()

    def _on_clear_all(self) -> None:
        if not self._session_id or not self._set.highlights:
            return
        confirm = QMessageBox.question(
            self, "Clear highlights?",
            f"Remove all {len(self._set.highlights)} highlight(s) for this session? "
            "Cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._set.highlights.clear()
        self._pending_start_ms = None
        self.highlights_changed.emit(self._set)
        self._refresh()

    def _on_region_right_clicked(self, index: int) -> None:
        ordered = self._set.sorted_by_start()
        if index < 0 or index >= len(ordered):
            return
        target = ordered[index]
        menu = QMenu(self)
        title_action = menu.addAction("Title...")
        edit_action = menu.addAction("Edit start/end...")
        menu.addSeparator()
        delete_action = menu.addAction("Delete")
        # Show at the strip's cursor position (mouse coordinates
        # not threaded back here; the menu pops at the widget's
        # current cursor by default).
        action = menu.exec(self._strip.mapToGlobal(QPoint(0, _BAR_HEIGHT)))
        if action is title_action:
            self._prompt_title(target)
        elif action is edit_action:
            self._prompt_range(target)
        elif action is delete_action:
            if remove_highlight(self._set, target):
                self.highlights_changed.emit(self._set)
                self._refresh()

    def _prompt_title(self, h: Highlight) -> None:
        text, ok = QInputDialog.getText(
            self, "Highlight title",
            "Title (leave blank for default):",
            QLineEdit.EchoMode.Normal,
            h.title,
        )
        if not ok:
            return
        if update_highlight_title(self._set, h, text.strip()) is None:
            return
        self.highlights_changed.emit(self._set)
        self._refresh()

    def _prompt_range(self, h: Highlight) -> None:
        text, ok = QInputDialog.getText(
            self, "Edit highlight range",
            "Start and end in MM:SS or HH:MM:SS, separated by a comma:",
            QLineEdit.EchoMode.Normal,
            f"{_ms_to_mmss(h.start_ms)}, {_ms_to_mmss(h.end_ms)}",
        )
        if not ok:
            return
        try:
            start, end = [_mmss_to_ms(s.strip()) for s in text.split(",", 1)]
        except (ValueError, IndexError):
            QMessageBox.warning(
                self, "Edit highlight range",
                "Couldn't parse the range; expected 'MM:SS, MM:SS' or "
                "'HH:MM:SS, HH:MM:SS'.",
            )
            return
        try:
            update_highlight_range(
                self._set, h, start, end,
                total_duration_ms=self._total_ms,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Edit highlight range", str(exc))
            return
        self.highlights_changed.emit(self._set)
        self._refresh()


def _ms_to_mmss(ms: int) -> str:
    seconds = int(ms / 1000)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _mmss_to_ms(text: str) -> int:
    """Accept MM:SS or HH:MM:SS. Anything else raises ValueError."""
    parts = text.split(":")
    if len(parts) == 2:
        h = 0
        m, s = parts
    elif len(parts) == 3:
        h, m, s = parts
    else:
        raise ValueError(f"unrecognized time format: {text!r}")
    return (int(h) * 3600 + int(m) * 60 + int(s)) * 1000
