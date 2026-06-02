"""Destination picker for the Notion + Confluence export feature (#79).

A QDialog with three sections stacked top to bottom:

  Favorites  -- user-pinned destinations, persistent in the config
                until the user un-stars them.
  Recents    -- last N=5 distinct destinations the user exported to.
  Browse     -- lazy-loaded tree from the target's API.

The dialog is target-agnostic; the caller supplies a ``browser``
object (Protocol below) that adapts a specific API. Two adapters
live in this module: ``NotionPickerBrowser`` and
``ConfluencePickerBrowser``. Both wrap the relevant REST client
and translate node refs into the picker's uniform ``PickerNode``
shape.

Network calls run synchronously with a wait cursor; lazy-load
keeps per-expansion latency bounded to one API call. A QThread
upgrade is straightforward later if the wait cursor proves too
intrusive on slow links.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QCursor
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)


@dataclass
class PickerNode:
    """One row in the picker.

    ``kind`` distinguishes API-side semantics (Confluence: "space" vs
    "page"; Notion always "page"). ``extra`` carries any adapter-
    specific metadata the caller may need to round-trip (e.g.
    Confluence's space_id).
    """
    id: str
    title: str
    has_children: bool = True
    kind: str = "page"
    extra: dict = field(default_factory=dict)


class PickerSelection:
    """The result of an accepted picker dialog.

    ``id`` + ``title`` go straight into the favorites / recents
    store and the export request. ``extra`` carries adapter-specific
    metadata (Confluence: ``space_id``).
    """
    def __init__(self, id: str, title: str, *, extra: Optional[dict] = None) -> None:
        self.id = id
        self.title = title
        self.extra = extra or {}


# ---- adapters --------------------------------------------------------------


class NotionPickerBrowser:
    """Adapts NotionClient to the picker's browse interface."""

    def __init__(self, client) -> None:
        self._client = client

    def browse_root(self) -> list[PickerNode]:
        return [
            PickerNode(id=p.id, title=p.title, has_children=p.has_children)
            for p in self._client.list_accessible_pages()
        ]

    def browse_children(self, node: PickerNode) -> list[PickerNode]:
        return [
            PickerNode(id=p.id, title=p.title, has_children=p.has_children)
            for p in self._client.list_child_pages(node.id)
        ]


class ConfluencePickerBrowser:
    """Adapts ConfluenceClient to the picker's browse interface."""

    def __init__(self, client) -> None:
        self._client = client

    def browse_root(self) -> list[PickerNode]:
        return [
            PickerNode(
                id=s.id, title=s.title, has_children=True, kind="space",
                extra={"space_id": s.space_id},
            )
            for s in self._client.list_spaces()
        ]

    def browse_children(self, node: PickerNode) -> list[PickerNode]:
        space_id = node.extra.get("space_id") or node.id
        if node.kind == "space":
            refs = self._client.list_root_pages(node.id)
        else:
            refs = self._client.list_child_pages(node.id, space_id=space_id)
        return [
            PickerNode(
                id=r.id, title=r.title, has_children=r.has_children,
                kind="page",
                extra={"space_id": r.space_id or space_id},
            )
            for r in refs
        ]


# ---- the dialog -----------------------------------------------------------


# Sentinel for "no children loaded yet" -- used in the QTreeWidget item's
# Qt.UserRole + 1 slot so the expand handler can distinguish a lazy stub
# from a leaf.
_LAZY_STUB = "__lazy__"


class IntegrationsPickerDialog(QDialog):
    """Pick a destination page from Favorites / Recents / Browse."""

    def __init__(
        self,
        *,
        title: str,
        browser,
        favorites: list[dict],
        recents: list[dict],
        parent: Optional[QWidget] = None,
        load_root_immediately: bool = True,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(560, 540)
        self._browser = browser
        self._favorites = list(favorites or [])
        self._recents = list(recents or [])
        self._selection: Optional[PickerSelection] = None

        layout = QVBoxLayout(self)

        header = QLabel(
            "Pick the page that will be the new export's parent. "
            "Star a row to add it to Favorites; the dialog also "
            "remembers the last few destinations you used.",
            self,
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        self._tree = QTreeWidget(self)
        self._tree.setHeaderHidden(True)
        self._tree.itemExpanded.connect(self._on_item_expanded)
        self._tree.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self._tree, 1)

        # Star toggle row -- pin/unpin the current selection.
        star_row = QHBoxLayout()
        self._star_btn = QPushButton("Star this selection", self)
        self._star_btn.setEnabled(False)
        self._star_btn.clicked.connect(self._on_star_clicked)
        star_row.addWidget(self._star_btn)
        star_row.addStretch(1)
        self._status_label = QLabel("", self)
        star_row.addWidget(self._status_label)
        layout.addLayout(star_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        self._ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_btn.setEnabled(False)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._populate_groups()
        if load_root_immediately:
            self._populate_browse_root()

    # ---- result ---------------------------------------------------------

    def selection(self) -> Optional[PickerSelection]:
        return self._selection

    def updated_favorites(self) -> list[dict]:
        return list(self._favorites)

    # ---- tree population ------------------------------------------------

    def _populate_groups(self) -> None:
        """Build the Favorites + Recents + Browse group items.

        Each group is a QTreeWidgetItem header (non-selectable). Real
        rows attach as its children. Empty Favorites + Recents groups
        are still rendered (collapsed) so the user can see the
        affordance exists.
        """
        # Favorites alphabetized so a glance always finds the right
        # pinned entry; Recents intentionally stays in recency order
        # (that's the entire point of the section).
        self._fav_root = self._make_group_item("Favorites")
        for entry in _sorted_entries(self._favorites):
            self._append_pinned_row(self._fav_root, entry, source="favorite")

        self._rec_root = self._make_group_item("Recents")
        for entry in self._recents:
            self._append_pinned_row(self._rec_root, entry, source="recent")

        self._browse_root = self._make_group_item("Browse")
        # Placeholder child so the chevron renders before lazy load.
        placeholder = QTreeWidgetItem(self._browse_root)
        placeholder.setText(0, "Loading...")
        placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
        placeholder.setData(0, Qt.ItemDataRole.UserRole + 1, _LAZY_STUB)

        # Open Favorites + Recents groups so their contents are visible.
        self._fav_root.setExpanded(True)
        self._rec_root.setExpanded(True)

    def _make_group_item(self, label: str) -> QTreeWidgetItem:
        item = QTreeWidgetItem(self._tree)
        item.setText(0, label)
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        # Group rows are organizational; not selectable as a destination.
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        return item

    def _append_pinned_row(
        self, parent: QTreeWidgetItem, entry: dict, source: str,
    ) -> QTreeWidgetItem:
        node = PickerNode(
            id=entry.get("id", ""),
            title=entry.get("title", "(untitled)"),
            kind=entry.get("kind", "page"),
            has_children=False,
            extra=entry.get("extra") or {},
        )
        item = QTreeWidgetItem(parent)
        item.setText(0, node.title)
        item.setData(0, Qt.ItemDataRole.UserRole, node)
        item.setData(0, Qt.ItemDataRole.UserRole + 1, source)
        return item

    def _populate_browse_root(self) -> None:
        """Run the root browse call + replace the placeholder."""
        # Drop the placeholder.
        while self._browse_root.childCount():
            self._browse_root.takeChild(0)
        try:
            nodes = self._with_wait(self._browser.browse_root)
        except Exception as exc:
            err = QTreeWidgetItem(self._browse_root)
            err.setText(0, f"Could not load: {exc}")
            err.setFlags(Qt.ItemFlag.NoItemFlags)
            return
        if not nodes:
            empty = QTreeWidgetItem(self._browse_root)
            empty.setText(0, "(none)")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            return
        for node in _sorted_nodes(nodes):
            self._append_lazy_node(self._browse_root, node)
        self._browse_root.setExpanded(True)

    def _append_lazy_node(
        self, parent: QTreeWidgetItem, node: PickerNode,
    ) -> QTreeWidgetItem:
        item = QTreeWidgetItem(parent)
        item.setText(0, node.title)
        item.setData(0, Qt.ItemDataRole.UserRole, node)
        if node.has_children:
            stub = QTreeWidgetItem(item)
            stub.setText(0, "Loading...")
            stub.setFlags(Qt.ItemFlag.NoItemFlags)
            stub.setData(0, Qt.ItemDataRole.UserRole + 1, _LAZY_STUB)
        return item

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        # Group rows (Favorites / Recents / Browse) don't lazy-load.
        if item.parent() is None and item.data(0, Qt.ItemDataRole.UserRole) is None:
            return
        # If the first child is the lazy stub, resolve it now.
        if item.childCount() != 1:
            return
        first = item.child(0)
        if first.data(0, Qt.ItemDataRole.UserRole + 1) != _LAZY_STUB:
            return
        node: Optional[PickerNode] = item.data(0, Qt.ItemDataRole.UserRole)
        if node is None:
            return
        item.removeChild(first)
        try:
            children = self._with_wait(
                lambda: self._browser.browse_children(node),
            )
        except Exception as exc:
            err = QTreeWidgetItem(item)
            err.setText(0, f"Could not load: {exc}")
            err.setFlags(Qt.ItemFlag.NoItemFlags)
            return
        if not children:
            empty = QTreeWidgetItem(item)
            empty.setText(0, "(no children)")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            return
        for child in _sorted_nodes(children):
            self._append_lazy_node(item, child)

    # ---- selection + star ------------------------------------------------

    def _current_node(self) -> Optional[PickerNode]:
        item = self._tree.currentItem()
        if item is None:
            return None
        return item.data(0, Qt.ItemDataRole.UserRole)

    def _on_selection_changed(self) -> None:
        node = self._current_node()
        has_pickable = node is not None
        # Spaces aren't valid destinations -- only pages can be parents.
        if node is not None and node.kind == "space":
            has_pickable = False
        self._ok_btn.setEnabled(has_pickable)
        self._star_btn.setEnabled(has_pickable)
        if not has_pickable:
            self._status_label.setText("")
            return
        # Mirror "already starred" state into the button label.
        already = self._is_favorite(node)
        self._star_btn.setText("Remove from Favorites" if already else "Star this selection")
        self._status_label.setText("Starred" if already else "")

    def _is_favorite(self, node: PickerNode) -> bool:
        return any(f.get("id") == node.id for f in self._favorites)

    def _on_star_clicked(self) -> None:
        node = self._current_node()
        if node is None:
            return
        if self._is_favorite(node):
            self._favorites = [f for f in self._favorites if f.get("id") != node.id]
        else:
            self._favorites.append({
                "id": node.id,
                "title": node.title,
                "kind": node.kind,
                "extra": node.extra,
            })
        # Rebuild the Favorites group rows in place, alphabetized.
        while self._fav_root.childCount():
            self._fav_root.takeChild(0)
        for entry in _sorted_entries(self._favorites):
            self._append_pinned_row(self._fav_root, entry, source="favorite")
        self._fav_root.setExpanded(True)
        # Refresh button label.
        self._on_selection_changed()

    # ---- accept ---------------------------------------------------------

    def _on_accept(self) -> None:
        node = self._current_node()
        if node is None or node.kind == "space":
            QMessageBox.information(
                self, self.windowTitle(),
                "Pick a page (not a space) as the destination parent.",
            )
            return
        self._selection = PickerSelection(
            id=node.id, title=node.title, extra=dict(node.extra),
        )
        self.accept()

    # ---- helpers --------------------------------------------------------

    def _with_wait(self, fn: Callable[[], Any]) -> Any:
        """Run ``fn`` with a wait cursor visible."""
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        try:
            return fn()
        finally:
            QApplication.restoreOverrideCursor()


# ---- module-level sort helpers -------------------------------------------


def _sorted_nodes(nodes: list[PickerNode]) -> list[PickerNode]:
    """Alphabetize a PickerNode list by title (case-insensitive)."""
    return sorted(nodes, key=lambda n: (n.title or "").casefold())


def _sorted_entries(entries: list[dict]) -> list[dict]:
    """Alphabetize a favorites / recents dict list by title."""
    return sorted(entries, key=lambda e: (e.get("title") or "").casefold())
