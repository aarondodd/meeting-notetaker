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
    """Build a typed OneNote.Application wrapper via the registry.

    Standard ``gencache.EnsureDispatch`` fails because pywin32 needs
    ``GetTypeInfo()`` on the running OneNote instance to find the
    typelib + the IID of the wrapped interface. OneNote refuses
    ``GetTypeInfo()``. Plain ``Dispatch`` returns ``CDispatch`` and
    every method call raises ``AttributeError(progid + "." + name)``
    because OneNote also hides those methods from the IDispatch
    surface.

    The workaround does not touch the live instance at all:

      1. ProgID -> CoClass CLSID via ``HKCR\\<ProgID>\\CLSID``.
      2. CoClass CLSID -> TypeLib LIBID + version via
         ``HKCR\\CLSID\\{...}\\TypeLib`` and ``\\Version``.
      3. ``gencache.EnsureModule(LIBID, lcid, major, minor)`` runs
         makepy against the registered typelib + drops a typed
         wrapper module into gen_py.
      4. Find the gen_py CoClass class whose ``CLSID`` matches our
         CoClass CLSID + instantiate it. The CoClass base class
         calls ``CoCreateInstance(CLSID)`` internally, which attaches
         to the running OneNote process and wraps the resulting
         IDispatch in the typed interface class -- no GetTypeInfo
         on the live object required.

    Try EnsureDispatch first so installs whose typelib binding
    works the normal way (most non-OneNote COM servers) take the
    fast path.
    """
    import win32com.client  # type: ignore[import-untyped]  # noqa: PLC0415

    try:
        client = win32com.client.gencache.EnsureDispatch(progid)
        if hasattr(client, "GetHierarchy"):
            return client
    except TypeError:
        pass  # GetTypeInfo refused; use the registry path below.
    except Exception as exc:  # noqa: BLE001
        # Other Dispatch failure (CLSID missing entirely, COM error,
        # etc.) -- the registry path will hit the same wall and give
        # a clearer error.
        log.debug("EnsureDispatch raised %s; trying registry path", exc)

    return _dispatch_via_registry(progid)


def _dispatch_via_registry(progid: str) -> object:
    """Bypass GetTypeInfo entirely: walk the registry to build a
    typed CoClass wrapper. Raises ``OneNoteUnavailable`` with a
    specific message at the first failing step."""
    import winreg  # noqa: PLC0415

    import pythoncom  # type: ignore[import-untyped]  # noqa: PLC0415
    from win32com.client import gencache  # noqa: PLC0415

    coclass_clsid = _read_progid_clsid(progid)
    typelib_libid, major, minor = _read_coclass_typelib(coclass_clsid)
    try:
        mod = gencache.EnsureModule(typelib_libid, 0, major, minor)
    except Exception as exc:  # noqa: BLE001
        raise OneNoteUnavailable(
            f"makepy could not build a wrapper for the OneNote "
            f"type library {typelib_libid} v{major}.{minor}: {exc}",
        ) from exc

    target_iid = pythoncom.MakeIID(coclass_clsid)
    for attr_name in dir(mod):
        cls = getattr(mod, attr_name, None)
        if not isinstance(cls, type):
            continue
        if getattr(cls, "CLSID", None) != target_iid:
            continue
        try:
            instance = cls()
        except Exception as exc:  # noqa: BLE001
            raise OneNoteUnavailable(
                f"CoCreateInstance({coclass_clsid}) via {attr_name} "
                f"failed: {exc}",
            ) from exc
        if not hasattr(instance, "GetHierarchy"):
            raise OneNoteUnavailable(
                f"Typed wrapper {attr_name} has no GetHierarchy. "
                f"The OneNote type library is registered but missing "
                f"the IApplication interface.",
            )
        return instance

    raise OneNoteUnavailable(
        f"Generated module for typelib {typelib_libid} has no class "
        f"for CLSID {coclass_clsid}. The typelib registration may be "
        f"corrupt; reinstall the desktop OneNote.",
    )


