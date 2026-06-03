"""IntegrationsPickerDialog tests (#79).

Uses an in-memory FakeBrowser so the tree population path is
exercised offline. Covers root + lazy expansion, favorites toggle,
recents rendering, OK gating on space rows, and the selection
payload returned to the caller.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.ui.integrations_picker_dialog import (  # noqa: E402
    ConfluencePickerBrowser,
    IntegrationsPickerDialog,
    NotionPickerBrowser,
    PickerNode,
)


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


# ---- fake browser ---------------------------------------------------------


class _FakeBrowser:
    """Returns canned children for canned parents."""

    def __init__(self, *, root: list[PickerNode], children: Optional[dict] = None) -> None:
        self._root = root
        self._children = children or {}
        self.create_folder_calls: list[tuple[Optional[PickerNode], str]] = []
        self._next_folder_id = 9000

    def browse_root(self) -> list[PickerNode]:
        return list(self._root)

    def browse_children(self, node: PickerNode) -> list[PickerNode]:
        return list(self._children.get(node.id, []))

    def create_folder(self, parent, name):
        self.create_folder_calls.append((parent, name))
        self._next_folder_id += 1
        return PickerNode(
            id=f"new-{self._next_folder_id}", title=name, has_children=False,
        )


# ---- dialog smoke ----------------------------------------------------------


def test_dialog_loads_browse_root_on_open(qt_app):
    browser = _FakeBrowser(root=[
        PickerNode(id="p1", title="Page 1"),
        PickerNode(id="p2", title="Page 2"),
    ])
    dlg = IntegrationsPickerDialog(
        title="Pick Notion page",
        browser=browser,
        favorites=[], recents=[],
    )
    try:
        browse = dlg._browse_root  # noqa: SLF001
        assert browse.childCount() == 2
        assert browse.child(0).text(0) == "Page 1"
        assert browse.child(1).text(0) == "Page 2"
    finally:
        dlg.deleteLater()


def test_dialog_lazy_expands_children_on_request(qt_app):
    """Expanding a page with has_children=True calls browse_children
    once + replaces the placeholder. Subsequent expand/collapse
    cycles don't re-fetch."""
    browser = _FakeBrowser(
        root=[PickerNode(id="p1", title="Parent")],
        children={"p1": [PickerNode(id="c1", title="Child")]},
    )
    dlg = IntegrationsPickerDialog(
        title="Pick", browser=browser, favorites=[], recents=[],
    )
    try:
        item = dlg._browse_root.child(0)  # noqa: SLF001
        # Pre-expand: one child placeholder.
        assert item.childCount() == 1
        # Expand triggers lazy load.
        item.setExpanded(True)
        # Now has the real child.
        assert item.childCount() == 1
        assert item.child(0).text(0) == "Child"
    finally:
        dlg.deleteLater()


def test_selecting_page_enables_ok_button(qt_app):
    browser = _FakeBrowser(root=[
        PickerNode(id="p1", title="Page 1"),
    ])
    dlg = IntegrationsPickerDialog(
        title="t", browser=browser, favorites=[], recents=[],
    )
    try:
        item = dlg._browse_root.child(0)  # noqa: SLF001
        dlg._tree.setCurrentItem(item)  # noqa: SLF001
        assert dlg._ok_btn.isEnabled()  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_selecting_space_disables_ok_button(qt_app):
    """Confluence spaces (kind='space') aren't valid destinations --
    a space holds pages but isn't itself a parent page."""
    browser = _FakeBrowser(root=[
        PickerNode(id="s1", title="Space One", kind="space"),
    ])
    dlg = IntegrationsPickerDialog(
        title="t", browser=browser, favorites=[], recents=[],
    )
    try:
        item = dlg._browse_root.child(0)  # noqa: SLF001
        dlg._tree.setCurrentItem(item)  # noqa: SLF001
        assert not dlg._ok_btn.isEnabled()  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_star_button_adds_then_removes_from_favorites(qt_app):
    browser = _FakeBrowser(root=[
        PickerNode(id="p1", title="Page 1"),
    ])
    dlg = IntegrationsPickerDialog(
        title="t", browser=browser, favorites=[], recents=[],
    )
    try:
        item = dlg._browse_root.child(0)  # noqa: SLF001
        dlg._tree.setCurrentItem(item)  # noqa: SLF001
        # Star adds.
        dlg._on_star_clicked()  # noqa: SLF001
        favs = dlg.updated_favorites()
        assert len(favs) == 1
        assert favs[0]["id"] == "p1"
        assert favs[0]["title"] == "Page 1"
        # Unstar removes.
        dlg._on_star_clicked()  # noqa: SLF001
        assert dlg.updated_favorites() == []
    finally:
        dlg.deleteLater()


