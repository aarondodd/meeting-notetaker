"""Collapsible session-attendees table for My Notes / Synthesis tabs.

Issue #51 Phase 3. Surfaces the rich Contact fields (title, company,
department, primary email, phone) for every Contact linked to the
current session, alongside a source emoji that tells the user at a
glance whether the data came from Outlook, an LLM extraction, or a
manual edit.

The drawer is collapsed by default so it doesn't push the editor
content down. The user clicks the expand chevron to see the table;
state is per-instance, not persisted (intentional for v0.7.2 -- if
users find themselves expanding it every time, we'll wire it to
session metadata).

Row click emits ``contact_clicked(contact_id)`` so the SessionView
can open the Address Book filtered to that contact.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


# Same source-emoji map used in the Address Book dialog. Duplicated
# here rather than imported because the dialog imports the model
# layer, and the drawer should be independent of dialog internals.
# If the icon set needs to change, update both.
_SOURCE_EMOJI = {
    "outlook": "\U0001F4E7",  # envelope
    "llm": "\U0001F916",      # robot
    "manual": "✋",       # raised hand -- "user touched this"
}


def _badge_for(source) -> str:
    return _SOURCE_EMOJI.get(source or "", "")


def _cell_with_badge(value, source) -> str:
    """Format a value cell with a trailing source emoji when set.

    Empty values stay empty so the cell isn't a lone badge. Trailing
    space + emoji keeps the value readable + the badge visually
    distinct without a separator character.
    """
    text = value or ""
    badge = _badge_for(source)
    if text and badge:
        return f"{text} {badge}"
    return text


class AttendeeDetailsDrawer(QWidget):
    """Collapsible attendees-with-rich-fields table.

    Construct, hand the SessionView a ``set_contacts(contacts)``
    handle to refresh the table whenever the session's attendee
    list changes. ``contact_clicked(int)`` fires when the user
    clicks a row; the caller routes to the Address Book.
    """

    contact_clicked = pyqtSignal(int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        # Frame so the drawer reads as a discrete UI element above
        # the editor rather than a stray table floating in the pane.
        self.setFrameShape = QFrame.Shape.StyledPanel  # noqa: defensive
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header row: toggle + label + count.
        header = QFrame(self)
        header.setStyleSheet(
            "QFrame { background-color: palette(alternate-base); }"
        )
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(6, 4, 6, 4)
        header_layout.setSpacing(8)
        self._toggle = QPushButton("▶", header)  # right-pointing triangle
        self._toggle.setFlat(True)
        self._toggle.setFixedWidth(20)
        self._toggle.clicked.connect(self._on_toggle)
        header_layout.addWidget(self._toggle)
        self._title = QLabel("Attendees (0)", header)
        font = QFont(self._title.font())
        font.setBold(True)
        self._title.setFont(font)
        header_layout.addWidget(self._title)
        header_layout.addStretch(1)
        layout.addWidget(header)

        # Body: a table that's hidden by default.
        self._body = QFrame(self)
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(6, 4, 6, 6)
        body_layout.setSpacing(0)
        self._table = QTableWidget(0, 6, self._body)
        self._table.setHorizontalHeaderLabels([
            "Name", "Title", "Company", "Email", "Notes", "",
        ])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        # No selection highlight + no hover highlight -- the drawer
        # is a read-only summary, the Edit button is the only
        # actionable element, so the default click/hover row tint
        # was just visual noise (2026-05-28 feedback).
        self._table.setSelectionMode(
            QTableWidget.SelectionMode.NoSelection,
        )
        self._table.setMouseTracking(False)
        self._table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._table.setStyleSheet(
            "QTableWidget::item:hover { background: transparent; }"
            "QTableWidget::item:selected {"
            " background: transparent; color: palette(text); }"
        )
        self._table.setAlternatingRowColors(True)
        h = self._table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(5, 32)
        self._table.cellDoubleClicked.connect(self._on_cell_clicked)
        body_layout.addWidget(self._table)
        self._body.setVisible(False)
        layout.addWidget(self._body)

        # Per-row contact id stash so cell-click can map back. Lives
        # on the row's first cell via setData(UserRole).
        self._row_contact_ids: list[int] = []
        # Tracked explicitly so is_expanded() is reliable in tests +
        # before the widget enters the shown hierarchy. setVisible
        # alone makes isVisible() return False until a shown ancestor
        # exists; the intent flag is independent of paint state.
        self._is_expanded = False

    # ---- public API ---------------------------------------------------

    def set_contacts(self, contacts) -> None:
        """Refresh the table from a list of Contact objects.

        Empty list -> drawer header shows "Attendees (0)" and the
        body table is empty; the drawer remains expandable so the
        user can verify "no contacts yet" rather than wonder if the
        drawer is broken.
        """
        contacts = list(contacts or [])
        self._title.setText(f"Attendees ({len(contacts)})")
        self._table.setRowCount(len(contacts))
        self._row_contact_ids = [c.id for c in contacts]
        for row, c in enumerate(contacts):
            # Name cell keeps the rollup badge -- one icon at the row
            # head signals "at least one field came from <source>"
            # without the user scanning every column.
            rollup = _badge_for(getattr(c, "last_enriched_source", None))
            name_text = f"{c.display_name} {rollup}".strip()
            name_item = QTableWidgetItem(name_text)
            name_item.setData(Qt.ItemDataRole.UserRole, c.id)
            self._table.setItem(row, 0, name_item)
            self._table.setItem(row, 1, QTableWidgetItem(
                _cell_with_badge(c.title, getattr(c, "title_source", None)),
            ))
            self._table.setItem(row, 2, QTableWidgetItem(
                _cell_with_badge(c.company, getattr(c, "company_source", None)),
            ))
            self._table.setItem(row, 3, QTableWidgetItem(
                _cell_with_badge(
                    c.primary_email,
                    getattr(c, "primary_email_source", None),
                ),
            ))
            notes_value = (getattr(c, "notes", "") or "")
            notes_item = QTableWidgetItem(
                _cell_with_badge(notes_value, getattr(c, "notes_source", None)),
            )
            # Multi-line notes get a tooltip with the full text so the
            # truncated single-line cell display isn't a dead-end.
            if notes_value:
                notes_item.setToolTip(notes_value)
            self._table.setItem(row, 4, notes_item)
            self._table.setItem(row, 5, QTableWidgetItem(""))
            self._install_edit_button(row, c.id)
        # If there's nothing to show, collapse so the drawer isn't
        # taking visual space with an empty table.
        if not contacts and self._is_expanded:
            self._set_expanded(False)

    def set_expanded(self, expanded: bool) -> None:
        """Programmatic expand/collapse. Used by the SessionView when
        restoring a prior state (future: per-session persistence)."""
        self._set_expanded(expanded)

    def is_expanded(self) -> bool:
        return self._is_expanded

    # ---- internals ----------------------------------------------------

    def _on_toggle(self) -> None:
        self._set_expanded(not self._is_expanded)

    def _set_expanded(self, expanded: bool) -> None:
        self._is_expanded = expanded
        self._body.setVisible(expanded)
        # Triangle: right when collapsed, down when expanded.
        self._toggle.setText("▼" if expanded else "▶")

    def _on_cell_clicked(self, row: int, _col: int) -> None:
        if 0 <= row < len(self._row_contact_ids):
            self.contact_clicked.emit(self._row_contact_ids[row])

    def _install_edit_button(self, row: int, contact_id: int) -> None:
        """Embed a small Edit button in the rightmost column.

        Single click routes through ``contact_clicked`` so the
        SessionView opens the Address Book filtered to this contact.
        Pencil glyph picked to read as "edit"; matches the "manual"
        source emoji intentionally -- both mean "human-editable".
        """
        btn = QPushButton("✎", self._table)  # lower-right pencil
        btn.setFlat(True)
        btn.setFixedWidth(28)
        btn.setToolTip("Edit contact in Address Book")
        btn.clicked.connect(
            lambda _checked=False, cid=contact_id: self.contact_clicked.emit(cid),
        )
        self._table.setCellWidget(row, 5, btn)
