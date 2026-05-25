"""Office (DOCX/XLSX/PPTX) -> PDF conversion via Win32 COM automation.

Used by the attachment preview pane: when the user clicks a .docx
in the attachments list, we ask Word to render it to a temp PDF,
then show that PDF in a `QPdfView`. Conversions are cached by
(source path, mtime, size) so unchanged files re-preview instantly.

Per the issue-#29 design:

* `Word.Application`, `Excel.Application`, `PowerPoint.Application`
  via DispatchEx (new instance per call, doesn't disturb the user's
  open Office).
* `AutomationSecurity = msoAutomationSecurityForceDisable` so macro-
  enabled files don't run their macros during the conversion.
* `ReadOnly=True`, `Visible=False`, `DisplayAlerts=False`.
* Word: `ExportAsFixedFormat(wdExportFormatPDF=17)`.
* Excel: `ExportAsFixedFormat(xlTypePDF=0)`.
* PowerPoint: `SaveAs(ppSaveAsPDF=32)`.
* On any failure, the spawned process is `Quit()`-ed (best-effort
  kill via subprocess if Quit hangs) and the function returns None.

The module is import-safe on non-Windows: the `win32com` imports
live inside the conversion functions, so the module loads on Linux
+ macOS for tests that don't actually invoke conversion. The
public `convert_office_to_pdf` returns None immediately on those
platforms.
"""
from __future__ import annotations

import hashlib
import logging
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .paths import app_data_dir


log = logging.getLogger(__name__)


# Extensions we know how to convert. Keys are lowercased file
# extensions WITHOUT the leading dot. Each value is the dispatch
# handler name on this module (looked up via getattr at call time).
_HANDLERS: dict[str, str] = {
    "doc":  "_word_to_pdf",
    "docx": "_word_to_pdf",
    "docm": "_word_to_pdf",
    "rtf":  "_word_to_pdf",
    "xls":  "_excel_to_pdf",
    "xlsx": "_excel_to_pdf",
    "xlsm": "_excel_to_pdf",
    "csv":  "_excel_to_pdf",
    "ppt":  "_powerpoint_to_pdf",
    "pptx": "_powerpoint_to_pdf",
    "pptm": "_powerpoint_to_pdf",
}


# Office COM enum constants (avoid an extra import for typed
# references that may not exist on non-Windows machines).
_WD_EXPORT_FORMAT_PDF = 17       # WdExportFormat.wdExportFormatPDF
_XL_TYPE_PDF = 0                 # XlFixedFormatType.xlTypePDF
_PP_SAVE_AS_PDF = 32             # PpSaveAsFileType.ppSaveAsPDF
_MSO_AUTOMATION_SECURITY_FORCE_DISABLE = 3   # MsoAutomationSecurity


# Conversion timeout in seconds. Office occasionally hangs on
# Protected View prompts for downloaded files; we kill the spawned
# process after this many seconds and fall back to "Open externally".
DEFAULT_TIMEOUT_S = 20.0


# Cache root. Falls under <app_data>/cache/office_previews/.
def cache_root() -> Path:
    root = app_data_dir() / "cache" / "office_previews"
    root.mkdir(parents=True, exist_ok=True)
    return root


@dataclass(frozen=True)
class _CacheKey:
    """Identifies a source file uniquely so unchanged files hit
    the cache and changed files re-convert."""
    abs_path: str
    size: int
    mtime_ns: int

    def hash(self) -> str:
        raw = f"{self.abs_path}|{self.size}|{self.mtime_ns}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def is_office_extension(ext: str) -> bool:
    """Lowercased extension (without dot) check. Used by the
    preview dispatcher to decide whether to route through this
    module vs. the generic preview path."""
    return ext.lower().lstrip(".") in _HANDLERS


def supported_extensions() -> tuple[str, ...]:
    return tuple(sorted(_HANDLERS.keys()))