def test_favorites_group_renders_passed_in_entries(qt_app):
    browser = _FakeBrowser(root=[])
    dlg = IntegrationsPickerDialog(
        title="t",
        browser=browser,
        favorites=[
            {"id": "f1", "title": "Pinned A"},
            {"id": "f2", "title": "Pinned B"},
        ],
        recents=[],
    )
    try:
        fav = dlg._fav_root  # noqa: SLF001
        assert fav.childCount() == 2
        assert {fav.child(i).text(0) for i in range(2)} == {"Pinned A", "Pinned B"}
    finally:
        dlg.deleteLater()


def test_recents_group_renders_passed_in_entries(qt_app):
    browser = _FakeBrowser(root=[])
    dlg = IntegrationsPickerDialog(
        title="t",
        browser=browser,
        favorites=[],
        recents=[
            {"id": "r1", "title": "Recent A", "used_at": "2026-06-02T10:00:00"},
        ],
    )
    try:
        rec = dlg._rec_root  # noqa: SLF001
        assert rec.childCount() == 1
        assert rec.child(0).text(0) == "Recent A"
    finally:
        dlg.deleteLater()


def test_accept_records_selection_payload(qt_app):
    browser = _FakeBrowser(root=[
        PickerNode(id="p1", title="Page 1", extra={"space_id": "100"}),
    ])
    dlg = IntegrationsPickerDialog(
        title="t", browser=browser, favorites=[], recents=[],
        default_page_title="default title",
    )
    try:
        item = dlg._browse_root.child(0)  # noqa: SLF001
        dlg._tree.setCurrentItem(item)  # noqa: SLF001
        dlg._on_accept()  # noqa: SLF001
        sel = dlg.selection()
        assert sel is not None
        assert sel.id == "p1"
        assert sel.title == "Page 1"
        assert sel.page_title == "default title"
        assert sel.extra == {"space_id": "100"}
    finally:
        dlg.deleteLater()


def test_root_browse_failure_surfaces_error_row(qt_app):
    class BrokenBrowser:
        def browse_root(self):
            raise RuntimeError("nope")

    dlg = IntegrationsPickerDialog(
        title="t", browser=BrokenBrowser(), favorites=[], recents=[],
    )
    try:
        rows = [
            dlg._browse_root.child(i).text(0)  # noqa: SLF001
            for i in range(dlg._browse_root.childCount())  # noqa: SLF001
        ]
        assert any("Could not load" in row for row in rows)
    finally:
        dlg.deleteLater()


# ---- adapter shape --------------------------------------------------------


def test_notion_browser_adapts_client_pages_to_picker_nodes():
    """Smoke check that NotionPickerBrowser hands back PickerNodes
    with the same id/title/has_children mapping."""
    from meeting_notetaker.integrations.notion_api import NotionPageRef

    class _StubClient:
        def list_accessible_pages(self):
            return [NotionPageRef(id="x", title="Hello", has_children=False)]

        def list_child_pages(self, parent_id):
            return [NotionPageRef(id=f"{parent_id}.c1", title="Child", has_children=True)]

    b = NotionPickerBrowser(_StubClient())
    root = b.browse_root()
    assert root[0].id == "x"
    assert root[0].has_children is False
    children = b.browse_children(PickerNode(id="x", title="Hello"))
    assert children[0].id == "x.c1"