def _read_progid_clsid(progid: str) -> str:
    """``HKCR\\<ProgID>\\CLSID`` -> ``"{XXXXXXXX-...}"``."""
    import winreg  # noqa: PLC0415
    try:
        with winreg.OpenKey(
            winreg.HKEY_CLASSES_ROOT, fr"{progid}\CLSID",
        ) as key:
            value = winreg.QueryValueEx(key, "")[0]
    except OSError as exc:
        raise OneNoteUnavailable(
            f"Registry path HKCR\\{progid}\\CLSID is missing. "
            f"The desktop OneNote may not be installed, or its "
            f"COM registration is broken. ({exc})",
        ) from exc
    if not value:
        raise OneNoteUnavailable(
            f"HKCR\\{progid}\\CLSID has an empty value.",
        )
    return value if value.startswith("{") else "{" + value + "}"


def _read_coclass_typelib(coclass_clsid: str) -> tuple[str, int, int]:
    """``HKCR\\CLSID\\{...}\\TypeLib`` + ``\\Version``.

    Returns ``(libid_string, major, minor)``.

    Version strings under ``HKCR\\CLSID\\...\\Version`` are decimal
    (e.g. ``"1.1"``) -- distinct from the per-version subkey names
    under ``HKCR\\TypeLib\\{LIBID}`` which are hex.
    """
    import winreg  # noqa: PLC0415

    try:
        with winreg.OpenKey(
            winreg.HKEY_CLASSES_ROOT, fr"CLSID\{coclass_clsid}\TypeLib",
        ) as key:
            libid = winreg.QueryValueEx(key, "")[0]
    except OSError as exc:
        raise OneNoteUnavailable(
            f"OneNote CoClass {coclass_clsid} has no registered "
            f"TypeLib. ({exc})",
        ) from exc

    version_str = ""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CLASSES_ROOT, fr"CLSID\{coclass_clsid}\Version",
        ) as key:
            version_str = winreg.QueryValueEx(key, "")[0]
    except OSError:
        version_str = ""

    if version_str:
        try:
            major_s, _, minor_s = version_str.partition(".")
            return libid, int(major_s), int(minor_s or "0")
        except ValueError:
            pass

    # Version subkey is absent or unparseable: fall back to the
    # highest-versioned entry under HKCR\TypeLib\{LIBID}. Subkey
    # names there are HEX (typelib's wMajorVerNum / wMinorVerNum
    # rendered as hex digits).
    best: Optional[tuple[int, int]] = None
    try:
        with winreg.OpenKey(
            winreg.HKEY_CLASSES_ROOT, fr"TypeLib\{libid}",
        ) as lib_key:
            idx = 0
            while True:
                try:
                    sub = winreg.EnumKey(lib_key, idx)
                except OSError:
                    break
                idx += 1
                if "." not in sub:
                    continue
                ma_s, _, mi_s = sub.partition(".")
                try:
                    ma, mi = int(ma_s, 16), int(mi_s, 16)
                except ValueError:
                    continue
                if best is None or (ma, mi) > best:
                    best = (ma, mi)
    except OSError as exc:
        raise OneNoteUnavailable(
            f"No registered versions for typelib {libid}. ({exc})",
        ) from exc

    if best is None:
        raise OneNoteUnavailable(
            f"Typelib {libid} has no parseable version subkeys.",
        )
    return libid, best[0], best[1]


