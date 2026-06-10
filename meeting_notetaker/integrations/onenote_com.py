"""Thin wrapper around the OneNote desktop COM automation (#100).

OneNote (Microsoft 365 desktop build, formerly OneNote 2016) exposes
``OneNote.Application`` as an in-process COM object. We use it for:

  * ``GetHierarchy(start, scope)`` -> XML tree of notebooks /
    section groups / sections; feeds the picker.
  * ``CreateNewPage(sectionId)`` -> new empty page; returns its id.
  * ``UpdatePageContent(xml, ...)`` -> set the page's title +
    outline content in one call.
  * ``NavigateTo(pageId)`` -> open the new page in the user's
    OneNote window (post-save).

Authentication: none. The OneNote process runs as the signed-in
user; whatever notebooks it has open are what we see.

UWP gotcha: "OneNote for Windows 10" (the deprecated UWP app)
does NOT expose COM. ``Dispatch("OneNote.Application")`` returns
``CoCreateInstance failed`` against UWP-only installs. We surface
that distinctly so the user knows installing the desktop OneNote
fixes it; the Settings page calls this out alongside the Verify
button.

This module is the COM boundary. ``onenote_xml.py`` produces the
page XML; ``export_to_onenote`` (in ``export.py`` later) glues the
two together.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
import xml.etree.ElementTree as ET


log = logging.getLogger("meeting_notetaker.onenote")


ONENOTE_NS_URI = "http://schemas.microsoft.com/office/onenote/2013/onenote"
_NS_MAP = {"one": ONENOTE_NS_URI}


# ---- data shapes ---------------------------------------------------------


@dataclass
class Section:
    id: str
    name: str
    notebook_id: str = ""
    notebook_name: str = ""
    parent_path: str = ""  # display path: "Notebook / Group / Subgroup"


@dataclass
class SectionGroup:
    id: str
    name: str
    parent_path: str = ""
    sections: list[Section] = field(default_factory=list)
    section_groups: list["SectionGroup"] = field(default_factory=list)


@dataclass
class Notebook:
    id: str
    name: str
    sections: list[Section] = field(default_factory=list)
    section_groups: list[SectionGroup] = field(default_factory=list)


# ---- exceptions ----------------------------------------------------------


class OneNoteError(Exception):
    """Operational failure from the COM layer."""


class OneNoteUnavailable(OneNoteError):
    """Raised when Dispatch can't reach the desktop OneNote.

    Common causes: pywin32 not installed (non-Windows / stripped
    env), OneNote not installed, only the UWP "OneNote for Windows
    10" app installed (UWP doesn't expose COM).
    """


# ---- factory + verify ----------------------------------------------------


_DispatchFn = Callable[[str], object]


def _default_dispatch(progid: str) -> object:
    """Real win32com Dispatch with type-library binding.

    Bare ``Dispatch`` uses late binding (IDispatch::Invoke). The
    OneNote desktop client hides ``GetHierarchy`` / ``CreateNewPage``
    / ``UpdatePageContent`` from the IDispatch surface on most
    Click-to-Run installs -- the COM object exists but the methods
    are only reachable through the type library. The symptom is an
    ``AttributeError: OneNote.Application.GetHierarchy`` at the first
    call (which is what Verify hits). ``gencache.EnsureDispatch``
    fixes this by generating a typed wrapper from the typelib at
    Dispatch time.

    Falls back to plain ``Dispatch`` if the gen_py cache directory is
    read-only (rare; some sandboxed environments). The fallback lets
    the wrapper still work on any future install whose IDispatch
    surface is complete.
    """
    import win32com.client  # type: ignore[import-untyped]  # noqa: PLC0415
    try:
        return win32com.client.gencache.EnsureDispatch(progid)
    except Exception:  # noqa: BLE001 -- broad on purpose: gen_py may fail many ways
        return win32com.client.Dispatch(progid)


def verify(*, dispatch: Optional[_DispatchFn] = None) -> dict:
    """Ping OneNote.Application via Dispatch + return a small dict.

    On success: ``{"ok": True, "notebooks": <int>}`` -- the notebook
    count is the diagnostic data the Settings UI shows. On failure:
    raise ``OneNoteUnavailable`` with the original error chained.
    """
    client = OneNoteClient(dispatch=dispatch)
    notebooks = client.list_notebooks()
    return {"ok": True, "notebooks": len(notebooks)}


# ---- main client ---------------------------------------------------------


class OneNoteClient:
    """Stateful adapter around the COM object.

    Caches the Dispatch handle for the lifetime of the instance.
    Hierarchy + page operations re-use the same handle.
    """

    def __init__(self, *, dispatch: Optional[_DispatchFn] = None) -> None:
        self._dispatch = dispatch or _default_dispatch
        try:
            self._app = self._dispatch("OneNote.Application")
        except Exception as exc:  # noqa: BLE001 -- COM raises whatever
            raise OneNoteUnavailable(
                "Could not start OneNote.Application via COM. "
                "The desktop OneNote (Microsoft 365 / 2016+) must be "
                "installed; the UWP 'OneNote for Windows 10' does not "
                "expose COM. Underlying error: " + str(exc)
            ) from exc

    # ---- hierarchy ------------------------------------------------------

    def list_notebooks(self) -> list[Notebook]:
        """Return every notebook OneNote currently has loaded.

        Scope=1 (hsNotebooks) returns the full hierarchy including
        section groups + sections + pages. We parse the XML once
        and group it ourselves to keep the COM round-trip count low.
        """
        xml = self._get_hierarchy(start_node_id="", scope=4)  # hsPages
        return _parse_hierarchy_xml(xml)

    def find_section(self, section_id: str) -> Optional[Section]:
        for nb in self.list_notebooks():
            for s in _walk_sections(nb):
                if s.id == section_id:
                    return s
        return None

    def _get_hierarchy(self, *, start_node_id: str, scope: int) -> str:
        """Wrap the OneNote OM call. The COM signature is:

        ``GetHierarchy(BSTR bstrStartNodeID, HierarchyScope hsScope,
        BSTR* pbstrHierarchyXmlOut)``. The pywin32-generated client
        returns the XML output as the function return value.
        """
        try:
            return self._app.GetHierarchy(start_node_id or "", scope)
        except Exception as exc:  # noqa: BLE001
            raise OneNoteError(
                "GetHierarchy failed: " + str(exc),
            ) from exc

    # ---- page ops -------------------------------------------------------

    def create_page(self, *, section_id: str) -> str:
        """Create a new empty page under ``section_id`` + return its id."""
        try:
            new_id = self._app.CreateNewPage(section_id, "", 0)
        except Exception as exc:  # noqa: BLE001
            raise OneNoteError(
                f"CreateNewPage failed for section {section_id!r}: {exc}",
            ) from exc
        # CreateNewPage returns the new page id as the out parameter.
        # pywin32 marshals the out into the call's return value when
        # the BSTR is the only out; some signatures return ("", id).
        # Handle both shapes.
        if isinstance(new_id, tuple) and len(new_id) >= 2:
            return str(new_id[1])
        return str(new_id or "")

    def update_page_content(self, *, page_xml: str) -> None:
        """Push ``page_xml`` to ``UpdatePageContent``. Force overwrite
        (no last-modified check) since the page was created moments
        ago by ``create_page`` and nothing else should be racing."""
        try:
            self._app.UpdatePageContent(page_xml)
        except Exception as exc:  # noqa: BLE001
            raise OneNoteError(
                "UpdatePageContent failed. The page XML was rejected by "
                "OneNote. Underlying error: " + str(exc),
            ) from exc

    def navigate_to(self, *, page_id: str) -> None:
        """Open ``page_id`` in OneNote. Used for the post-save
        'open after save' affordance."""
        try:
            self._app.NavigateTo(page_id, "")
        except Exception as exc:  # noqa: BLE001
            log.warning("NavigateTo(%s) failed: %s", page_id, exc)

    def get_page_url(self, *, page_id: str) -> str:
        """Best-effort: return the ``onenote:`` URI for ``page_id``.

        Some OneNote builds expose ``GetHyperlinkToObject`` which
        returns a click-to-open URL; others don't. On failure we
        return an empty string and the caller falls back to
        NavigateTo without a clickable link surface."""
        try:
            return self._app.GetHyperlinkToObject(page_id, "")
        except Exception as exc:  # noqa: BLE001
            log.debug("GetHyperlinkToObject(%s) unavailable: %s", page_id, exc)
            return ""


# ---- hierarchy XML parser -----------------------------------------------


def _parse_hierarchy_xml(xml: str) -> list[Notebook]:
    """Convert the ``GetHierarchy`` XML into a list of Notebook dataclasses.

    The XML shape is roughly::

        <one:Notebooks>
          <one:Notebook ID="..." name="...">
            <one:Section ID="..." name="..."/>
            <one:SectionGroup ID="..." name="...">
              <one:Section .../>
              <one:SectionGroup .../>
            </one:SectionGroup>
          </one:Notebook>
        </one:Notebooks>
    """
    if not xml:
        return []
    root = ET.fromstring(xml)
    out: list[Notebook] = []
    for nb_el in root.findall("one:Notebook", _NS_MAP):
        notebook = Notebook(
            id=nb_el.get("ID") or "",
            name=nb_el.get("name") or "(unnamed)",
        )
        _parse_section_children(
            parent_xml=nb_el,
            sections_out=notebook.sections,
            groups_out=notebook.section_groups,
            notebook_id=notebook.id,
            notebook_name=notebook.name,
            parent_path=notebook.name,
        )
        out.append(notebook)
    return out


def _parse_section_children(
    *,
    parent_xml: ET.Element,
    sections_out: list[Section],
    groups_out: list[SectionGroup],
    notebook_id: str,
    notebook_name: str,
    parent_path: str,
) -> None:
    for child in parent_xml:
        tag = child.tag.split("}", 1)[-1]
        if tag == "Section":
            sections_out.append(Section(
                id=child.get("ID") or "",
                name=child.get("name") or "(unnamed)",
                notebook_id=notebook_id,
                notebook_name=notebook_name,
                parent_path=parent_path,
            ))
        elif tag == "SectionGroup":
            group = SectionGroup(
                id=child.get("ID") or "",
                name=child.get("name") or "(unnamed)",
                parent_path=parent_path,
            )
            group_path = f"{parent_path} / {group.name}"
            _parse_section_children(
                parent_xml=child,
                sections_out=group.sections,
                groups_out=group.section_groups,
                notebook_id=notebook_id,
                notebook_name=notebook_name,
                parent_path=group_path,
            )
            groups_out.append(group)


def _walk_sections(nb: Notebook):
    yield from nb.sections
    for g in nb.section_groups:
        yield from _walk_group_sections(g)


def _walk_group_sections(g: SectionGroup):
    yield from g.sections
    for sub in g.section_groups:
        yield from _walk_group_sections(sub)
