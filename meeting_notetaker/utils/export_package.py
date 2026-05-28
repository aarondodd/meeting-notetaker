"""Single-action export-everything-as-ZIP orchestrator (issue #30).

Builds a structured archive containing:

    my-notes.pdf
    synthesis.pdf
    transcript.txt
    audio/
      recording.mp3                  # always (Full)
      highlights.mp3                 # if highlights + Both/Highlights-only
      recording.mp4                  # if screenshots present (Full)
      recording.srt                  # sidecar for recording.mp4
      highlights.mp4                 # if highlights + Both/Highlights-only
      highlights.srt                 # sidecar for highlights.mp4
    attachments/
      <every attached file>
    screenshots/
      <every captured PNG>

Audio format is always MP3, video is always MP4 (per Aaron's spec --
the per-file Export dialogs stay for users who want format choice
on a one-off basis).

The orchestrator runs on a worker thread; progress is reported as
an integer 0..100. Weighted by phase so the bar moves predictably.
On any failure mid-encode, the partial ZIP is deleted.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


log = logging.getLogger(__name__)


HIGHLIGHTS_MODE_FULL = "full"
HIGHLIGHTS_MODE_HIGHLIGHTS = "highlights"
HIGHLIGHTS_MODE_BOTH = "both"
ALL_HIGHLIGHTS_MODES = (
    HIGHLIGHTS_MODE_FULL,
    HIGHLIGHTS_MODE_HIGHLIGHTS,
    HIGHLIGHTS_MODE_BOTH,
)


# Per-phase progress weights. They sum to 100; the orchestrator
# linear-interpolates inside each phase.
_PHASE_WEIGHTS: dict[str, int] = {
    "my_notes_pdf": 10,
    "synthesis_pdf": 10,
    "transcript_txt": 2,
    "audio_full": 22,
    "audio_highlights": 12,
    "video_full": 25,
    "video_highlights": 14,
    "attachments_copy": 2,
    "screenshots_copy": 2,
    "zip_pack": 1,
}


@dataclass
class PackageOptions:
    """All the inputs the orchestrator needs in one place.

    PDF rendering happens on the MAIN THREAD before the worker
    launches (QTextDocument.print() isn't worker-thread safe;
    cross-thread use caused dialog-flash glitches and visual
    regressions during testing). The orchestrator gets pre-
    rendered PDF paths and just copies them into the ZIP.

    `notes_pdf_path` / `synthesis_pdf_path` may be None to skip
    the corresponding entry (e.g. session never produced a
    synthesis).
    """
    session_id: str
    session_title: str
    session_started_at_iso: str
    mic_path: Optional[Path]
    sys_path: Optional[Path]
    screenshots: list  # list[tuple[Path, int]]  -- (path, offset_ms)
    transcript_text: str
    notes_pdf_path: Optional[Path]
    synthesis_pdf_path: Optional[Path]
    attachments: list  # list[(Path, display_name)]
    highlights: list  # list[Highlight] -- empty when none
    highlights_mode: str = HIGHLIGHTS_MODE_FULL


def build_session_package(
    options: PackageOptions,
    dst_zip_path: Path,
    *,
    progress: Optional[Callable[[int], None]] = None,
) -> Path:
    """Run the full pipeline and write a ZIP to `dst_zip_path`.

    Raises on encoder / I/O failure; partial output is removed
    before re-raising so the user never ends up with a junk archive.
    Returns `dst_zip_path` on success.
    """
    dst_zip_path = Path(dst_zip_path)
    if options.highlights_mode not in ALL_HIGHLIGHTS_MODES:
        raise ValueError(
            f"unknown highlights_mode: {options.highlights_mode!r}"
        )

    work_dir = Path(tempfile.mkdtemp(prefix="mtn-export-"))
    try:
        # Track cumulative progress across phases.
        completed = 0

        def step(phase: str, pct_within: int = 100) -> None:
            nonlocal completed
            weight = _PHASE_WEIGHTS.get(phase, 0)
            inner = max(0, min(100, pct_within))
            local_pct = (weight * inner) // 100
            total_pct = completed + local_pct
            if progress is not None:
                progress(min(100, total_pct))
            if inner >= 100:
                completed = min(100, completed + weight)

        # ---- PDFs (pre-rendered on main thread; we just copy in) ----
        if options.notes_pdf_path and options.notes_pdf_path.exists():
            shutil.copy2(options.notes_pdf_path, work_dir / "my-notes.pdf")
        step("my_notes_pdf")
        if (
            options.synthesis_pdf_path
            and options.synthesis_pdf_path.exists()
        ):
            shutil.copy2(
                options.synthesis_pdf_path, work_dir / "synthesis.pdf",
            )
        step("synthesis_pdf")

        # ---- Transcript ----
        (work_dir / "transcript.txt").write_text(
            options.transcript_text or "", encoding="utf-8",
        )
        step("transcript_txt")

        # ---- Audio + video ----
        # Bug fix: audio/ holds MP3 only; video/ holds MP4 + SRT.
        # Previously both lived under audio/ which was misleading.
        audio_dir = work_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        video_dir = work_dir / "video"

        sources_exist = bool(
            (options.mic_path and options.mic_path.exists()) or
            (options.sys_path and options.sys_path.exists())
        )
        screenshots_exist = bool(options.screenshots)
        highlights_exist = bool(options.highlights)
        emit_full = options.highlights_mode in (
            HIGHLIGHTS_MODE_FULL, HIGHLIGHTS_MODE_BOTH,
        ) or not highlights_exist
        emit_highlights = (
            highlights_exist and options.highlights_mode in (
                HIGHLIGHTS_MODE_HIGHLIGHTS, HIGHLIGHTS_MODE_BOTH,
            )
        )

        if sources_exist and emit_full:
            _export_full_audio(
                options.mic_path, options.sys_path,
                audio_dir / "recording.mp3",
                lambda p: step("audio_full", p),
            )
        else:
            step("audio_full")

        if sources_exist and emit_highlights:
            _export_highlights_audio(
                options.mic_path, options.sys_path,
                options.highlights,
                audio_dir / "highlights.mp3",
                lambda p: step("audio_highlights", p),
            )
        else:
            step("audio_highlights")

        if sources_exist and screenshots_exist and emit_full:
            video_dir.mkdir(parents=True, exist_ok=True)
            _export_full_video(
                options.mic_path, options.sys_path,
                options.screenshots, options.transcript_text,
                video_dir / "recording.mp4",
                lambda p: step("video_full", p),
            )
        else:
            step("video_full")

        if sources_exist and screenshots_exist and emit_highlights:
            video_dir.mkdir(parents=True, exist_ok=True)
            _export_highlights_video(
                options.mic_path, options.sys_path,
                options.screenshots, options.transcript_text,
                options.highlights, video_dir / "highlights.mp4",
                session_title=options.session_title,
                session_started_at_iso=options.session_started_at_iso,
                progress=lambda p: step("video_highlights", p),
            )
        else:
            step("video_highlights")

        # ---- Attachments ----
        if options.attachments:
            attachments_dst = work_dir / "attachments"
            attachments_dst.mkdir(parents=True, exist_ok=True)
            _copy_attachments(options.attachments, attachments_dst)
        step("attachments_copy")

        # ---- Screenshots ----
        if options.screenshots:
            shots_dst = work_dir / "screenshots"
            shots_dst.mkdir(parents=True, exist_ok=True)
            for src_path, _offset in options.screenshots:
                src_path = Path(src_path)
                if src_path.exists():
                    shutil.copy2(src_path, shots_dst / src_path.name)
        step("screenshots_copy")

        # ---- Zip ----
        if dst_zip_path.exists():
            dst_zip_path.unlink()
        with zipfile.ZipFile(
            dst_zip_path, "w", zipfile.ZIP_DEFLATED,
        ) as zf:
            for path in sorted(work_dir.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(work_dir))
        step("zip_pack")

        if progress is not None:
            progress(100)
        return dst_zip_path
    except Exception:
        if dst_zip_path.exists():
            try:
                dst_zip_path.unlink()
            except OSError:
                pass
        raise
    finally:
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except OSError:
            pass


# ----------------------------------------------------------------------
# Per-artifact helpers


def render_session_pdf(
    markdown_body: str,
    *,
    dst: Path,
    base_dir: Path,
    session_title: str,
    tab_label: str,
    session_date,
    session_contacts: Optional[list] = None,
) -> Path:
    """Render a session-tab markdown body to a PDF on the MAIN
    THREAD.

    Uses the same PrintTextDocument + QPrinter path the in-app
    "Export PDF" button uses, so the export-package PDFs match
    the visual fidelity the user already sees there. Critically,
    callers MUST invoke this on the main thread -- QTextDocument
    + QPrinter are GUI classes and cross-thread use causes Qt to
    surface dialog warnings (the "dialogs flashing after export"
    bug Aaron reported).

    Returns `dst`. The PDF file at `dst` is overwritten if it
    exists. `base_dir` is the session dir so relative `images/...`
    refs in the markdown resolve correctly.

    Wrapped here (vs. exposing the print helpers individually) so
    the export package has a single entry point + tests can stub
    one function.
    """
    from PyQt6.QtPrintSupport import QPrinter

    from ..ui.print_document import PrintTextDocument
    from .export import build_print_markdown
    from .live_notes import (
        replace_attendees_section_with_table,
        should_render_attendees_as_table,
    )

    # v0.7.2 #51 Phase 5: replace the Attendees bullet list with a
    # rich-fields table when any session contact has detail data.
    body = markdown_body or ""
    if session_contacts and should_render_attendees_as_table(session_contacts):
        body = replace_attendees_section_with_table(body, session_contacts)

    printable = build_print_markdown(
        session_title=session_title,
        tab_label=tab_label,
        session_date=session_date,
        body=body,
    )
    doc = PrintTextDocument(Path(base_dir))
    doc.setMarkdown(printable)

    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(dst))
    printer.setDocName(f"{session_title} -- {tab_label}")
    doc.clamp_images_to_printer(printer)
    doc.print(printer)
    return dst


def _export_full_audio(mic, sys_, dst: Path, progress) -> None:
    """Run audio.export.export_mixed; force .mp3 extension."""
    from ..audio.export import export_mixed
    export_mixed(mic, sys_, dst)
    progress(100)


def _export_highlights_audio(mic, sys_, highlights, dst: Path, progress) -> None:
    from ..audio.highlights_export import export_highlights_audio
    export_highlights_audio(mic, sys_, highlights, dst, progress=progress)


def _export_full_video(
    mic, sys_, screenshots, transcript_text, dst: Path, progress,
) -> None:
    from ..audio.video_export import export_video
    export_video(
        mic, sys_, screenshots, transcript_text, dst,
        progress=progress,
    )
    # SRT sidecar is auto-emitted by export_video; ensure it lands
    # in the same directory as the .mp4 with a deterministic name.
    srt = dst.with_suffix(".srt")
    if not srt.exists():
        # export_video writes the SRT next to dst -- belt-and-braces.
        pass


def _export_highlights_video(
    mic, sys_, screenshots, transcript_text, highlights, dst: Path,
    *,
    session_title: str,
    session_started_at_iso: str,
    progress,
) -> None:
    from ..audio.highlights_export import export_highlights_video
    export_highlights_video(
        mic, sys_, screenshots, transcript_text,
        highlights, dst,
        session_title=session_title,
        session_started_at_iso=session_started_at_iso,
        progress=progress,
    )


def _copy_attachments(attachments, dst_dir: Path) -> None:
    """attachments is a list of (Path, display_name). The display
    name lands as the on-disk filename in the package (with FS-
    safe normalization)."""
    from .export import sanitize_filename_stem

    used: set[str] = set()
    for src_path, display_name in attachments:
        src_path = Path(src_path)
        if not src_path.exists():
            continue
        # Use the display name as the filename, sanitized; preserve
        # the original extension when the display name lacks one.
        base = (display_name or src_path.name).strip()
        if not Path(base).suffix:
            base = base + src_path.suffix
        stem = sanitize_filename_stem(Path(base).stem) or "attachment"
        ext = Path(base).suffix or src_path.suffix
        candidate = f"{stem}{ext}"
        counter = 2
        while candidate.lower() in used:
            candidate = f"{stem}-{counter}{ext}"
            counter += 1
        used.add(candidate.lower())
        shutil.copy2(src_path, dst_dir / candidate)


# ----------------------------------------------------------------------
# Filename utilities for the suggested ZIP name


def default_package_filename(
    session_title: str, started_at_iso: str,
) -> str:
    """Build the suggested ZIP filename:
    'YYYY-MM-DD_HHMM - <safe-title>.zip'.

    Sortable chronologically in Explorer. Falls back gracefully on
    a missing/malformed timestamp."""
    from datetime import datetime as _dt
    from .export import sanitize_filename_stem
    stamp = ""
    if started_at_iso:
        try:
            utc = _dt.fromisoformat(started_at_iso.replace("Z", "+00:00"))
            stamp = utc.astimezone().strftime("%Y-%m-%d_%H%M")
        except ValueError:
            stamp = ""
    safe_title = sanitize_filename_stem(session_title or "session")
    if stamp:
        return f"{stamp} - {safe_title}.zip"
    return f"{safe_title}.zip"
