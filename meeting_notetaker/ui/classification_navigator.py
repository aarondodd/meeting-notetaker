"""Session-list filter pulldown (Issue #24, By Series / By Person /
By Topic views).

Two combo boxes side-by-side above the session list:

* **View:** All / By Series / By Person / By Topic.
* **Value:** the specific series / person / topic (or "(any)" when
  View is All). Populates from the ClassificationStore.

When the filter changes the widget emits `filter_changed(view,
value_id)` where value_id is None for "All" and the int row id of
the chosen series/person/topic otherwise. MainApp listens, calls
`store.session_ids_for_*`, and intersects with the SessionStore's
session list before pushing the visible set to MainWindow.

Deliberately a pulldown rather than a tree pane: keeps the left
column thin, avoids stealing horizontal space from the session
titles, and matches the existing Sessions header row layout.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QWidget,
)


VIEW_ALL = "all"
VIEW_BY_SERIES = "by_series"
VIEW_BY_PERSON = "by_person"
VIEW_BY_TOPIC = "by_topic"
VALID_VIEWS = (VIEW_ALL, VIEW_BY_SERIES, VIEW_BY_PERSON, VIEW_BY_TOPIC)


@dataclass
class FilterState:
    """Snapshot of the navigator's selection. `value_id` is None
    when view is VIEW_ALL or when the value combo has nothing
    selected (e.g. no series exist yet)."""
    view: str = VIEW_ALL
    value_id: Optional[int] = None


class ClassificationNavigator(QWidget):
    """Two-combo filter for the session list."""

    # view (str), value_id (Optional[int])
    filter_changed = pyqtSignal(str, object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        layout.addWidget(QLabel("View:", self))
        self._view_combo = QComboBox(self)
        self._view_combo.addItem("All sessions", VIEW_ALL)
        self._view_combo.addItem("By Series", VIEW_BY_SERIES)
        self._view_combo.addItem("By Person", VIEW_BY_PERSON)
        self._view_combo.addItem("By Topic", VIEW_BY_TOPIC)
        self._view_combo.currentIndexChanged.connect(self._on_view_changed)
        layout.addWidget(self._view_combo, 0)

        self._value_combo = QComboBox(self)
        self._value_combo.setMinimumWidth(140)
        self._value_combo.currentIndexChanged.connect(self._on_value_changed)
        layout.addWidget(self._value_combo, 1)

        # Pre-populated with the All-sessions placeholder so the
        # combo isn't empty before MainApp pushes data.
        self._series_list: list[tuple[int, str]] = []
        self._people_list: list[tuple[int, str]] = []
        self._topics_list: list[tuple[int, str]] = []
        self._suppress_emit = False
        self._update_value_combo()

    # ---- public API ----
    def set_series(self, items: list[tuple[int, str]]) -> None:
        """items = [(id, display_name), ...] sorted alphabetically."""
        self._series_list = list(items)
        if self.current_view() == VIEW_BY_SERIES:
            self._update_value_combo()

    def set_people(self, items: list[tuple[int, str]]) -> None:
        self._people_list = list(items)
        if self.current_view() == VIEW_BY_PERSON:
            self._update_value_combo()

    def set_topics(self, items: list[tuple[int, str]]) -> None:
        self._topics_list = list(items)
        if self.current_view() == VIEW_BY_TOPIC:
            self._update_value_combo()

    def current_view(self) -> str:
        return self._view_combo.currentData() or VIEW_ALL

    def current_state(self) -> FilterState:
        return FilterState(
            view=self.current_view(),
            value_id=self._current_value_id(),
        )

    def reset(self) -> None:
        """Snap back to View=All. Used by MainApp when the user
        clears a filter via the dedicated 'show all' button."""
        self._suppress_emit = True
        try:
            idx = self._view_combo.findData(VIEW_ALL)
            if idx >= 0:
                self._view_combo.setCurrentIndex(idx)
            self._update_value_combo()
        finally:
            self._suppress_emit = False
        self._emit()

    # ---- internals ----
    def _on_view_changed(self, _idx: int) -> None:
        # Switching views always wipes the value selection -- a
        # series_id has no meaning under By-Person, etc.
        self._update_value_combo(preserve_selection=False)
        self._emit()

    def _on_value_changed(self, _idx: int) -> None:
        if self._suppress_emit:
            return
        self._emit()

    def _update_value_combo(self, *, preserve_selection: bool = True) -> None:
        """Refresh the second combo's contents based on the active
        view.

        `preserve_selection=True` (the default and the case for any
        set_series/set_people/set_topics push from MainApp) snapshots
        the currently-selected value_id before clearing and restores
        it if it still exists in the new items list. Otherwise the
        user's "I'm filtering by topic MDM" gets silently reset to
        index 0 every time MainApp pushes a fresh choices list
        (which happens on every session selection / classification
        mutation).

        `preserve_selection=False` is used when the view itself
        changed; the previous value_id has no meaning under the
        new view, so the combo starts fresh.

        Always runs under signal suppression so neither the clear()
        nor the re-select fires a spurious filter_changed.
        """
        prev_value_id = self._current_value_id() if preserve_selection else None
        self._suppress_emit = True
        try:
            self._value_combo.clear()
            view = self.current_view()
            if view == VIEW_ALL:
                self._value_combo.addItem("(any)", None)
                self._value_combo.setEnabled(False)
            elif view == VIEW_BY_SERIES:
                self._populate_value_combo(self._series_list, "Pick a series...")
            elif view == VIEW_BY_PERSON:
                self._populate_value_combo(self._people_list, "Pick a person...")
            elif view == VIEW_BY_TOPIC:
                self._populate_value_combo(self._topics_list, "Pick a topic...")
            else:
                self._value_combo.addItem("(invalid view)", None)
                self._value_combo.setEnabled(False)
            # Restore the prior selection if it survived the refresh.
            # Otherwise the combo stays at index 0 (placeholder)
            # which is the right behavior when the previously-
            # selected value is no longer available.
            if prev_value_id is not None:
                for i in range(self._value_combo.count()):
                    if self._value_combo.itemData(i) == prev_value_id:
                        self._value_combo.setCurrentIndex(i)
                        break
        finally:
            self._suppress_emit = False

    def _populate_value_combo(
        self,
        items: list[tuple[int, str]],
        empty_placeholder: str,
    ) -> None:
        if not items:
            self._value_combo.addItem(empty_placeholder, None)
            self._value_combo.setEnabled(False)
            return
        self._value_combo.setEnabled(True)
        for row_id, display in items:
            self._value_combo.addItem(display, row_id)

    def _current_value_id(self) -> Optional[int]:
        data = self._value_combo.currentData()
        return data if isinstance(data, int) else None

    def _emit(self) -> None:
        if self._suppress_emit:
            return
        self.filter_changed.emit(self.current_view(), self._current_value_id())
