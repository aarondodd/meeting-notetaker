"""Typeahead popup for Topics + Series assignment (#82, v0.7.8).

Replaces the QMenu-based Topics picker and the QInputDialog.getItem
Series picker on the classification bar with a unified popup pattern
modelled on GitHub's label picker / Linear's label picker / Notion's
multi-select property.

Public widgets:
    TopicsAssignmentPopup -- multi-select; rows stay open across
        toggles so the user can add several topics in one visit.
    SeriesAssignmentPopup -- single-select; clicking a row sets the
        series and dismisses the popup. "(none)" is pinned at the
        top as the clear/unfile action.

Both extend ``_AssignmentPopupBase`` which owns the common shape:
    - Filter input at the top (also accepts free text for create-new)
    - Scrollable QListWidget below
    - Optional "+ Create '...'" row at the bottom when the typed
      text doesn't match any existing item
    - Dismisses on click-outside / Escape (via Qt.Popup flag)
    - Anchored under an originating widget via ``show_for_widget``

Signals:
    TopicsAssignmentPopup.toggle_requested(name, currently_assigned)
        emitted whenever the user toggles a row. The host should
        emit add_topic_requested for the un-assigned case and
        remove/reject_topic_requested for the assigned case. Stays
        open so the user can continue toggling.
    TopicsAssignmentPopup.reject_suggestion_requested(name)
        emitted when the user clicks the small X on a suggestion
        row. Removes the suggestion without flipping it to accepted.
    SeriesAssignmentPopup.series_chosen(name)
        emitted with the picked series name. Empty string is the
        clear/unfile sentinel. The popup dismisses itself before
        emit.

The host (ClassificationBar) wires these into its existing outward-
facing pyqtSignals (add_topic_requested, remove_topic_requested,
accept_topic_requested, set_series_requested) so MainApp wiring
doesn't churn.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from PyQt6.QtCore import QPoint, Qt, pyqtSignal
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


# Anchor offset below the originating widget. Small gap so the popup
# visually clears the button border.
_ANCHOR_GAP_PX = 4


@dataclass
class AssignmentRow:
    """One row in the popup's list.

    name:       display text + key for the assignment store
    assigned:   currently linked to the active session
    suggested:  came from the LLM auto-extract path (False for
                manually-added items). Only meaningful for Topics
                rows; Series ignores the flag.
    """
    name: str
    assigned: bool = False
    suggested: bool = False


class _AssignmentPopupBase(QFrame):
    """Common chrome: title row, filter input, list, footer hint.

    Subclasses populate the list via ``set_rows`` and wire their own
    item-clicked semantics (toggle vs single-select).
    """

    def __init__(
        self,
        *,
        title: str,
        placeholder: str,
        allow_create: bool = True,
        parent: Optional[QWidget] = None,
    ) -> None:
        # Qt.Popup gives us click-outside dismissal + auto-focus
        # behavior without manually wiring an event filter. The
        # FramelessWindowHint keeps the OS chrome off so the popup
        # reads as an anchored helper, not a separate window.
        super().__init__(
            parent,
            Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self._allow_create = allow_create
        # A subtle border so the popup is visually contained against
        # the underlying chip + content; uses palette colors so it
        # tracks the OS theme.
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFrameShadow(QFrame.Shadow.Plain)
        self.setLineWidth(1)
        self.resize(320, 360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 6)
        layout.setSpacing(6)

        title_label = QLabel(title, self)
        title_label.setStyleSheet("font-weight: 600;")
        layout.addWidget(title_label)

        self._filter = QLineEdit(self)
        self._filter.setPlaceholderText(placeholder)
        self._filter.textChanged.connect(self._on_filter_changed)
        self._filter.returnPressed.connect(self._on_filter_return)
        layout.addWidget(self._filter)

        self._list = QListWidget(self)
        # Pin a slightly tighter row height so the popup fits more
        # items at a glance without scrolling.
        self._list.setSpacing(0)
        self._list.itemActivated.connect(self._on_item_activated)
        self._list.itemClicked.connect(self._on_item_activated)
        layout.addWidget(self._list, 1)

        # Footer: shows the "+ Create 'X'" hint when allow_create is
        # on and the filter text doesn't match an existing row.
        self._create_btn = QPushButton("", self)
        self._create_btn.setVisible(False)
        self._create_btn.clicked.connect(self._on_create_clicked)
        layout.addWidget(self._create_btn)

        # Source-of-truth row list; the QListWidget is rebuilt from
        # this on every filter change.
        self._rows: list[AssignmentRow] = []

    # ---- public API used by the host -------------------------------

    def set_rows(self, rows: Iterable[AssignmentRow]) -> None:
        """Replace the popup's list. Rebuilds the visible items
        through the current filter."""
        self._rows = list(rows)
        self._refresh_list()

    def show_for_widget(self, anchor: QWidget) -> None:
        """Position the popup just under ``anchor`` and show it.

        Clamps to the available screen rect so the popup doesn't
        slide off-screen when the anchor is near the bottom or right
        edge of the display.
        """
        # Anchor at the bottom-left of the originating widget.
        global_pos = anchor.mapToGlobal(
            QPoint(0, anchor.height() + _ANCHOR_GAP_PX),
        )
        # Clamp inside the screen the anchor lives on.
        screen = anchor.screen() or self.screen()
        if screen is not None:
            available = screen.availableGeometry()
            x = max(
                available.left(),
                min(global_pos.x(), available.right() - self.width()),
            )
            y = max(
                available.top(),
                min(global_pos.y(), available.bottom() - self.height()),
            )
            global_pos = QPoint(x, y)
        self.move(global_pos)
        self._filter.clear()
        self._filter.setFocus()
        self.show()

    # ---- subclass hooks --------------------------------------------

    def _activate_row(self, row: AssignmentRow) -> None:
        """Called when the user clicks / Enter-activates a row.
        Subclasses implement the toggle (multi) or pick (single)
        semantics."""
        raise NotImplementedError

    def _create_new(self, name: str) -> None:
        """Called when the user activates the create-new row or
        presses Enter on a non-matching filter."""
        raise NotImplementedError

    def _make_item(self, row: AssignmentRow, filter_text: str) -> QListWidgetItem:
        """Subclass override: build the QListWidgetItem with the
        appropriate visual treatment (check vs radio, suggestion
        badge, reject icon). Default carries a plain check mark
        when assigned."""
        prefix = "[x] " if row.assigned else "[ ] "
        suffix = "  (suggested)" if row.suggested else ""
        item = QListWidgetItem(f"{prefix}{row.name}{suffix}")
        item.setData(Qt.ItemDataRole.UserRole, row.name)
        return item

    # ---- filter + list rebuild -------------------------------------

    def _on_filter_changed(self, text: str) -> None:
        self._refresh_list(text)

    def _refresh_list(self, filter_text: Optional[str] = None) -> None:
        if filter_text is None:
            filter_text = self._filter.text()
        needle = filter_text.strip().casefold()
        self._list.clear()
        any_visible_match = False
        for row in self._rows:
            if needle and needle not in row.name.casefold():
                continue
            any_visible_match = True
            item = self._make_item(row, filter_text)
            self._list.addItem(item)
        # Create-new footer: only when allow_create is on AND the
        # filter has text AND no existing row's name matches the
        # typed text case-insensitively (so the user can't create
        # an exact dup).
        if self._allow_create and needle:
            exact_match = any(
                row.name.casefold() == needle for row in self._rows
            )
            if not exact_match:
                self._create_btn.setText(f"+ Create '{filter_text.strip()}'")
                self._create_btn.setVisible(True)
            else:
                self._create_btn.setVisible(False)
        else:
            self._create_btn.setVisible(False)
        # Highlight the first row so Enter does the expected thing
        # for an unambiguous filter.
        if self._list.count() > 0:
            self._list.setCurrentRow(0)

    def _on_filter_return(self) -> None:
        """Enter in the filter: either activate the highlighted row
        (for an unambiguous filter) or trigger create-new.

        Uses ``isHidden()`` rather than ``isVisible()`` because the
        latter returns False when an ancestor isn't shown -- which
        is the case under offscreen tests that never call show()."""
        current = self._list.currentItem()
        if current is not None:
            self._on_item_activated(current)
            return
        if not self._create_btn.isHidden():
            self._on_create_clicked()

    def _on_item_activated(self, item: QListWidgetItem) -> None:
        name = item.data(Qt.ItemDataRole.UserRole)
        if not name:
            return
        row = next((r for r in self._rows if r.name == name), None)
        if row is None:
            return
        self._activate_row(row)

    def _on_create_clicked(self) -> None:
        name = self._filter.text().strip()
        if not name:
            return
        self._create_new(name)

    # Pressing Escape closes the popup. Qt.Popup gives us
    # click-outside dismiss for free, but the Escape binding from
    # QDialog isn't inherited because we're a QFrame.
    def keyPressEvent(self, event: QKeyEvent) -> None:  # type: ignore[override]
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)


class TopicsAssignmentPopup(_AssignmentPopupBase):
    """Multi-select popup: rows stay live across toggles.

    Emits:
        toggle_requested(name, currently_assigned) -- the host
            should emit add_topic_requested for currently_assigned
            False, and remove or accept for True. The popup updates
            its own visible state immediately so the user sees the
            check flip without waiting for the round-trip.
        reject_suggestion_requested(name) -- the small X on a
            suggestion row. Removes the suggestion entirely.
    """

    toggle_requested = pyqtSignal(str, bool)  # name, currently_assigned
    reject_suggestion_requested = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            title="Topics",
            placeholder="Filter or type a new topic...",
            allow_create=True,
            parent=parent,
        )

    def _activate_row(self, row: AssignmentRow) -> None:
        # Optimistically flip the visible state so the popup feels
        # snappy; the host's update_assignments call will reconcile
        # if anything diverges.
        for r in self._rows:
            if r.name == row.name:
                r.assigned = not r.assigned
                break
        self._refresh_list()
        self.toggle_requested.emit(row.name, row.assigned)

    def _create_new(self, name: str) -> None:
        # Append the new row in-memory + flip it assigned so the
        # user sees the result immediately. Host wiring writes the
        # store + re-pushes the canonical list on the next refresh.
        if any(r.name.casefold() == name.casefold() for r in self._rows):
            return
        new_row = AssignmentRow(name=name, assigned=True, suggested=False)
        self._rows.append(new_row)
        # Clear the filter so the created row is visible in the
        # refreshed list.
        self._filter.clear()
        self._refresh_list()
        self.toggle_requested.emit(name, True)


class SeriesAssignmentPopup(_AssignmentPopupBase):
    """Single-select popup: clicking a row sets the series and
    dismisses the popup. "(none)" is pinned at the top as the
    clear/unfile sentinel.

    Emits:
        series_chosen(name) -- the picked series name. Empty string
            means clear / unfile. The popup closes itself before
            emit so the host doesn't need to coordinate dismiss.
    """

    NONE_SENTINEL = "(none)"

    series_chosen = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            title="Series",
            placeholder="Filter or type a new series...",
            allow_create=True,
            parent=parent,
        )

    def set_rows_and_current(
        self,
        names: Iterable[str],
        current: str,
    ) -> None:
        """Series-specific helper: rebuild from a names list +
        flag the current pick. Prepends the "(none)" sentinel."""
        sentinel = AssignmentRow(
            name=self.NONE_SENTINEL,
            assigned=(not current),
            suggested=False,
        )
        rows = [sentinel] + [
            AssignmentRow(name=n, assigned=(n == current), suggested=False)
            for n in names
        ]
        self.set_rows(rows)

    def _activate_row(self, row: AssignmentRow) -> None:
        emitted = "" if row.name == self.NONE_SENTINEL else row.name
        # Close first so the host's repaint runs against a clean
        # widget tree. Qt re-entrancy via emit-then-close occasionally
        # surfaces the popup briefly on top after the host repaints.
        self.close()
        self.series_chosen.emit(emitted)

    def _create_new(self, name: str) -> None:
        self.close()
        self.series_chosen.emit(name)

    def _make_item(self, row: AssignmentRow, filter_text: str) -> QListWidgetItem:
        # Single-select: render the current pick with a filled
        # bullet, others with a hollow bullet. (No suggested concept
        # for series.)
        prefix = "(*) " if row.assigned else "( ) "
        item = QListWidgetItem(f"{prefix}{row.name}")
        item.setData(Qt.ItemDataRole.UserRole, row.name)
        return item
