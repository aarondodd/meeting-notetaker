"""Tests for the OneNote COM wrapper (#100).

The COM dispatch is mocked via the ``dispatch`` injection point so
the suite runs on Linux without pywin32 or OneNote installed.
"""
from __future__ import annotations

import pytest

from meeting_notetaker.integrations import onenote_com as oc


class _FakeApp:
    """In-memory stand-in for the OneNote.Application COM object."""

    def __init__(self, *, hierarchy_xml: str = "", create_returns: str = "",
                 update_raises: Exception = None, navigate_raises: Exception = None,
                 hyperlink: str = ""):
        self._hierarchy = hierarchy_xml
        self._create = create_returns
        self._update_raises = update_raises
        self._navigate_raises = navigate_raises
        self._hyperlink = hyperlink
        self.calls: list[tuple] = []

    def GetHierarchy(self, start, scope):  # noqa: N802 -- COM API name
        self.calls.append(("GetHierarchy", start, scope))
        return self._hierarchy

    def CreateNewPage(self, section_id, _style):  # noqa: N802
        self.calls.append(("CreateNewPage", section_id))
        return self._create

    def UpdatePageContent(self, xml):  # noqa: N802
        self.calls.append(("UpdatePageContent", xml))
        if self._update_raises:
            raise self._update_raises

    def NavigateTo(self, page_id, _placeholder):  # noqa: N802
        self.calls.append(("NavigateTo", page_id))
        if self._navigate_raises:
            raise self._navigate_raises

    def GetHyperlinkToObject(self, page_id, _placeholder):  # noqa: N802
        self.calls.append(("GetHyperlinkToObject", page_id))
        return self._hyperlink


def _make_client(app: _FakeApp) -> oc.OneNoteClient:
    return oc.OneNoteClient(dispatch=lambda _progid: app)


# ---- verify -------------------------------------------------------------


def test_verify_returns_notebook_count():
    app = _FakeApp(hierarchy_xml=(
        '<one:Notebooks xmlns:one="http://schemas.microsoft.com/office/onenote/2013/onenote">'
        '<one:Notebook ID="{N1}" name="A"/>'
        '<one:Notebook ID="{N2}" name="B"/>'
        '</one:Notebooks>'
    ))
    info = oc.verify(dispatch=lambda _p: app)
    assert info == {"ok": True, "notebooks": 2}


def test_verify_raises_unavailable_when_dispatch_fails():
    def failing(_progid):
        raise RuntimeError("CoCreateInstance failed")
    with pytest.raises(oc.OneNoteUnavailable) as exc_info:
        oc.verify(dispatch=failing)
    msg = str(exc_info.value)
    assert "OneNote.Application" in msg
    assert "UWP" in msg  # surfaces the UWP-only-install hint


# ---- list_notebooks -----------------------------------------------------


def test_list_notebooks_parses_flat_sections():
    app = _FakeApp(hierarchy_xml=(
        '<one:Notebooks xmlns:one="http://schemas.microsoft.com/office/onenote/2013/onenote">'
        '<one:Notebook ID="{NB1}" name="Personal">'
            '<one:Section ID="{S1}" name="Inbox"/>'
            '<one:Section ID="{S2}" name="Done"/>'
        '</one:Notebook>'
        '</one:Notebooks>'
    ))
    client = _make_client(app)
    nbs = client.list_notebooks()
    assert len(nbs) == 1
    assert nbs[0].name == "Personal"
    assert [s.name for s in nbs[0].sections] == ["Inbox", "Done"]
    assert nbs[0].sections[0].parent_path == "Personal"


def test_list_notebooks_parses_nested_section_groups():
    app = _FakeApp(hierarchy_xml=(
        '<one:Notebooks xmlns:one="http://schemas.microsoft.com/office/onenote/2013/onenote">'
        '<one:Notebook ID="{NB1}" name="Work">'
            '<one:SectionGroup ID="{G1}" name="Projects">'
                '<one:Section ID="{S1}" name="Alpha"/>'
                '<one:SectionGroup ID="{G2}" name="2026">'
                    '<one:Section ID="{S2}" name="Q3"/>'
                '</one:SectionGroup>'
            '</one:SectionGroup>'
        '</one:Notebook>'
        '</one:Notebooks>'
    ))
    client = _make_client(app)
    nbs = client.list_notebooks()
    group = nbs[0].section_groups[0]
    assert group.name == "Projects"
    assert group.sections[0].name == "Alpha"
    assert group.sections[0].parent_path == "Work / Projects"
    subgroup = group.section_groups[0]
    assert subgroup.name == "2026"
    assert subgroup.sections[0].name == "Q3"
    assert subgroup.sections[0].parent_path == "Work / Projects / 2026"


def test_list_notebooks_handles_empty_hierarchy():
    app = _FakeApp(hierarchy_xml=(
        '<one:Notebooks xmlns:one="http://schemas.microsoft.com/office/onenote/2013/onenote"/>'
    ))
    client = _make_client(app)
    assert client.list_notebooks() == []


