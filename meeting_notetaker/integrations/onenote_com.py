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
    # Order candidates: the CLSID matching our ProgID first
    # (natural pick), then every other CoClass class that exposes
    # GetHierarchy. OneNote ships two Application CoClasses
    # (Application + Application2); on some installs the marshaling
    # for one fails with TYPE_E_LIBNOTREGISTERED on the first method
    # call while the other works. The fallback iteration covers it.
    primary: list[tuple[str, type]] = []
    others: list[tuple[str, type]] = []
    for attr_name in dir(mod):
        cls = getattr(mod, attr_name, None)
        if not isinstance(cls, type):
            continue
        if getattr(cls, "CLSID", None) is None:
            continue
        if cls.CLSID == target_iid:
            primary.append((attr_name, cls))
        else:
            others.append((attr_name, cls))
    candidates = primary + others

    if not candidates:
        raise OneNoteUnavailable(
            f"Generated module for typelib {typelib_libid} has no "
            f"class for CLSID {coclass_clsid}. The typelib "
            f"registration may be corrupt; reinstall the desktop "
            f"OneNote.",
        )

    errors: list[str] = []
    saw_libnotregistered = False
    for attr_name, cls in candidates:
        try:
            instance = cls()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{attr_name} construct: {exc}")
            continue
        if not hasattr(instance, "GetHierarchy"):
            errors.append(f"{attr_name} has no GetHierarchy")
            continue
        # Live smoke test. The cost is one COM round-trip that
        # returns the same XML list_notebooks() returns later; if
        # the marshaling is going to fail, it fails here with a
        # specific candidate name so we can move on.
        try:
            instance.GetHierarchy("", 4)
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            if "-2147319779" in err or "Library not registered" in err:
                saw_libnotregistered = True
            errors.append(f"{attr_name} GetHierarchy call: {exc}")
            continue
        log.debug("OneNote CoClass %s passed smoke test", attr_name)
        return instance

    if saw_libnotregistered:
        raise OneNoteUnavailable(
            "OneNote is installed but its COM registration is "
            "incomplete on this machine. The Windows COM marshaling "
            "layer returned 'Library not registered' "
            "(TYPE_E_LIBNOTREGISTERED) for every entry point we "
            "tried -- a known Microsoft 365 Click-to-Run side "
            "effect. The fix is Microsoft's built-in repair tool:\n\n"
            "  1. Close all Office apps (Outlook, Word, OneNote).\n"
            "  2. Settings > Apps > Installed apps > Microsoft 365.\n"
            "  3. Modify > Quick Repair > Repair.\n"
            "  4. Restart Meeting Notetaker + click Verify again.\n\n"
            "If Quick Repair doesn't help, run Online Repair from "
            "the same dialog (slower, more thorough). In the "
            "meantime Save to Notion / Confluence / Obsidian / PDF / "
            "Word all work without OneNote."
        )

    raise OneNoteUnavailable(
        "Every OneNote CoClass candidate failed. Errors: "
        + " | ".join(errors)
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
    # Phase 3b: registry checks that the universal marshaler needs.
    # TYPE_E_LIBNOTREGISTERED at call time is almost always one of:
    #   * HKCR\Interface\{IID}\TypeLib missing or wrong LIBID
    #   * HKCR\Interface\{IID}\ProxyStubClsid32 missing
    #   * The pointed-at typelib not registered for the current
    #     bitness (32-bit vs 64-bit)
    out.append("--- interface marshaling registrations ---")
    try:
        import winreg  # noqa: PLC0415
        # Pull the IApplication IID out of the gen_py dump above so
        # this still works if OneNote ships a different IID.
        target_iids = [
            "{452AC71A-B655-4967-A208-A4CC39DD7949}",  # IApplication
        ]
        for iid in target_iids:
            out.append(f"  Interface {iid}:")
            for sub in ("TypeLib", "ProxyStubClsid32"):
                try:
                    with winreg.OpenKey(
                        winreg.HKEY_CLASSES_ROOT,
                        fr"Interface\{iid}\{sub}",
                    ) as k:
                        val = winreg.QueryValueEx(k, "")[0]
                    out.append(f"    {sub} = {val}")
                    if sub == "TypeLib":
                        try:
                            with winreg.OpenKey(
                                winreg.HKEY_CLASSES_ROOT,
                                fr"Interface\{iid}\TypeLib",
                            ) as k:
                                ver = winreg.QueryValueEx(k, "Version")[0]
                            out.append(f"    Version = {ver}")
                        except OSError as exc:
                            out.append(f"    Version: MISSING ({exc})")
                except OSError as exc:
                    out.append(f"    {sub}: MISSING ({exc})")
    except Exception as exc:  # noqa: BLE001
        out.append(f"  interface check failed: {exc}")

    # Phase 3c: OneNote server bitness vs Python bitness. Mismatch
    # is the canonical cause for missing 64-bit Interface registrations.
    out.append("--- OneNote server bitness ---")
    try:
        import winreg  # noqa: PLC0415
        # The CoClass CLSID (Application2) we resolved above.
        clsid = "{DC67E480-C3CB-49F8-8232-60B0C2056C8E}"
        for ls_key in ("LocalServer32", "LocalServer"):
            try:
                with winreg.OpenKey(
                    winreg.HKEY_CLASSES_ROOT,
                    fr"CLSID\{clsid}\{ls_key}",
                ) as k:
                    val = winreg.QueryValueEx(k, "")[0]
                out.append(f"  {ls_key} = {val}")
                # Look for the exe and dump architecture by reading PE header
                import re  # noqa: PLC0415
                m = re.match(r'"?([^"]+\.exe)', val, re.IGNORECASE)
                exe_path = m.group(1) if m else val.split(" ")[0]
                out.append(f"  exe path: {exe_path}")
                try:
                    with open(exe_path, "rb") as f:
                        f.seek(0x3C)
                        pe_offset = int.from_bytes(f.read(4), "little")
                        f.seek(pe_offset + 4)
                        machine = int.from_bytes(f.read(2), "little")
                    # 0x014c = x86 32-bit, 0x8664 = x64 64-bit
                    arch = {0x014c: "32-bit", 0x8664: "64-bit"}.get(
                        machine, f"unknown(0x{machine:04x})",
                    )
                    out.append(f"  exe arch: {arch}")
                except OSError as exc:
                    out.append(f"  exe read failed: {exc}")
            except OSError:
                continue
    except Exception as exc:  # noqa: BLE001
        out.append(f"  server-bitness check failed: {exc}")

    # Phase 3d: typelib registered file paths for both bitnesses.
    # Universal marshaler picks via the current process's bitness;
    # if only 32-bit is registered, 64-bit Python's marshaler trips.
    out.append("--- typelib registered files (per bitness) ---")
    try:
        import winreg  # noqa: PLC0415
        libid = "{0EA692EE-BB50-4E3C-AEF0-356D91732725}"
        try:
            with winreg.OpenKey(
                winreg.HKEY_CLASSES_ROOT, fr"TypeLib\{libid}",
            ) as lib_key:
                ver_idx = 0
                while True:
                    try:
                        ver_name = winreg.EnumKey(lib_key, ver_idx)
                    except OSError:
                        break
                    ver_idx += 1
                    if "." not in ver_name:
                        continue
                    out.append(f"  v{ver_name}:")
                    for arch_sub in ("win32", "win64"):
                        try:
                            with winreg.OpenKey(
                                lib_key, fr"{ver_name}\0\{arch_sub}",
                            ) as ak:
                                val = winreg.QueryValueEx(ak, "")[0]
                            out.append(f"    {arch_sub}: {val}")
                        except OSError:
                            out.append(f"    {arch_sub}: not registered")
        except OSError as exc:
            out.append(f"  typelib registry missing: {exc}")
    except Exception as exc:  # noqa: BLE001
        out.append(f"  typelib path check failed: {exc}")

    # Phase 4: exercise every CoClass candidate so we can see which
    # one survives the live GetHierarchy call (the marshaling step
    # that broke for Application2 with TYPE_E_LIBNOTREGISTERED).
    out.append("--- per-CoClass smoke test ---")
    try:
        import pythoncom  # noqa: PLC0415
        for n, c in names_with_clsid:
            cls = getattr(mod, n)
            try:
                inst = cls()
            except Exception as exc:  # noqa: BLE001
                out.append(f"  {n}: construct failed: {exc}")
                continue
            if not hasattr(inst, "GetHierarchy"):
                out.append(f"  {n}: no GetHierarchy method")
                continue
            try:
                xml = inst.GetHierarchy("", 4)
                out.append(
                    f"  {n}: ok; returned {len(xml or '')} chars"
                )
            except Exception as exc:  # noqa: BLE001
                out.append(f"  {n}: GetHierarchy raised: {exc}")
    except Exception as exc:  # noqa: BLE001
        out.append(f"  per-CoClass smoke test failed: {exc}")

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
