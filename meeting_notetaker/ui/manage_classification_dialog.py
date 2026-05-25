"""Manage Classification dialog (Series + Topics tabs).

People moved to the Address Book in Phase 2 (issue #28); this
dialog now covers Series + Topics only. Two tabs, one
QDialog wrapper, Close button at the bottom.

Each tab is a CatalogManagerWidget configured for its catalog --
the widget owns the table + Rename/Merge/Delete row actions +
optional bulk-delete-orphans button. Mutating one tab doesn't
disturb the other; both refresh on tab switch.
"""
from __future__ import annotations

from typing import Callable, Optional, Protocol

from PyQt6.QtCore import Qt
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
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..models.classification import ClassificationStore


class CatalogAdapter(Protocol):
    """How a catalog (Series / Topics) is read + mutated.

    CatalogManagerWidget delegates to one of these per tab so the
    widget itself stays catalog-agnostic.
    """
    singular: str          # "Series" / "Topic"
    plural: str            # "Series" / "Topics"
    unit: str              # "Sessions" (column header for the count column)

    def list_items(self) -> list[tuple[int, str, int, str]]:
        """Return (id, name, session_count, created_at) per row."""
        ...

    def find_by_name(self, name: str): ...
    def rename(self, item_id: int, new_name: str) -> None: ...
    def merge(self, source_id: int, target_id: int) -> None: ...
    def delete(self, item_id: int) -> None: ...

    def supports_bulk_orphan_delete(self) -> bool: ...
    def list_orphans(self) -> list[tuple[int, str]]:
        """For the bulk-delete UI -- (id, name) per orphan."""
        ...
    def delete_orphans(self) -> int: ...


class _SeriesAdapter:
    singular = "Series"
    plural = "Series"
    unit = "Sessions"

    def __init__(self, store: ClassificationStore) -> None:
        self._store = store

    def list_items(self) -> list[tuple[int, str, int, str]]:
        out: list[tuple[int, str, int, str]] = []
        for s in self._store.list_series():
            n = len(self._store.session_ids_for_series(s.id))
            out.append((s.id, s.name, n, s.created_at))
        return out

    def find_by_name(self, name: str):
        return self._store.find_series_by_name(name)

    def rename(self, item_id: int, new_name: str) -> None:
        self._store.rename_series(item_id, new_name)

    def merge(self, source_id: int, target_id: int) -> None:
        self._store.merge_series(source_id, target_id)

    def delete(self, item_id: int) -> None:
        self._store.delete_series(item_id)

    def supports_bulk_orphan_delete(self) -> bool:
        return False

    def list_orphans(self) -> list[tuple[int, str]]:
        return []

    def delete_orphans(self) -> int:
        return 0


class _TopicsAdapter:
    singular = "Topic"
    plural = "Topics"
    unit = "Sessions"

    def __init__(self, store: ClassificationStore) -> None:
        self._store = store

    def list_items(self) -> list[tuple[int, str, int, str]]:
        out: list[tuple[int, str, int, str]] = []
        for t in self._store.list_topics():
            n = self._store.session_count_for_topic(t.id, accepted_only=True)
            out.append((t.id, t.name, n, t.created_at))
        return out

    def find_by_name(self, name: str):
        for t in self._store.list_topics():
            if t.name.casefold() == name.casefold():
                return t
        return None

    def rename(self, item_id: int, new_name: str) -> None:
        self._store.rename_topic(item_id, new_name)

    def merge(self, source_id: int, target_id: int) -> None:
        self._store.merge_topics(source_id, target_id)

    def delete(self, item_id: int) -> None:
        self._store.delete_topic(item_id)

    def supports_bulk_orphan_delete(self) -> bool:
        return True

    def list_orphans(self) -> list[tuple[int, str]]:
        return [(t.id, t.name) for t in self._store.list_orphan_topics()]

    def delete_orphans(self) -> int:
        return self._store.delete_orphan_topics()


_NAME_COL = 0
_COUNT_COL = 1
_CREATED_COL = 2