def test_browse_root_entries_are_alphabetized(qt_app):
    """API responses may return spaces or pages in arbitrary order
    (creation date, page id, etc.). The picker shows them sorted by
    title so users find their destination by scanning, not searching."""
    browser = _FakeBrowser(root=[
        PickerNode(id="z", title="Zebra"),
        PickerNode(id="a", title="apple"),
        PickerNode(id="m", title="Mango"),
    ])
    dlg = IntegrationsPickerDialog(
        title="t", browser=browser, favorites=[], recents=[],
    )
    try:
        rows = [
            dlg._browse_root.child(i).text(0)  # noqa: SLF001
            for i in range(dlg._browse_root.childCount())  # noqa: SLF001
        ]
        assert rows == ["apple", "Mango", "Zebra"]
    finally:
        dlg.deleteLater()


def test_browse_children_are_alphabetized(qt_app):
    browser = _FakeBrowser(
        root=[PickerNode(id="p", title="Parent")],
        children={"p": [
            PickerNode(id="c3", title="Zucchini"),
            PickerNode(id="c1", title="Asparagus"),
            PickerNode(id="c2", title="kale"),
        ]},
    )
    dlg = IntegrationsPickerDialog(
        title="t", browser=browser, favorites=[], recents=[],
    )
    try:
        parent_item = dlg._browse_root.child(0)  # noqa: SLF001
        parent_item.setExpanded(True)
        rows = [
            parent_item.child(i).text(0)
            for i in range(parent_item.childCount())
        ]
        assert rows == ["Asparagus", "kale", "Zucchini"]
    finally:
        dlg.deleteLater()


def test_favorites_render_alphabetized(qt_app):
    """Favorites get the same alphabetical treatment as Browse rows.
    Recents stay in recency order (they're date-sorted by definition)."""
    browser = _FakeBrowser(root=[])
    dlg = IntegrationsPickerDialog(
        title="t",
        browser=browser,
        favorites=[
            {"id": "f1", "title": "Zeta"},
            {"id": "f2", "title": "alpha"},
            {"id": "f3", "title": "Mu"},
        ],
        recents=[],
    )
    try:
        rows = [
            dlg._fav_root.child(i).text(0)  # noqa: SLF001
            for i in range(dlg._fav_root.childCount())  # noqa: SLF001
        ]
        assert rows == ["alpha", "Mu", "Zeta"]
    finally:
        dlg.deleteLater()


def test_recents_preserve_input_order(qt_app):
    """Recents must NOT be alphabetized; they're ordered by recency
    so the most-recently-used destination sits at the top."""
    browser = _FakeBrowser(root=[])
    dlg = IntegrationsPickerDialog(
        title="t",
        browser=browser,
        favorites=[],
        recents=[
            {"id": "r1", "title": "Zebra", "used_at": "2026-06-02T15:00:00"},
            {"id": "r2", "title": "Apple", "used_at": "2026-06-02T14:00:00"},
        ],
    )
    try:
        rows = [
            dlg._rec_root.child(i).text(0)  # noqa: SLF001
            for i in range(dlg._rec_root.childCount())  # noqa: SLF001
        ]
        assert rows == ["Zebra", "Apple"]  # input order preserved
    finally:
        dlg.deleteLater()


def test_starring_keeps_favorites_alphabetized(qt_app):
    """Toggling star on a new entry should slot it into the
    alphabetized favorites list, not append to the end."""
    browser = _FakeBrowser(root=[
        PickerNode(id="bbb", title="Banana"),
    ])
    dlg = IntegrationsPickerDialog(
        title="t",
        browser=browser,
        favorites=[
            {"id": "aaa", "title": "Apple"},
            {"id": "ccc", "title": "Cherry"},
        ],
        recents=[],
    )
    try:
        # Star the Banana row.
        banana = dlg._browse_root.child(0)  # noqa: SLF001
        dlg._tree.setCurrentItem(banana)  # noqa: SLF001
        dlg._on_star_clicked()  # noqa: SLF001
        rows = [
            dlg._fav_root.child(i).text(0)  # noqa: SLF001
            for i in range(dlg._fav_root.childCount())  # noqa: SLF001
        ]
        assert rows == ["Apple", "Banana", "Cherry"]
    finally:
        dlg.deleteLater()


