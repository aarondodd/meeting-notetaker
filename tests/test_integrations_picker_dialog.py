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

    def browse_root(self) -> list[PickerNode]:
        return list(self._root)

    def browse_children(self, node: PickerNode) -> list[PickerNode]:
        return list(self._children.get(node.id, []))


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
    )
    try:
        item = dlg._browse_root.child(0)  # noqa: SLF001
        dlg._tree.setCurrentItem(item)  # noqa: SLF001
        dlg._on_accept()  # noqa: SLF001
        sel = dlg.selection()
        assert sel is not None
        assert sel.id == "p1"
        assert sel.title == "Page 1"
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