def diagnose_onenote_com() -> str:
    """Multi-line diagnostic report covering every COM path we try.

    Surfaced via the Settings dialog's Verify error fallback so the
    operator gets a complete picture (pywin32 version, gen_py cache
    path, EnsureDispatch error, plain Dispatch error, method
    visibility on the dispatched object, GetHierarchy invocation).
    """
    out: list[str] = []
    try:
        import sys  # noqa: PLC0415
        out.append(
            f"Python: {'.'.join(map(str, sys.version_info[:3]))}, "
            f"{'64' if sys.maxsize > 2**32 else '32'}-bit"
        )
    except Exception as exc:  # noqa: BLE001
        out.append(f"Python info: {exc}")
    try:
        import win32com.client  # noqa: PLC0415
        out.append(f"pywin32 win32com.client: {win32com.client.__file__}")
    except Exception as exc:  # noqa: BLE001
        out.append(f"pywin32 import failed: {exc}")
        return "\n".join(out)
    try:
        from win32com.client import gencache  # noqa: PLC0415
        out.append(f"gen_py cache path: {gencache.GetGeneratePath()}")
        try:
            import os  # noqa: PLC0415
            cache_path = gencache.GetGeneratePath()
            out.append(f"  cache writable: {os.access(cache_path, os.W_OK)}")
        except Exception as exc:  # noqa: BLE001
            out.append(f"  cache writability check failed: {exc}")
    except Exception as exc:  # noqa: BLE001
        out.append(f"gen_py: {exc}")

    # Phase 1: EnsureDispatch
    out.append("--- EnsureDispatch('OneNote.Application') ---")
    try:
        client = win32com.client.gencache.EnsureDispatch("OneNote.Application")
        out.append(f"  ok; type={type(client).__name__}")
        out.append(f"  has GetHierarchy: {hasattr(client, 'GetHierarchy')}")
        if hasattr(client, "GetHierarchy"):
            try:
                xml = client.GetHierarchy("", 4)
                out.append(
                    f"  GetHierarchy ok; returned {len(xml or '')} chars"
                )
            except Exception as exc:  # noqa: BLE001
                out.append(
                    f"  GetHierarchy raised: {type(exc).__name__}: {exc}"
                )
    except Exception as exc:  # noqa: BLE001
        out.append(f"  failed: {type(exc).__name__}: {exc}")

    # Phase 2: plain Dispatch
    out.append("--- Dispatch('OneNote.Application') ---")
    try:
        client = win32com.client.Dispatch("OneNote.Application")
        out.append(f"  ok; type={type(client).__name__}")
        out.append(f"  has GetHierarchy: {hasattr(client, 'GetHierarchy')}")
        if hasattr(client, "GetHierarchy"):
            try:
                xml = client.GetHierarchy("", 4)
                out.append(
                    f"  GetHierarchy ok; returned {len(xml or '')} chars"
                )
            except Exception as exc:  # noqa: BLE001
                out.append(
                    f"  GetHierarchy raised: {type(exc).__name__}: {exc}"
                )
    except Exception as exc:  # noqa: BLE001
        out.append(f"  failed: {type(exc).__name__}: {exc}")

    # Phase 3: the registry-walk workaround we actually use.
    out.append("--- registry-walk path ---")
    try:
        clsid = _read_progid_clsid("OneNote.Application")
        out.append(f"  ProgID -> CLSID: {clsid}")
    except Exception as exc:  # noqa: BLE001
        out.append(f"  ProgID -> CLSID failed: {type(exc).__name__}: {exc}")
        return "\n".join(out)
    try:
        libid, major, minor = _read_coclass_typelib(clsid)
        out.append(f"  CLSID -> TypeLib: {libid} v{major}.{minor}")
    except Exception as exc:  # noqa: BLE001
        out.append(f"  CLSID -> TypeLib failed: {type(exc).__name__}: {exc}")
        return "\n".join(out)
    try:
        from win32com.client import gencache  # noqa: PLC0415
        mod = gencache.EnsureModule(libid, 0, major, minor)
        out.append(f"  EnsureModule ok; module={mod.__name__}")
        names_with_clsid = [
            (n, getattr(mod, n).CLSID)
            for n in dir(mod)
            if isinstance(getattr(mod, n, None), type)
            and getattr(getattr(mod, n), "CLSID", None) is not None
        ]
        out.append(f"  CoClass candidates: {len(names_with_clsid)}")
        for n, c in names_with_clsid[:6]:
            out.append(f"    {n}: {c}")
    except Exception as exc:  # noqa: BLE001
        out.append(f"  EnsureModule failed: {type(exc).__name__}: {exc}")
        return "\n".join(out)
    try:
        client = _dispatch_via_registry("OneNote.Application")
        out.append(f"  registry-walk dispatch ok; type={type(client).__name__}")
        out.append(f"  has GetHierarchy: {hasattr(client, 'GetHierarchy')}")
        if hasattr(client, "GetHierarchy"):
            try:
                xml = client.GetHierarchy("", 4)
                out.append(
                    f"  GetHierarchy ok; returned {len(xml or '')} chars"
                )
            except Exception as exc:  # noqa: BLE001
                out.append(
                    f"  GetHierarchy raised: {type(exc).__name__}: {exc}"
                )
    except Exception as exc:  # noqa: BLE001
        out.append(
            f"  registry-walk dispatch failed: "
            f"{type(exc).__name__}: {exc}"
        )

    return "\n".join(out)


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