# ---- title field + page-title round trip --------------------------------


def test_title_field_defaults_from_constructor(qt_app):
    browser = _FakeBrowser(root=[PickerNode(id="p", title="P")])
    dlg = IntegrationsPickerDialog(
        title="t",
        browser=browser,
        favorites=[], recents=[],
        default_page_title="2026-06-03 14:30 - Weekly Sync",
    )
    try:
        assert dlg._title_edit.text() == "2026-06-03 14:30 - Weekly Sync"  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_accept_carries_user_edited_page_title(qt_app):
    browser = _FakeBrowser(root=[PickerNode(id="p", title="P")])
    dlg = IntegrationsPickerDialog(
        title="t", browser=browser, favorites=[], recents=[],
        default_page_title="default title",
    )
    try:
        dlg._title_edit.setText("user edited title")  # noqa: SLF001
        dlg._tree.setCurrentItem(dlg._browse_root.child(0))  # noqa: SLF001
        dlg._on_accept()  # noqa: SLF001
        sel = dlg.selection()
        assert sel is not None
        assert sel.page_title == "user edited title"
    finally:
        dlg.deleteLater()


def test_accept_blocked_when_title_is_empty(qt_app):
    """A blank title would create a page with no name; block accept."""
    from unittest.mock import patch

    browser = _FakeBrowser(root=[PickerNode(id="p", title="P")])
    dlg = IntegrationsPickerDialog(
        title="t", browser=browser, favorites=[], recents=[],
        default_page_title="",
    )
    try:
        dlg._tree.setCurrentItem(dlg._browse_root.child(0))  # noqa: SLF001
        # Title is empty -- accept should refuse + dialog stays open.
        with patch(
            "meeting_notetaker.ui.integrations_picker_dialog.QMessageBox.information"
        ):
            dlg._on_accept()  # noqa: SLF001
        assert dlg.selection() is None
    finally:
        dlg.deleteLater()


# ---- create-folder flow -------------------------------------------------


def test_create_folder_button_disabled_when_nothing_selected(qt_app):
    browser = _FakeBrowser(root=[])
    dlg = IntegrationsPickerDialog(
        title="t", browser=browser, favorites=[], recents=[],
    )
    try:
        assert not dlg._create_folder_btn.isEnabled()  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_create_folder_button_enabled_for_space_selection(qt_app):
    """Confluence spaces aren't valid destinations but ARE valid
    parents for new folders. Create-folder must enable when a space
    is selected even though OK is disabled."""
    browser = _FakeBrowser(root=[
        PickerNode(id="s", title="Eng Space", kind="space"),
    ])
    dlg = IntegrationsPickerDialog(
        title="t", browser=browser, favorites=[], recents=[],
        default_page_title="t",
    )
    try:
        dlg._tree.setCurrentItem(dlg._browse_root.child(0))  # noqa: SLF001
        assert dlg._create_folder_btn.isEnabled()  # noqa: SLF001
        assert not dlg._ok_btn.isEnabled()  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_create_folder_calls_browser_with_parent_node_and_name(qt_app, monkeypatch):
    """Clicking Create Folder + entering a name routes through the
    browser's create_folder + slots the new node under the selected
    parent + selects it."""
    from PyQt6.QtWidgets import QDialog as _QDialog

    browser = _FakeBrowser(
        root=[PickerNode(id="parent", title="Parent Page")],
        children={"parent": []},
    )
    dlg = IntegrationsPickerDialog(
        title="t", browser=browser, favorites=[], recents=[],
        default_page_title="t",
    )
    try:
        parent_item = dlg._browse_root.child(0)  # noqa: SLF001
        dlg._tree.setCurrentItem(parent_item)  # noqa: SLF001
        # Stub the sub-dialog so the test doesn't need user input.
        from meeting_notetaker.ui import integrations_picker_dialog as ipd

        class _StubSub:
            def __init__(self, *args, **kwargs):
                self._name = "My New Folder"
            def exec(self):
                return _QDialog.DialogCode.Accepted
            def entered_name(self):
                return self._name
        monkeypatch.setattr(ipd, "_CreateFolderDialog", _StubSub)
        dlg._on_create_folder_clicked()  # noqa: SLF001
        # Browser saw the create call with the right args.
        assert len(browser.create_folder_calls) == 1
        parent_arg, name_arg = browser.create_folder_calls[0]
        assert parent_arg.id == "parent"
        assert name_arg == "My New Folder"
        # New folder appears under the parent + is the current selection.
        current = dlg._tree.currentItem()  # noqa: SLF001
        assert current is not None
        new_node = current.data(0, Qt.ItemDataRole.UserRole)
        assert new_node.title == "My New Folder"
    finally:
        dlg.deleteLater()


