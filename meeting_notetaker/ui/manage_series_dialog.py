"""Series catalog management (File > Manage Series).

Lists every series with its session count + created date. Per-row
Rename / Merge / Delete actions, all writing through the
ClassificationStore directly (no separate apply step). Bottom-bar
Refresh + Close.

Mirrors SpeakersManageDialog's shape so users get a consistent
"manage <thing>" UX across the app:

* Rename            -- prompts for a new name, calls rename_series;
                       errors out cleanly on collision.
* Merge into...     -- picks a target series, reassigns every
                       source session, deletes the source.
* Delete            -- confirms, then drops the series; affected
                       sessions become unfiled (no series_id).

The dialog is constructed with a ClassificationStore the caller
opened. Mutations are visible immediately because every action
refreshes the table from the store; MainApp's navigator refresh
runs after the dialog closes so the filter pulldown reflects the
new catalog.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..models.classification import ClassificationStore, Series


_NAME_COL = 0
_COUNT_COL = 1
_CREATED_COL = 2


class ManageSeriesDialog(QDialog):
    """Edit the classification.db series catalog."""

    def __init__(
        self,
        store: ClassificationStore,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self.setWindowTitle("Manage Series")
        self.resize(620, 440)

        layout = QVBoxLayout(self)

        blurb = QLabel(
            "Series group recurring meetings under a stable name "
            "(e.g. \"Platform Team Sync\"). Sessions assigned to a "
            "series can be filtered to from the navigator pulldown.\n\n"
            "Rename updates the name in place; Merge moves every "
            "session of one series to another and deletes the empty "
            "source; Delete drops the series and leaves its sessions "
            "unfiled.",
            self,
        )
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        self._table = QTableWidget(self)
        self._table.setColumnCount(3)
        self._table.setHorizontalHeaderLabels([
            "Name", "Sessions", "Created",
        ])
        self._table.horizontalHeader().setSectionResizeMode(
            _NAME_COL, QHeaderView.ResizeMode.Stretch,
        )
        self._table.horizontalHeader().setSectionResizeMode(
            _COUNT_COL, QHeaderView.ResizeMode.ResizeToContents,
        )
        self._table.horizontalHeader().setSectionResizeMode(
            _CREATED_COL, QHeaderView.ResizeMode.ResizeToContents,
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows,
        )
        self._table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection,
        )
        self._table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers,
        )
        self._table.itemSelectionChanged.connect(self._refresh_action_buttons)
        self._table.itemDoubleClicked.connect(self._on_double_click_rename)
        layout.addWidget(self._table, 1)

        action_row = QHBoxLayout()
        self._rename_btn = QPushButton("Rename...", self)
        self._rename_btn.clicked.connect(self._on_rename)
        action_row.addWidget(self._rename_btn)
        self._merge_btn = QPushButton("Merge into...", self)
        self._merge_btn.setToolTip(
            "Move every session of the selected series into another "
            "series, then delete the source."
        )
        self._merge_btn.clicked.connect(self._on_merge)
        action_row.addWidget(self._merge_btn)
        self._delete_btn = QPushButton("Delete...", self)
        self._delete_btn.setToolTip(
            "Drop the selected series. Its sessions become unfiled "
            "(no series_id) -- their transcripts, notes, and audio "
            "are untouched."
        )
        self._delete_btn.clicked.connect(self._on_delete)
        action_row.addWidget(self._delete_btn)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        button_row = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Close, self,
        )
        button_row.rejected.connect(self.reject)
        button_row.accepted.connect(self.accept)
        layout.addWidget(button_row)

        self._reload()

    # ---- table refresh ----
    def _reload(self) -> None:
        series = self._store.list_series()
        self._table.setRowCount(len(series))
        for row, s in enumerate(series):
            count = len(self._store.session_ids_for_series(s.id))
            name_item = QTableWidgetItem(s.name)
            name_item.setData(Qt.ItemDataRole.UserRole, s.id)
            count_item = QTableWidgetItem(str(count))
            count_item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            )
            created_item = QTableWidgetItem(_format_created(s.created_at))
            self._table.setItem(row, _NAME_COL, name_item)
            self._table.setItem(row, _COUNT_COL, count_item)
            self._table.setItem(row, _CREATED_COL, created_item)
        self._refresh_action_buttons()

    def _refresh_action_buttons(self) -> None:
        has_selection = self._selected_series_id() is not None
        all_series = self._store.list_series()
        # Merge needs at least two series total -- you can't merge
        # the only series anywhere.
        self._merge_btn.setEnabled(has_selection and len(all_series) >= 2)
        self._rename_btn.setEnabled(has_selection)
        self._delete_btn.setEnabled(has_selection)

    def _selected_series_id(self) -> Optional[int]:
        items = self._table.selectedItems()
        if not items:
            return None
        # First column carries the id.
        return self._table.item(items[0].row(), _NAME_COL).data(
            Qt.ItemDataRole.UserRole,
        )

    def _selected_series_name(self) -> str:
        items = self._table.selectedItems()
        if not items:
            return ""
        return self._table.item(items[0].row(), _NAME_COL).text()

    # ---- mutators ----
    def _on_double_click_rename(self, _item: QTableWidgetItem) -> None:
        # Double-click is a discoverable shortcut for the most common
        # action; same code path as the Rename button.
        self._on_rename()

    def _on_rename(self) -> None:
        sid = self._selected_series_id()
        if sid is None:
            return
        current = self._selected_series_name()
        new_name, ok = QInputDialog.getText(
            self, "Rename Series",
            "New name:",
            QLineEdit.EchoMode.Normal,
            current,
        )
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name:
            QMessageBox.warning(
                self, "Rename Series", "Name cannot be empty.",
            )
            return
        if new_name == current:
            return
        # Collision check -- a case-insensitive duplicate would
        # land an existing series under a confusing name.
        existing = self._store.find_series_by_name(new_name)
        if existing is not None and existing.id != sid:
            confirm = QMessageBox.question(
                self, "Series exists",
                f"A series named \"{existing.name}\" already exists. "
                "Merge this series into it instead?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return
            self._store.merge_series(sid, existing.id)
        else:
            try:
                self._store.rename_series(sid, new_name)
            except Exception as exc:
                QMessageBox.warning(
                    self, "Rename Series",
                    f"Could not rename: {exc}",
                )
                return
        self._reload()

    def _on_merge(self) -> None:
        sid = self._selected_series_id()
        if sid is None:
            return
        source_name = self._selected_series_name()
        # Build the picker from every OTHER series. Same dialog
        # shape ClassificationBar uses for its dropdown+text combo.
        candidates = [
            s for s in self._store.list_series() if s.id != sid
        ]
        if not candidates:
            QMessageBox.information(
                self, "Merge Series",
                "No other series to merge into.",
            )
            return
        names = sorted(s.name for s in candidates)
        target_name, ok = QInputDialog.getItem(
            self, "Merge Series",
            f"Move every session of \"{source_name}\" into:",
            names, 0, False,  # NOT editable -- must pick existing
        )
        if not ok:
            return
        target = next(
            (s for s in candidates if s.name == target_name), None,
        )
        if target is None:
            return
        confirm = QMessageBox.question(
            self, "Merge Series",
            f"Move all sessions of \"{source_name}\" into "
            f"\"{target.name}\"? \"{source_name}\" will be deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            self._store.merge_series(sid, target.id)
        except Exception as exc:
            QMessageBox.warning(
                self, "Merge Series", f"Could not merge: {exc}",
            )
            return
        self._reload()

    def _on_delete(self) -> None:
        sid = self._selected_series_id()
        if sid is None:
            return
        name = self._selected_series_name()
        count = len(self._store.session_ids_for_series(sid))
        message = f"Delete the series \"{name}\"?"
        if count:
            message += (
                f"\n\n{count} session(s) currently in this series "
                "will become unfiled. Their transcripts, notes, "
                "and audio are not affected."
            )
        confirm = QMessageBox.question(
            self, "Delete Series",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            self._store.delete_series(sid)
        except Exception as exc:
            QMessageBox.warning(
                self, "Delete Series", f"Could not delete: {exc}",
            )
            return
        self._reload()


def _format_created(iso_str: str) -> str:
    """UTC ISO -> 'YYYY-MM-DD' (local). Just the date; the time
    isn't useful for series metadata."""
    if not iso_str:
        return ""
    try:
        from datetime import datetime as _dt
        utc_aware = _dt.fromisoformat(iso_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return iso_str
    return utc_aware.astimezone().strftime("%Y-%m-%d")
