"""Editor for the four LLM-derived appendix sections.

Tabbed dialog: one tab per section (Attendee Context, Attendee
Details, Suggested Topics, Referenced Attachments). Each tab
exposes a ``QTableWidget`` with editable cells + Add Row / Delete
Selected buttons. On OK, the edited entries get written to the
sidecar and the raw JSON blocks in ``notes.md`` get re-rendered
so the two stay in sync. Cancel discards all edits.

Why edit notes.md alongside the sidecar: the debounced
``_flush_notes`` re-parses ``notes.md`` after every save, so if
the dialog only touched the sidecar, the next keystroke would
re-import the LLM's original observations and stomp the user's
edits. Mirroring back to notes.md keeps both surfaces in lockstep.

The Suggested Topics tab is a single-column "Topic" table since
its entries are bare strings, not records.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..utils.attendee_appendix import AttendeeAppendixEntry
from ..utils.attendee_context import AttendeeContextEntry
from ..utils.invite_mentions import InviteMentionEntry


_CONTEXT_COLS = ("Name", "Observation")
_DETAILS_COLS = ("Name", "Title", "Company", "Department", "Email", "Phone")
_TOPICS_COLS = ("Topic",)
_REFERENCED_COLS = ("Name", "Context")


class _SectionTab(QWidget):
    """Editable table + Add / Delete buttons for one section."""

    def __init__(
        self,
        columns: tuple[str, ...],
        rows: list[list[str]],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)
        self._columns = columns

        self._table = QTableWidget(len(rows), len(columns), self)
        self._table.setHorizontalHeaderLabels(list(columns))
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows,
        )
        self._table.setSelectionMode(
            QTableWidget.SelectionMode.ExtendedSelection,
        )
        header = self._table.horizontalHeader()
        for col in range(len(columns) - 1):
            header.setSectionResizeMode(
                col, QHeaderView.ResizeMode.ResizeToContents,
            )
        header.setSectionResizeMode(
            len(columns) - 1, QHeaderView.ResizeMode.Stretch,
        )
        for row_idx, row in enumerate(rows):
            for col_idx, value in enumerate(row):
                self._table.setItem(
                    row_idx, col_idx, QTableWidgetItem(str(value or "")),
                )
        outer.addWidget(self._table, 1)

        controls = QHBoxLayout()
        controls.setSpacing(6)
        self._add_btn = QPushButton("Add Row", self)
        self._add_btn.clicked.connect(self._on_add)
        controls.addWidget(self._add_btn)
        self._del_btn = QPushButton("Delete Selected", self)
        self._del_btn.clicked.connect(self._on_delete)
        controls.addWidget(self._del_btn)
        controls.addStretch(1)
        outer.addLayout(controls)

    def collect(self) -> list[list[str]]:
        """Return the table's current rows as a list of column-value
        lists. Cells that round-trip to empty strings after .strip()
        are reported as "". Caller is expected to discard rows whose
        identifier (first non-empty column, typically Name) is empty.
        """
        rows: list[list[str]] = []
        for r in range(self._table.rowCount()):
            row: list[str] = []
            for c in range(self._table.columnCount()):
                item = self._table.item(r, c)
                row.append((item.text() if item is not None else "").strip())
            rows.append(row)
        return rows

    def _on_add(self) -> None:
        r = self._table.rowCount()
        self._table.insertRow(r)
        for c in range(self._table.columnCount()):
            self._table.setItem(r, c, QTableWidgetItem(""))
        # Drop focus into the new row's first cell so the user can
        # type immediately.
        self._table.editItem(self._table.item(r, 0))

    def _on_delete(self) -> None:
        # Walk selected rows in reverse so removeRow doesn't shift
        # indices out from under us.
        rows = sorted(
            {idx.row() for idx in self._table.selectedIndexes()},
            reverse=True,
        )
        for r in rows:
            self._table.removeRow(r)


class AppendixEditDialog(QDialog):
    """Edit the four LLM-derived appendix sections in tabular form."""

    def __init__(
        self,
        *,
        attendee_context: list[AttendeeContextEntry],
        attendee_details: list[AttendeeAppendixEntry],
        topics: list[str],
        referenced_attachments: list[InviteMentionEntry],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Appendix")
        self.setModal(True)
        self.resize(820, 520)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        self._tabs = QTabWidget(self)
        self._context_tab = _SectionTab(
            _CONTEXT_COLS,
            [[e.name, e.observation] for e in attendee_context],
            self,
        )
        self._details_tab = _SectionTab(
            _DETAILS_COLS,
            [
                [e.name, e.title, e.company, e.department, e.email, e.phone]
                for e in attendee_details
            ],
            self,
        )
        self._topics_tab = _SectionTab(
            _TOPICS_COLS,
            [[t] for t in topics],
            self,
        )
        self._referenced_tab = _SectionTab(
            _REFERENCED_COLS,
            [[e.name, e.context] for e in referenced_attachments],
            self,
        )
        self._tabs.addTab(self._context_tab, "Attendee Context")
        self._tabs.addTab(self._details_tab, "Attendee Details")
        self._tabs.addTab(self._topics_tab, "Suggested Topics")
        self._tabs.addTab(self._referenced_tab, "Referenced Attachments")
        outer.addWidget(self._tabs, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    # ---- result extraction --------------------------------------------

    def attendee_context(self) -> list[AttendeeContextEntry]:
        out: list[AttendeeContextEntry] = []
        for row in self._context_tab.collect():
            name, obs = row + [""] * (2 - len(row))
            if not name:
                continue
            out.append(AttendeeContextEntry(name=name, observation=obs))
        return out

    def attendee_details(self) -> list[AttendeeAppendixEntry]:
        out: list[AttendeeAppendixEntry] = []
        for row in self._details_tab.collect():
            row = row + [""] * (6 - len(row))
            name, title, company, dept, email, phone = row[:6]
            if not name:
                continue
            out.append(AttendeeAppendixEntry(
                name=name, title=title, company=company,
                department=dept, email=email, phone=phone,
            ))
        return out

    def topics(self) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for row in self._topics_tab.collect():
            topic = (row[0] if row else "").strip()
            if not topic:
                continue
            key = topic.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(topic)
        return out

    def referenced_attachments(self) -> list[InviteMentionEntry]:
        out: list[InviteMentionEntry] = []
        for row in self._referenced_tab.collect():
            row = row + [""] * (2 - len(row))
            name, context = row[:2]
            if not name:
                continue
            out.append(InviteMentionEntry(name=name, context=context))
        return out