def test_create_folder_cancelled_does_not_call_browser(qt_app, monkeypatch):
    from PyQt6.QtWidgets import QDialog as _QDialog
    browser = _FakeBrowser(root=[PickerNode(id="p", title="P")])
    dlg = IntegrationsPickerDialog(
        title="t", browser=browser, favorites=[], recents=[],
        default_page_title="t",
    )
    try:
        dlg._tree.setCurrentItem(dlg._browse_root.child(0))  # noqa: SLF001
        from meeting_notetaker.ui import integrations_picker_dialog as ipd

        class _Cancel:
            def __init__(self, *args, **kwargs):
                pass
            def exec(self):
                return _QDialog.DialogCode.Rejected
            def entered_name(self):
                return ""
        monkeypatch.setattr(ipd, "_CreateFolderDialog", _Cancel)
        dlg._on_create_folder_clicked()  # noqa: SLF001
        assert browser.create_folder_calls == []
    finally:
        dlg.deleteLater()


# ---- sub-dialog: use-series-name affordance -----------------------------


def test_create_folder_dialog_use_series_btn_fills_name(qt_app):
    from meeting_notetaker.ui.integrations_picker_dialog import _CreateFolderDialog

    sub = _CreateFolderDialog(
        series_name="Weekly Engineering Sync",
        parent_title="Meetings",
    )
    try:
        assert sub._series_btn.isEnabled()  # noqa: SLF001
        sub._on_use_series()  # noqa: SLF001
        assert sub.entered_name() == "Weekly Engineering Sync"
    finally:
        sub.deleteLater()


def test_create_folder_dialog_use_series_btn_disabled_when_no_series(qt_app):
    from meeting_notetaker.ui.integrations_picker_dialog import _CreateFolderDialog

    sub = _CreateFolderDialog(series_name="", parent_title="Meetings")
    try:
        assert not sub._series_btn.isEnabled()  # noqa: SLF001
    finally:
        sub.deleteLater()


# ---- adapter create_folder ----------------------------------------------


def test_notion_browser_create_folder_routes_through_client():
    """NotionPickerBrowser.create_folder creates a child page via
    the underlying NotionClient. The returned PickerNode carries
    the new page id."""

    class _Client:
        def __init__(self):
            self.create_calls = []
        def create_page(self, **kwargs):
            self.create_calls.append(kwargs)
            return {"id": "new-page-id", "url": "https://notion.so/new-page-id"}

    from meeting_notetaker.ui.integrations_picker_dialog import NotionPickerBrowser

    client = _Client()
    browser = NotionPickerBrowser(client)
    parent = PickerNode(id="parent-page", title="Meetings")
    result = browser.create_folder(parent, "Weekly Sync")
    assert result.id == "new-page-id"
    assert result.title == "Weekly Sync"
    assert client.create_calls[0]["parent_id"] == "parent-page"
    assert client.create_calls[0]["title"] == "Weekly Sync"
    assert client.create_calls[0]["children"] == []