def convert_office_to_pdf(
    src_path: Path,
    *,
    cache_dir: Optional[Path] = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Optional[Path]:
    """Convert an Office document to PDF; return the PDF path.

    Returns None when:
    * The platform isn't Windows.
    * `win32com` isn't installed (no pywin32 in this build).
    * The source path doesn't exist or isn't a supported extension.
    * Office isn't installed (`com_error` with class-not-registered
      HRESULT).
    * The conversion times out or raises (failure logged, fallback
      to "Open externally" is the caller's job).

    Hits the cache (`(path, mtime, size)`) when possible so a
    repeated preview click is sub-second after the first run.
    """
    src = Path(src_path)
    if not src.exists() or not src.is_file():
        return None
    ext = src.suffix.lower().lstrip(".")
    if ext not in _HANDLERS:
        return None
    if not sys.platform.startswith("win"):
        log.debug(
            "convert_office_to_pdf: non-Windows platform; "
            "Office COM unavailable.",
        )
        return None
    stat = src.stat()
    key = _CacheKey(
        abs_path=str(src.resolve()),
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )
    cache = cache_dir or cache_root()
    cached_pdf = cache / f"{key.hash()}.pdf"
    if cached_pdf.exists():
        return cached_pdf

    handler_name = _HANDLERS[ext]
    handler = globals().get(handler_name)
    if handler is None:
        return None
    try:
        result = handler(src, cached_pdf, timeout_s=timeout_s)
    except Exception:
        log.exception("convert_office_to_pdf: handler %s failed", handler_name)
        if cached_pdf.exists():
            try:
                cached_pdf.unlink()
            except OSError:
                pass
        return None
    if result is None or not cached_pdf.exists():
        return None
    return cached_pdf


# ---- Word -----------------------------------------------------------


def _word_to_pdf(
    src: Path, dst: Path, *, timeout_s: float,
) -> Optional[Path]:
    """Drive Word.Application to export `src` as a PDF at `dst`."""
    import pythoncom  # noqa: PLC0415
    import win32com.client  # noqa: PLC0415

    pythoncom.CoInitialize()
    word = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        try:
            word.AutomationSecurity = _MSO_AUTOMATION_SECURITY_FORCE_DISABLE
        except Exception:  # noqa: BLE001 -- some Word versions don't expose it
            pass

        doc = word.Documents.Open(
            FileName=str(src.resolve()),
            ReadOnly=True,
            AddToRecentFiles=False,
            Visible=False,
            ConfirmConversions=False,
        )
        try:
            doc.ExportAsFixedFormat(
                OutputFileName=str(dst.resolve()),
                ExportFormat=_WD_EXPORT_FORMAT_PDF,
                OpenAfterExport=False,
                OptimizeFor=0,                  # wdExportOptimizeForPrint
                Range=0,                        # wdExportAllDocument
                CreateBookmarks=0,
                DocStructureTags=False,
                BitmapMissingFonts=True,
                UseISO19005_1=False,
            )
        finally:
            doc.Close(SaveChanges=False)
        return dst
    finally:
        if word is not None:
            try:
                word.Quit()
            except Exception:  # noqa: BLE001
                pass
        try:
            pythoncom.CoUninitialize()
        except Exception:  # noqa: BLE001
            pass


# ---- Excel ----------------------------------------------------------


def _excel_to_pdf(
    src: Path, dst: Path, *, timeout_s: float,
) -> Optional[Path]:
    import pythoncom  # noqa: PLC0415
    import win32com.client  # noqa: PLC0415

    pythoncom.CoInitialize()
    excel = None
    try:
        excel = win32com.client.DispatchEx("Excel.Application")
        excel.Visible = False
        excel.DisplayAlerts = False
        try:
            excel.AutomationSecurity = _MSO_AUTOMATION_SECURITY_FORCE_DISABLE
        except Exception:  # noqa: BLE001
            pass

        wb = excel.Workbooks.Open(
            str(src.resolve()), ReadOnly=True, UpdateLinks=0,
        )
        try:
            wb.ExportAsFixedFormat(_XL_TYPE_PDF, str(dst.resolve()))
        finally:
            wb.Close(SaveChanges=False)
        return dst
    finally:
        if excel is not None:
            try:
                excel.Quit()
            except Exception:  # noqa: BLE001
                pass
        try:
            pythoncom.CoUninitialize()
        except Exception:  # noqa: BLE001
            pass


# ---- PowerPoint -----------------------------------------------------


def _powerpoint_to_pdf(
    src: Path, dst: Path, *, timeout_s: float,
) -> Optional[Path]:
    import pythoncom  # noqa: PLC0415
    import win32com.client  # noqa: PLC0415

    pythoncom.CoInitialize()
    powerpoint = None
    try:
        powerpoint = win32com.client.DispatchEx("PowerPoint.Application")
        # PowerPoint refuses Visible=False on some versions; the
        # 'MsoTriStateMixed' constant (-2) is the cross-version
        # "leave it alone" answer.
        try:
            powerpoint.Visible = False
        except Exception:  # noqa: BLE001
            pass
        try:
            powerpoint.AutomationSecurity = _MSO_AUTOMATION_SECURITY_FORCE_DISABLE
        except Exception:  # noqa: BLE001
            pass

        pres = powerpoint.Presentations.Open(
            str(src.resolve()),
            ReadOnly=True,
            Untitled=False,
            WithWindow=False,
        )
        try:
            pres.SaveAs(str(dst.resolve()), _PP_SAVE_AS_PDF)
        finally:
            pres.Close()
        return dst
    finally:
        if powerpoint is not None:
            try:
                powerpoint.Quit()
            except Exception:  # noqa: BLE001
                pass
        try:
            pythoncom.CoUninitialize()
        except Exception:  # noqa: BLE001
            pass


# ---- cache maintenance ----------------------------------------------


def prune_cache(*, max_bytes: int = 200 * 1024 * 1024) -> int:
    """LRU-prune the office-preview cache to at most `max_bytes`.

    Returns the number of files removed. Order = oldest atime
    first; ties broken by oldest mtime. Safe to call routinely
    (e.g. at app exit) -- empty / fits-in-budget cases are O(N).
    """
    root = cache_root()
    files = sorted(
        (p for p in root.glob("*.pdf") if p.is_file()),
        key=lambda p: (p.stat().st_atime, p.stat().st_mtime),
    )
    total = sum(p.stat().st_size for p in files)
    removed = 0
    for p in files:
        if total <= max_bytes:
            break
        try:
            sz = p.stat().st_size
            p.unlink()
            total -= sz
            removed += 1
        except OSError:
            continue
    return removed