class CatalogManagerWidget(QWidget):
    """Reusable catalog editor (table + Rename / Merge / Delete +
    optional bulk-orphan-delete).

    The widget is catalog-agnostic; the CatalogAdapter passed in
    decides what's being managed."""

    def __init__(
        self,
        adapter: CatalogAdapter,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._adapter = adapter

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._table = QTableWidget(self)
        self._table.setColumnCount(3)
        self._table.setHorizontalHeaderLabels([
            "Name", adapter.unit, "Created",
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
        self._table.itemSelectionChanged.connect(self._refresh_buttons)
        self._table.itemDoubleClicked.connect(self._on_double_click_rename)
        layout.addWidget(self._table, 1)

        action_row = QHBoxLayout()
        self._rename_btn = QPushButton("Rename...", self)
        self._rename_btn.clicked.connect(self._on_rename)
        action_row.addWidget(self._rename_btn)
        self._merge_btn = QPushButton("Merge into...", self)
        self._merge_btn.clicked.connect(self._on_merge)
        action_row.addWidget(self._merge_btn)
        self._delete_btn = QPushButton("Delete...", self)
        self._delete_btn.clicked.connect(self._on_delete)
        action_row.addWidget(self._delete_btn)
        action_row.addStretch(1)
        if self._adapter.supports_bulk_orphan_delete():
            self._cleanup_btn = QPushButton("Cleanup orphans...", self)
            self._cleanup_btn.setToolTip(
                f"Delete every {self._adapter.singular.lower()} with no "
                "session associations."
            )
            self._cleanup_btn.clicked.connect(self._on_cleanup_orphans)
            action_row.addWidget(self._cleanup_btn)
        else:
            self._cleanup_btn = None
        layout.addLayout(action_row)

        self.reload()

    # ---- public ----
    def reload(self) -> None:
        items = self._adapter.list_items()
        self._table.setRowCount(len(items))
        for row, (item_id, name, count, created) in enumerate(items):
            name_item = QTableWidgetItem(name)
            name_item.setData(Qt.ItemDataRole.UserRole, item_id)
            count_item = QTableWidgetItem(str(count))
            count_item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            )
            created_item = QTableWidgetItem(_format_created(created))
            self._table.setItem(row, _NAME_COL, name_item)
            self._table.setItem(row, _COUNT_COL, count_item)
            self._table.setItem(row, _CREATED_COL, created_item)
        self._refresh_buttons()

    # ---- internals ----
    def _refresh_buttons(self) -> None:
        sel = self._selected_id()
        all_items = self._adapter.list_items()
        has_selection = sel is not None
        self._rename_btn.setEnabled(has_selection)
        self._merge_btn.setEnabled(has_selection and len(all_items) >= 2)
        self._delete_btn.setEnabled(has_selection)
        if self._cleanup_btn is not None:
            orphans = self._adapter.list_orphans()
            self._cleanup_btn.setEnabled(bool(orphans))
            self._cleanup_btn.setText(
                f"Cleanup orphans ({len(orphans)})..."
                if orphans else "Cleanup orphans..."
            )

    def _selected_id(self) -> Optional[int]:
        items = self._table.selectedItems()
        if not items:
            return None
        return self._table.item(items[0].row(), _NAME_COL).data(
            Qt.ItemDataRole.UserRole,
        )

    def _selected_name(self) -> str:
        items = self._table.selectedItems()
        if not items:
            return ""
        return self._table.item(items[0].row(), _NAME_COL).text()

    def _on_double_click_rename(self, _item: QTableWidgetItem) -> None:
        self._on_rename()

    def _on_rename(self) -> None:
        item_id = self._selected_id()
        if item_id is None:
            return
        current = self._selected_name()
        new_name, ok = QInputDialog.getText(
            self, f"Rename {self._adapter.singular}",
            "New name:",
            QLineEdit.EchoMode.Normal,
            current,
        )
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == current:
            return
        existing = self._adapter.find_by_name(new_name)
        if existing is not None and existing.id != item_id:
            confirm = QMessageBox.question(
                self, f"{self._adapter.singular} exists",
                f"A {self._adapter.singular.lower()} named "
                f"\"{existing.name if hasattr(existing, 'name') else new_name}\" "
                "already exists. Merge into it instead?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return
            try:
                self._adapter.merge(item_id, existing.id)
            except Exception as exc:
                QMessageBox.warning(
                    self, f"Rename {self._adapter.singular}",
                    f"Could not merge: {exc}",
                )
                return
        else:
            try:
                self._adapter.rename(item_id, new_name)
            except Exception as exc:
                QMessageBox.warning(
                    self, f"Rename {self._adapter.singular}",
                    f"Could not rename: {exc}",
                )
                return
        self.reload()

    def _on_merge(self) -> None:
        item_id = self._selected_id()
        if item_id is None:
            return
        source_name = self._selected_name()
        candidates = [
            (cid, name) for (cid, name, *_rest)
            in self._adapter.list_items() if cid != item_id
        ]
        if not candidates:
            QMessageBox.information(
                self, f"Merge {self._adapter.singular}",
                f"No other {self._adapter.plural.lower()} to merge into.",
            )
            return
        names = sorted(name for _id, name in candidates)
        target_name, ok = QInputDialog.getItem(
            self, f"Merge {self._adapter.singular}",
            f"Move every session of \"{source_name}\" into:",
            names, 0, False,
        )
        if not ok:
            return
        target_id = next(
            (cid for cid, name in candidates if name == target_name), None,
        )
        if target_id is None:
            return
        confirm = QMessageBox.question(
            self, f"Merge {self._adapter.singular}",
            f"Move all sessions of \"{source_name}\" into "
            f"\"{target_name}\"? \"{source_name}\" will be deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            self._adapter.merge(item_id, target_id)
        except Exception as exc:
            QMessageBox.warning(
                self, f"Merge {self._adapter.singular}",
                f"Could not merge: {exc}",
            )
            return
        self.reload()

    def _on_delete(self) -> None:
        item_id = self._selected_id()
        if item_id is None:
            return
        name = self._selected_name()
        items = self._adapter.list_items()
        count = 0
        for (cid, _n, c, _created) in items:
            if cid == item_id:
                count = c
                break
        message = (
            f"Delete the {self._adapter.singular.lower()} \"{name}\"?"
        )
        if count:
            message += (
                f"\n\n{count} session(s) currently associated will lose "
                "this link. Transcripts / notes / audio are not affected."
            )
        confirm = QMessageBox.question(
            self, f"Delete {self._adapter.singular}",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            self._adapter.delete(item_id)
        except Exception as exc:
            QMessageBox.warning(
                self, f"Delete {self._adapter.singular}",
                f"Could not delete: {exc}",
            )
            return
        self.reload()

    def _on_cleanup_orphans(self) -> None:
        orphans = self._adapter.list_orphans()
        if not orphans:
            return
        confirm = QMessageBox.question(
            self, f"Cleanup {self._adapter.plural} orphans",
            f"Delete {len(orphans)} {self._adapter.singular.lower()}(s) "
            "with zero session associations? Cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._adapter.delete_orphans()
        self.reload()


class ManageClassificationDialog(QDialog):
    """Tabbed catalog editor for Series + Topics.

    People migrated to the Address Book in Phase 2.
    """

    def __init__(
        self,
        store: ClassificationStore,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Manage Classification")
        self.resize(680, 520)

        layout = QVBoxLayout(self)
        blurb = QLabel(
            "Edit your Series + Topics catalogs. Per-tab: rename, "
            "merge two together (every session of A becomes a session "
            "of B; A is deleted), or delete (the affected sessions "
            "lose the link; their notes / transcripts / audio are "
            "untouched).\n\n"
            "People moved to the unified Address Book "
            "(File > Address Book...) -- aliases let typing 'BS' "
            "resolve to the Bob Smith contact across People + Speakers.",
            self,
        )
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        self._tabs = QTabWidget(self)
        self._series_widget = CatalogManagerWidget(_SeriesAdapter(store), self)
        self._topics_widget = CatalogManagerWidget(_TopicsAdapter(store), self)
        self._tabs.addTab(self._series_widget, "Series")
        self._tabs.addTab(self._topics_widget, "Topics")
        self._tabs.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self._tabs, 1)

        button_row = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Close, self,
        )
        button_row.rejected.connect(self.reject)
        button_row.accepted.connect(self.accept)
        layout.addWidget(button_row)

    def _on_tab_changed(self, _idx: int) -> None:
        # Catalogs are independent but Manage Classification users
        # often jump tabs in the middle of cleanup; reload on focus
        # to catch any background mutation.
        current = self._tabs.currentWidget()
        if hasattr(current, "reload"):
            current.reload()


def _format_created(iso_str: str) -> str:
    if not iso_str:
        return ""
    try:
        from datetime import datetime as _dt
        utc_aware = _dt.fromisoformat(iso_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return iso_str
    return utc_aware.astimezone().strftime("%Y-%m-%d")