def test_list_notebooks_skips_unnamed_attribute():
    """Real OneNote always populates name + ID but the parser shouldn't
    crash if a future schema change drops them."""
    app = _FakeApp(hierarchy_xml=(
        '<one:Notebooks xmlns:one="http://schemas.microsoft.com/office/onenote/2013/onenote">'
        '<one:Notebook>'
            '<one:Section/>'
        '</one:Notebook>'
        '</one:Notebooks>'
    ))
    client = _make_client(app)
    nbs = client.list_notebooks()
    assert len(nbs) == 1
    assert nbs[0].name == "(unnamed)"
    assert nbs[0].sections[0].name == "(unnamed)"


# ---- find_section -------------------------------------------------------


def test_find_section_walks_nested_groups():
    app = _FakeApp(hierarchy_xml=(
        '<one:Notebooks xmlns:one="http://schemas.microsoft.com/office/onenote/2013/onenote">'
        '<one:Notebook ID="{NB1}" name="Work">'
            '<one:SectionGroup ID="{G1}" name="P">'
                '<one:SectionGroup ID="{G2}" name="Sub">'
                    '<one:Section ID="{S1}" name="X"/>'
                '</one:SectionGroup>'
            '</one:SectionGroup>'
        '</one:Notebook>'
        '</one:Notebooks>'
    ))
    client = _make_client(app)
    found = client.find_section("{S1}")
    assert found is not None
    assert found.name == "X"
    assert client.find_section("{missing}") is None


# ---- create_page --------------------------------------------------------


def test_create_page_returns_string_id():
    app = _FakeApp(hierarchy_xml="<one:Notebooks xmlns:one='http://schemas.microsoft.com/office/onenote/2013/onenote'/>",
                   create_returns="{NEW-PAGE}")
    client = _make_client(app)
    pid = client.create_page(section_id="{SECT}")
    assert pid == "{NEW-PAGE}"
    assert ("CreateNewPage", "{SECT}") in app.calls


def test_create_page_handles_tuple_return_from_pywin32_marshal():
    """pywin32 sometimes marshals the [out] BSTR as a tuple. We
    accept either shape so the caller doesn't have to guess."""
    app = _FakeApp(
        hierarchy_xml="<one:Notebooks xmlns:one='http://schemas.microsoft.com/office/onenote/2013/onenote'/>",
        create_returns=("", "{TUPLE-PAGE}"),
    )
    client = _make_client(app)
    pid = client.create_page(section_id="{SECT}")
    assert pid == "{TUPLE-PAGE}"


def test_create_page_raises_onenote_error_on_com_failure():
    class _Failing(_FakeApp):
        def CreateNewPage(self, *_args):  # noqa: N802
            raise RuntimeError("HRESULT 0x80004005")
    app = _Failing(
        hierarchy_xml="<one:Notebooks xmlns:one='http://schemas.microsoft.com/office/onenote/2013/onenote'/>",
    )
    client = _make_client(app)
    with pytest.raises(oc.OneNoteError) as exc:
        client.create_page(section_id="{SECT}")
    assert "CreateNewPage failed" in str(exc.value)
    assert "{SECT}" in str(exc.value)


# ---- update_page_content -----------------------------------------------


def test_update_page_content_passes_xml_through():
    app = _FakeApp(hierarchy_xml="<one:Notebooks xmlns:one='http://schemas.microsoft.com/office/onenote/2013/onenote'/>")
    client = _make_client(app)
    client.update_page_content(page_xml="<one:Page/>")
    assert ("UpdatePageContent", "<one:Page/>") in app.calls


def test_update_page_content_raises_onenote_error_on_com_failure():
    app = _FakeApp(
        hierarchy_xml="<one:Notebooks xmlns:one='http://schemas.microsoft.com/office/onenote/2013/onenote'/>",
        update_raises=RuntimeError("schema error"),
    )
    client = _make_client(app)
    with pytest.raises(oc.OneNoteError) as exc:
        client.update_page_content(page_xml="<one:Page/>")
    assert "UpdatePageContent failed" in str(exc.value)


# ---- navigate_to + get_page_url -----------------------------------------


def test_navigate_to_is_best_effort_and_does_not_raise():
    """NavigateTo failures shouldn't abort the export -- the page was
    successfully saved; opening it in the OneNote window is the
    cherry on top."""
    app = _FakeApp(
        hierarchy_xml="<one:Notebooks xmlns:one='http://schemas.microsoft.com/office/onenote/2013/onenote'/>",
        navigate_raises=RuntimeError("no window"),
    )
    client = _make_client(app)
    # Should NOT raise.
    client.navigate_to(page_id="{P}")


def test_get_page_url_returns_hyperlink_when_supported():
    app = _FakeApp(
        hierarchy_xml="<one:Notebooks xmlns:one='http://schemas.microsoft.com/office/onenote/2013/onenote'/>",
        hyperlink="onenote:///\\\\server\\notebook&section-id={S}&page-id={P}",
    )
    client = _make_client(app)
    url = client.get_page_url(page_id="{P}")
    assert url.startswith("onenote:")


def test_get_page_url_returns_empty_on_failure():
    class _NoHyperlink(_FakeApp):
        def GetHyperlinkToObject(self, *_args):  # noqa: N802
            raise RuntimeError("not implemented")
    app = _NoHyperlink(hierarchy_xml="<one:Notebooks xmlns:one='http://schemas.microsoft.com/office/onenote/2013/onenote'/>")
    client = _make_client(app)
    assert client.get_page_url(page_id="{P}") == ""