def test_confluence_browser_create_folder_in_space_omits_parent():
    """When the parent is a space, the folder is a root-level page;
    the client is called with parent_id='' so confluence_api.create_page
    drops parentId from the body."""

    class _Client:
        def __init__(self):
            self.calls = []
        def create_page(self, **kwargs):
            self.calls.append(kwargs)
            return {"id": "9999"}

    from meeting_notetaker.ui.integrations_picker_dialog import ConfluencePickerBrowser

    client = _Client()
    browser = ConfluencePickerBrowser(client)
    space = PickerNode(
        id="100", title="Engineering", kind="space",
        extra={"space_id": "100"},
    )
    result = browser.create_folder(space, "Project Notes")
    assert result.id == "9999"
    assert result.title == "Project Notes"
    assert client.calls[0]["space_id"] == "100"
    assert client.calls[0]["parent_id"] == ""
    assert client.calls[0]["storage_xml"] == "<p></p>"


def test_confluence_browser_create_folder_under_page_passes_parent_id():
    class _Client:
        def __init__(self):
            self.calls = []
        def create_page(self, **kwargs):
            self.calls.append(kwargs)
            return {"id": "9999"}

    from meeting_notetaker.ui.integrations_picker_dialog import ConfluencePickerBrowser

    client = _Client()
    browser = ConfluencePickerBrowser(client)
    page = PickerNode(
        id="123", title="Parent Page", kind="page",
        extra={"space_id": "100"},
    )
    browser.create_folder(page, "Child Folder")
    assert client.calls[0]["space_id"] == "100"
    assert client.calls[0]["parent_id"] == "123"


def test_confluence_api_create_page_drops_empty_parent_id():
    """Smoke test for the confluence_api.create_page change: when
    parent_id is empty, parentId must be omitted from the request
    body so the v2 API treats it as a root-level page in the space."""
    from unittest.mock import MagicMock
    from meeting_notetaker.integrations.confluence_api import ConfluenceClient

    sess = MagicMock()
    sess.request.return_value.status_code = 200
    sess.request.return_value.content = b"{}"
    sess.request.return_value.json.return_value = {"id": "1"}
    client = ConfluenceClient(
        "https://x/wiki", "u@x.com", "t", session=sess,
    )
    client.create_page(
        parent_id="", space_id="100", title="t", storage_xml="<p></p>",
    )
    body = sess.request.call_args.kwargs["json"]
    assert "parentId" not in body
    # Now confirm that when parent_id is non-empty the field IS present.
    sess.reset_mock()
    sess.request.return_value.status_code = 200
    sess.request.return_value.content = b"{}"
    sess.request.return_value.json.return_value = {"id": "1"}
    client.create_page(
        parent_id="9001", space_id="100", title="t", storage_xml="<p></p>",
    )
    body = sess.request.call_args.kwargs["json"]
    assert body["parentId"] == "9001"


def test_confluence_browser_adapts_spaces_then_pages():
    from meeting_notetaker.integrations.confluence_api import ConfluenceNodeRef

    class _StubClient:
        def list_spaces(self):
            return [ConfluenceNodeRef(
                id="100", title="Eng", kind="space", space_id="100",
            )]

        def list_root_pages(self, space_id):
            return [ConfluenceNodeRef(
                id="9001", title="Welcome", kind="page", space_id=space_id,
            )]

        def list_child_pages(self, page_id, *, space_id=None):
            return [ConfluenceNodeRef(
                id="9002", title="Child", kind="page", space_id=space_id,
            )]

    b = ConfluencePickerBrowser(_StubClient())
    root = b.browse_root()
    assert root[0].kind == "space"
    assert root[0].extra["space_id"] == "100"
    # Expanding the space returns root pages.
    space_node = PickerNode(
        id="100", title="Eng", kind="space", extra={"space_id": "100"},
    )
    pages = b.browse_children(space_node)
    assert pages[0].kind == "page"
    # Expanding a page returns children.
    page_node = PickerNode(
        id="9001", title="Welcome", kind="page", extra={"space_id": "100"},
    )
    children = b.browse_children(page_node)
    assert children[0].id == "9002"
