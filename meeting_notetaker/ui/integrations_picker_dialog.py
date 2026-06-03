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
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
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

    ``id`` + ``title`` identify the destination parent. ``page_title``
    is the user-edited (or defaulted) title the export will use for
    the new page. ``include_attachments`` flags whether the export
    should upload the session's files to the created page (only
    meaningful when the picker was opened with attachments present).
    ``extra`` carries adapter-specific metadata (Confluence:
    ``space_id``).
    """
    def __init__(
        self,
        id: str,
        title: str,
        *,
        page_title: str = "",
        include_attachments: bool = False,
        extra: Optional[dict] = None,
    ) -> None:
        self.id = id
        self.title = title
        self.page_title = page_title or title
        self.include_attachments = include_attachments
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

    def create_folder(
        self, parent: Optional[PickerNode], name: str,
    ) -> PickerNode:
        """Create an empty page under ``parent`` and return its
        PickerNode (for the picker to select after the call).

        Folders in Notion are just regular pages with empty children.
        Parent must be an existing accessible page -- the integration
        can't create top-level workspace pages.
        """
        if parent is None:
            raise ValueError(
                "Notion folders need a parent page; pick a destination first."
            )
        payload = self._client.create_page(
            parent_id=parent.id, title=name, children=[],
        )
        return PickerNode(
            id=payload.get("id", ""),
            title=name,
            has_children=False,
        )


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

    def create_folder(
        self, parent: Optional[PickerNode], name: str,
    ) -> PickerNode:
        """Create an empty page under ``parent`` (which may be a space
        or a page). Returns the new page's PickerNode."""
        if parent is None:
            raise ValueError(
                "Confluence folders need a parent space or page."
            )
        space_id = parent.extra.get("space_id") or parent.id
        # When the parent is a space, the v2 create-page endpoint
        # accepts parentId omitted (root-level in the space). We pass
        # the space's id as a soft parent so it still anchors visually,
        # but the API only requires spaceId.
        if parent.kind == "space":
            parent_id = ""  # root of space
        else:
            parent_id = parent.id
        payload = self._client.create_page(
            parent_id=parent_id,
            space_id=space_id,
            title=name,
            storage_xml="<p></p>",
        )
        return PickerNode(
            id=str(payload.get("id", "")),
            title=name,
            has_children=False,
            kind="page",
            extra={"space_id": space_id},
        )


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
        default_page_title: str = "",
        series_name: str = "",
        attachment_count: int = 0,
        parent: Optional[QWidget] = None,
        load_root_immediately: bool = True,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(580, 600)
        self._browser = browser
        self._favorites = list(favorites or [])
        self._recents = list(recents or [])
        self._series_name = (series_name or "").strip()
        self._attachment_count = max(0, int(attachment_count))
        self._selection: Optional[PickerSelection] = None

        layout = QVBoxLayout(self)

        header = QLabel(
            "Pick the page that will be the new export's parent and "
            "set the title. Star a row to add it to Favorites; the "
            "dialog also remembers the last few destinations you used.",
            self,
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        # Page title row -- the user can edit before saving.
        title_form = QFormLayout()
        self._title_edit = QLineEdit(self)
        self._title_edit.setText(default_page_title or "")
        self._title_edit.setPlaceholderText(
            "YYYY-MM-DD HH:MM - Session Title"
        )
        title_form.addRow("Page title:", self._title_edit)
        layout.addLayout(title_form)

        # Optional Include-Attachments checkbox. Only rendered when
        # the session has at least one attachment; clean dialog
        # surface for sessions without files. When checked, the
        # export uploads each attachment to the created page (Notion:
        # file blocks; Confluence: page attachments with a linked
        # Attachments section).
        self._include_attachments_cb: Optional[QCheckBox] = None
        if self._attachment_count > 0:
            noun = "attachment" if self._attachment_count == 1 else "attachments"
            self._include_attachments_cb = QCheckBox(
                f"Include {self._attachment_count} {noun} from this session",
                self,
            )
            self._include_attachments_cb.setToolTip(
                "Upload the session's attachment files to the created "
                "page so the saved record is self-contained."
            )
            layout.addWidget(self._include_attachments_cb)

        self._tree = QTreeWidget(self)
        self._tree.setHeaderHidden(True)
        self._tree.itemExpanded.connect(self._on_item_expanded)
        self._tree.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self._tree, 1)

        # Star + create-folder row -- pin/unpin the current selection
        # and add a new container page under it without leaving the
        # dialog.
        action_row = QHBoxLayout()
        self._star_btn = QPushButton("Star this selection", self)
        self._star_btn.setEnabled(False)
        self._star_btn.clicked.connect(self._on_star_clicked)
        action_row.addWidget(self._star_btn)
        self._create_folder_btn = QPushButton("Create folder...", self)
        self._create_folder_btn.setToolTip(
            "Create a new container page under the currently selected "
            "destination. The new folder becomes the picker's "
            "selection so you can save the session into it."
        )
        self._create_folder_btn.setEnabled(False)
        self._create_folder_btn.clicked.connect(self._on_create_folder_clicked)
        action_row.addWidget(self._create_folder_btn)
        action_row.addStretch(1)
        self._status_label = QLabel("", self)
        action_row.addWidget(self._status_label)
        layout.addLayout(action_row)

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
        # Create-folder can target any selected node (page OR space)
        # because both can host child pages. Disable only when nothing
        # is selected.
        self._create_folder_btn.setEnabled(node is not None)
        if not has_pickable:
            # Clear the starred-state label when selection moves to a
            # space or empties out.
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

    # ---- create folder --------------------------------------------------

    def _on_create_folder_clicked(self) -> None:
        """Open the name-input sub-dialog. On confirm, call the
        browser to create a new container page under the current
        selection + re-select the new node in the tree."""
        parent_node = self._current_node()
        if parent_node is None:
            return
        sub = _CreateFolderDialog(
            series_name=self._series_name,
            parent_title=parent_node.title,
            parent=self,
        )
        if sub.exec() != QDialog.DialogCode.Accepted:
            return
        name = sub.entered_name()
        if not name:
            return
        try:
            new_node = self._with_wait(
                lambda: self._browser.create_folder(parent_node, name),
            )
        except Exception as exc:
            QMessageBox.warning(
                self, "Create folder",
                f"Could not create the folder page:\n\n{exc}",
            )
            return
        self._insert_new_folder(parent_node, new_node)

    def _insert_new_folder(
        self, parent_node: PickerNode, new_node: PickerNode,
    ) -> None:
        """Slot the freshly-created folder under its parent in the
        tree and select it so the user can immediately save into it.

        For an already-expanded parent we append + re-sort the
        siblings; for a lazy / unexpanded parent we leave it for the
        next expand-fetch + select via a refresh."""
        parent_item = self._tree.currentItem()
        if parent_item is None:
            return
        # Drop any "(no children)" placeholder.
        for i in range(parent_item.childCount() - 1, -1, -1):
            stub = parent_item.child(i)
            if stub.data(0, Qt.ItemDataRole.UserRole) is None:
                parent_item.removeChild(stub)
        new_item = self._append_lazy_node(parent_item, new_node)
        # Re-sort siblings alphabetically by title for visual stability.
        children = [
            parent_item.child(i)
            for i in range(parent_item.childCount())
        ]
        children.sort(key=lambda it: (it.text(0) or "").casefold())
        # QTreeWidget has no in-place reorder; rebuild.
        while parent_item.childCount():
            parent_item.takeChild(0)
        for it in children:
            parent_item.addChild(it)
        parent_item.setExpanded(True)
        self._tree.setCurrentItem(new_item)
        del parent_node  # used only for read; selection runs on new_item

    # ---- accept ---------------------------------------------------------

    def _on_accept(self) -> None:
        node = self._current_node()
        if node is None or node.kind == "space":
            QMessageBox.information(
                self, self.windowTitle(),
                "Pick a page (not a space) as the destination parent.",
            )
            return
        page_title = self._title_edit.text().strip()
        if not page_title:
            QMessageBox.information(
                self, self.windowTitle(),
                "Enter a page title before saving.",
            )
            self._title_edit.setFocus()
            return
        include_attachments = bool(
            self._include_attachments_cb
            and self._include_attachments_cb.isChecked()
        )
        self._selection = PickerSelection(
            id=node.id, title=node.title,
            page_title=page_title,
            include_attachments=include_attachments,
            extra=dict(node.extra),
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


# ---- create-folder sub-dialog --------------------------------------------


class _CreateFolderDialog(QDialog):
    """Small modal that prompts for a new folder name.

    The ``Use series name`` button is enabled only when the session
    has a series assigned; clicking it fills the text field with the
    series so the user can name a folder after the recurring meeting
    series in one click.
    """

    def __init__(
        self,
        *,
        series_name: str,
        parent_title: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Create folder")
        self.setModal(True)
        self.resize(420, 160)
        self._series_name = series_name or ""

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f"Create a new container page under \"{parent_title}\".",
            self,
        ))

        form = QFormLayout()
        self._name_edit = QLineEdit(self)
        self._name_edit.setPlaceholderText("Folder name")
        form.addRow("Name:", self._name_edit)
        layout.addLayout(form)

        row = QHBoxLayout()
        self._series_btn = QPushButton("Use series name", self)
        self._series_btn.setEnabled(bool(self._series_name))
        self._series_btn.setToolTip(
            self._series_name or "This session has no series assigned."
        )
        self._series_btn.clicked.connect(self._on_use_series)
        row.addWidget(self._series_btn)
        row.addStretch(1)
        layout.addLayout(row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def entered_name(self) -> str:
        return self._name_edit.text().strip()

    def _on_use_series(self) -> None:
        if self._series_name:
            self._name_edit.setText(self._series_name)

    def _on_accept(self) -> None:
        if not self._name_edit.text().strip():
            QMessageBox.information(
                self, "Create folder", "Enter a folder name first.",
            )
            self._name_edit.setFocus()
            return
        self.accept()
