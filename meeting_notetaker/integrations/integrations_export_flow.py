"""Glue between MainApp and the Notion + Confluence export workers (#79).

Owns the picker dialog -> worker -> result-handler choreography so
``app.py`` stays thin. Each ``run_*_export`` function:

  1. Builds the client + browser from the live config.
  2. Opens IntegrationsPickerDialog seeded with current favorites +
     recents.
  3. On accept: kicks off the matching worker QThread with a small
     progress dialog wired to the worker's progress signal.
  4. On worker success: updates favorites/recents, persists config,
     opens the page URL in the default browser, status-bar toast.
  5. On worker failure: error dialog with the upstream message.

Pulled out of app.py so the imports of integration modules + Qt
dialog plumbing live in one place rather than scattered across
MainApp's massive class body.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QMessageBox, QProgressDialog

from ..models.attachments import AttachmentsStore
from ..models.transcript import TranscriptStore
from ..utils.live_notes import parse_attendees
from ..utils.paths import session_dir
from .confluence_api import ConfluenceClient
from .export import ExportAttachment
from .export_worker import (
    ConfluenceExportWorker,
    NotionExportWorker,
    ObsidianExportWorker,
    OneNoteExportWorker,
)
from .notion_api import NotionClient
from .obsidian_export import (
    ObsidianSessionInfo,
    find_republish_candidate,
    resolve_filename_template,
    resolve_location_template,
    sanitize_obsidian_path_segment,
    sanitize_obsidian_stem,
)
from .obsidian_vault import (
    is_vault_registered,
    vault_is_valid,
    vault_name_for_path,
)
from .onenote_com import OneNoteClient, OneNoteUnavailable
from ..ui.integrations_picker_dialog import (
    ConfluencePickerBrowser,
    IntegrationsPickerDialog,
    NotionPickerBrowser,
    OneNotePickerBrowser,
)
from ..ui.obsidian_picker_dialog import ObsidianSavePicker


if TYPE_CHECKING:  # pragma: no cover -- type-check only
    from ..app import MainApp


_RECENTS_LIMIT = 5


# ---- public entry points --------------------------------------------------


def run_notion_export(
    main_app: "MainApp", *, session_id: str, tab_label: str, body: str,
) -> None:
    cfg = main_app.config.notion
    if not (cfg.api_token and cfg.last_verified_at):
        QMessageBox.information(
            main_app.window, "Save to Notion",
            "Verify the Notion connection in Settings -> Integrations first.",
        )
        return
    client = NotionClient(cfg.api_token)
    browser = NotionPickerBrowser(client)
    session_attachments = _load_session_attachments(session_id)
    dlg = IntegrationsPickerDialog(
        title="Save to Notion",
        browser=browser,
        favorites=cfg.favorites,
        recents=cfg.recents,
        default_page_title=_default_page_title(main_app, session_id),
        series_name=_session_series_name(main_app, session_id),
        attachment_count=len(session_attachments),
        parent=main_app.window,
    )
    if dlg.exec() != dlg.DialogCode.Accepted:
        return
    selection = dlg.selection()
    if selection is None:
        return
    # Persist updated favorites immediately so a star action is
    # durable even if the export itself fails.
    main_app.config.notion.favorites = dlg.updated_favorites()

    worker = NotionExportWorker(
        client=client,
        parent_id=selection.id,
        title=selection.page_title,
        markdown_body=body,
        session_dir=session_dir(session_id),
        attachments=(
            session_attachments if selection.include_attachments else []
        ),
        number_headings=getattr(
            main_app.config.synthesis, "heading_numbering", False,
        ),
        include_toc=getattr(
            main_app.config.synthesis, "toc_in_exports", False,
        ),
        toc_max_depth=int(getattr(
            main_app.config.synthesis, "toc_max_depth", 3,
        ) or 3),
    )
    del tab_label  # picker title carries the user's wording now
    _run_worker_with_progress(
        main_app, worker,
        progress_title="Exporting to Notion",
        cancel_label="Cancel",
        target="notion",
        selection_id=selection.id,
        selection_title=selection.title,
        selection_extra=selection.extra,
    )


def run_confluence_export(
    main_app: "MainApp", *, session_id: str, tab_label: str, body: str,
) -> None:
    cfg = main_app.config.confluence
    if not (cfg.base_url and cfg.email and cfg.api_token and cfg.last_verified_at):
        QMessageBox.information(
            main_app.window, "Save to Confluence",
            "Verify the Confluence connection in Settings -> Integrations first.",
        )
        return
    client = ConfluenceClient(cfg.base_url, cfg.email, cfg.api_token)
    browser = ConfluencePickerBrowser(client)
    session_attachments = _load_session_attachments(session_id)
    dlg = IntegrationsPickerDialog(
        title="Save to Confluence",
        browser=browser,
        favorites=cfg.favorites,
        recents=cfg.recents,
        default_page_title=_default_page_title(main_app, session_id),
        series_name=_session_series_name(main_app, session_id),
        attachment_count=len(session_attachments),
        parent=main_app.window,
    )
    if dlg.exec() != dlg.DialogCode.Accepted:
        return
    selection = dlg.selection()
    if selection is None:
        return
    main_app.config.confluence.favorites = dlg.updated_favorites()

    space_id = selection.extra.get("space_id") or ""
    if not space_id:
        QMessageBox.warning(
            main_app.window, "Save to Confluence",
            "The picked parent has no associated space; pick a page inside a space.",
        )
        return

    worker = ConfluenceExportWorker(
        client=client,
        parent_id=selection.id,
        space_id=space_id,
        title=selection.page_title,
        markdown_body=body,
        session_dir=session_dir(session_id),
        attachments=(
            session_attachments if selection.include_attachments else []
        ),
        number_headings=getattr(
            main_app.config.synthesis, "heading_numbering", False,
        ),
        include_toc=getattr(
            main_app.config.synthesis, "toc_in_exports", False,
        ),
        toc_max_depth=int(getattr(
            main_app.config.synthesis, "toc_max_depth", 3,
        ) or 3),
    )
    del tab_label  # picker title carries the user's wording now
    _run_worker_with_progress(
        main_app, worker,
        progress_title="Exporting to Confluence",
        cancel_label="Cancel",
        target="confluence",
        selection_id=selection.id,
        selection_title=selection.title,
        selection_extra=selection.extra,
    )


def run_onenote_export(
    main_app: "MainApp", *, session_id: str, tab_label: str, body: str,
) -> None:
    cfg = main_app.config.onenote
    if not (cfg.enabled and cfg.last_verified_at):
        QMessageBox.information(
            main_app.window, "Save to OneNote",
            "Enable OneNote + click Verify in Settings -> Integrations first.",
        )
        return
    try:
        client = OneNoteClient()
    except OneNoteUnavailable as exc:
        QMessageBox.warning(
            main_app.window, "Save to OneNote",
            f"OneNote is not reachable via COM:\n\n{exc}",
        )
        return
    browser = OneNotePickerBrowser(client)
    session_attachments = _load_session_attachments(session_id)
    dlg = IntegrationsPickerDialog(
        title="Save to OneNote",
        browser=browser,
        favorites=cfg.favorites,
        recents=cfg.recents,
        default_page_title=_default_page_title(main_app, session_id),
        series_name=_session_series_name(main_app, session_id),
        attachment_count=len(session_attachments),
        parent=main_app.window,
    )
    if dlg.exec() != dlg.DialogCode.Accepted:
        return
    selection = dlg.selection()
    if selection is None:
        return
    main_app.config.onenote.favorites = dlg.updated_favorites()

    # Picker pre-checks for `default_include_attachments`; the dialog
    # doesn't (yet) take a default-checked parameter, so we honor the
    # config setting at this layer: if the user opted in once, pass
    # the attachments even when they didn't tick the box.
    include_attachments = (
        selection.include_attachments
        or cfg.default_include_attachments
    )

    worker = OneNoteExportWorker(
        client=client,
        section_id=selection.id,
        title=selection.page_title,
        markdown_body=body,
        session_dir=session_dir(session_id),
        attachments=(
            session_attachments if include_attachments else []
        ),
        number_headings=getattr(
            main_app.config.synthesis, "heading_numbering", False,
        ),
        open_after_save=cfg.open_after_save,
    )
    del tab_label
    _run_worker_with_progress(
        main_app, worker,
        progress_title="Saving to OneNote",
        cancel_label="Cancel",
        target="onenote",
        selection_id=selection.id,
        selection_title=selection.title,
        selection_extra=selection.extra,
    )


def run_obsidian_export(
    main_app: "MainApp", *, session_id: str, tab_label: str, body: str,
) -> None:
    cfg = main_app.config.obsidian
    vault_root = Path(cfg.vault_root) if cfg.vault_root else None
    if vault_root is None or not vault_is_valid(vault_root):
        QMessageBox.information(
            main_app.window, "Save to Obsidian",
            "Set the Obsidian vault path in Settings -> Integrations first.",
        )
        return
    info = _obsidian_session_info(main_app, session_id)
    session_attachments = _load_session_attachments(session_id)
    when = info.started_at
    default_subdir = resolve_location_template(
        template_name=cfg.location_template_name,
        template_custom=cfg.location_template_custom,
        session=info,
        when=when,
    )
    default_stem = sanitize_obsidian_stem(
        resolve_filename_template(
            template_name=cfg.location_template_name,
            template_custom=cfg.location_template_custom,
            session=info,
            when=when,
        ),
        fallback=session_id,
    )

    dlg = ObsidianSavePicker(
        vault_root=vault_root,
        vault_name=cfg.vault_name or vault_name_for_path(vault_root),
        default_subdir=default_subdir,
        default_filename_stem=default_stem,
        attachment_count=len(session_attachments),
        session=info,
        write_frontmatter=cfg.write_frontmatter,
        wikilink_attendees=cfg.wikilink_attendees,
        wikilink_series=cfg.wikilink_series,
        include_classification=cfg.include_classification,
        daily_note_backlink=cfg.daily_note_backlink,
        open_after_save=cfg.open_after_save,
        default_include_attachments=cfg.default_include_attachments,
        parent=main_app.window,
    )
    if dlg.exec() != dlg.DialogCode.Accepted:
        return
    options = dlg.result_options()
    options.vault_name = cfg.vault_name or vault_name_for_path(vault_root)

    # Re-publish detection. Scan the vault for any md whose
    # source_session_id matches; surface Overwrite / Save as new
    # / Cancel before kicking off the worker.
    candidate = find_republish_candidate(
        search_root=vault_root, session_id=session_id,
    )
    if candidate is not None:
        choice = _ask_republish_choice(
            main_app, candidate.existing_path.relative_to(vault_root),
        )
        if choice == "cancel":
            return
        options.on_conflict = choice
        if choice == "overwrite":
            options.target_subdir = str(
                candidate.existing_path.parent.relative_to(vault_root),
            ).replace("\\", "/")
            options.filename_stem = candidate.existing_path.stem

    sanitized_subdir = sanitize_obsidian_path_segment(
        options.target_subdir, fallback="Meetings",
    ) if options.target_subdir else ""
    options.target_subdir = sanitized_subdir

    worker = ObsidianExportWorker(
        session=info,
        body=body,
        options=options,
        session_dir=session_dir(session_id),
        attachments=(
            session_attachments if options.include_attachments else []
        ),
        location_template_name=cfg.location_template_name,
        location_template_custom=cfg.location_template_custom,
    )
    del tab_label
    _run_worker_with_progress(
        main_app, worker,
        progress_title="Saving to Obsidian",
        cancel_label="Cancel",
        target="obsidian",
        selection_id=session_id,
        selection_title=options.filename_stem,
        selection_extra={
            "vault_name": options.vault_name,
            "vault_root": str(vault_root),
            "open_after_save": options.open_after_save,
        },
    )


def _ask_republish_choice(
    main_app: "MainApp", existing_rel: Path,
) -> str:
    box = QMessageBox(main_app.window)
    box.setWindowTitle("Save to Obsidian")
    box.setIcon(QMessageBox.Icon.Question)
    box.setText(
        f"This session has already been saved to:\n\n  {existing_rel}\n\n"
        "What would you like to do?"
    )
    overwrite = box.addButton("Overwrite", QMessageBox.ButtonRole.AcceptRole)
    save_new = box.addButton("Save as new", QMessageBox.ButtonRole.AcceptRole)
    cancel = box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(overwrite)
    box.exec()
    clicked = box.clickedButton()
    if clicked is overwrite:
        return "overwrite"
    if clicked is save_new:
        return "save_as_new"
    del cancel
    return "cancel"


def _obsidian_session_info(
    main_app: "MainApp", session_id: str,
) -> ObsidianSessionInfo:
    session = main_app.store.get_session(session_id)
    title = (session.title if session else session_id) or session_id
    started_at = None
    duration_minutes = None
    if session and session.created_at:
        try:
            started_at = datetime.fromisoformat(
                session.created_at.replace("Z", "+00:00")
            ).astimezone()
        except ValueError:
            started_at = None
    if session and session.duration_seconds:
        duration_minutes = max(1, session.duration_seconds // 60)
    attendees: list[str] = []
    try:
        live_body = TranscriptStore(session_id).read_live_notes()
        attendees = parse_attendees(live_body)
    except Exception:
        attendees = []
    series_name = ""
    classification_name = ""
    topic_names: list[str] = []
    store = getattr(main_app, "classification", None)
    if store is not None:
        try:
            classification = store.classification_for_session(session_id)
            if classification.series:
                series_name = classification.series.name or ""
            topic_names = [
                t.topic.name for t in classification.topics if t.topic.name
            ]
        except Exception:
            pass
    return ObsidianSessionInfo(
        session_id=session_id,
        title=title,
        started_at=started_at,
        duration_minutes=duration_minutes,
        attendees=attendees,
        series=series_name,
        tags=topic_names,
        classification=classification_name,
    )


# ---- helpers --------------------------------------------------------------


def _default_page_title(main_app: "MainApp", session_id: str) -> str:
    """Default the picker's title field to
    'YYYY-MM-DD HH:MM - <session title>' (#79 polish, Aaron's
    2026-06-03 ask). The date+time is the session's stored UTC
    converted to the user's local timezone so it lines up with
    what the rest of the app shows."""
    session = main_app.store.get_session(session_id)
    title = (session.title if session else session_id) or session_id
    when_str = ""
    if session and session.created_at:
        try:
            local = datetime.fromisoformat(
                session.created_at.replace("Z", "+00:00")
            ).astimezone()
            when_str = local.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            when_str = ""
    if when_str:
        return f"{when_str} - {title}"
    return title


def _load_session_attachments(session_id: str) -> list[ExportAttachment]:
    """Return the session's tracked files as ExportAttachment records,
    skipping any whose on-disk file is missing. The picker uses the
    count to decide whether to surface the Include-attachments
    checkbox; the worker uses the list when the user opts in."""
    try:
        records = AttachmentsStore(session_id).list()
    except Exception:
        return []
    out: list[ExportAttachment] = []
    for rec in records:
        path = session_dir(session_id) / "attachments" / rec.stored_name
        if not path.is_file():
            continue
        out.append(ExportAttachment(
            path=path,
            display_name=rec.display_name or rec.stored_name,
            mime=rec.mime or "",
        ))
    return out


def _session_series_name(main_app: "MainApp", session_id: str) -> str:
    """Look up the session's series name for the Create-folder
    sub-dialog's 'Use series name' button. Empty when no series is
    set or the classification store isn't wired up."""
    store = getattr(main_app, "classification", None)
    if store is None:
        return ""
    try:
        series = store.series_for_session(session_id)
    except Exception:
        return ""
    return (series.name or "") if series else ""


def _build_export_title(
    main_app: "MainApp", session_id: str, tab_label: str,
) -> str:
    """Legacy helper retained for tests that pinned the old
    '<session title> -- <tab>' shape. New call sites should use
    `_default_page_title` (the picker title field is now the source
    of truth for the page name)."""
    session = main_app.store.get_session(session_id)
    base = (session.title if session else session_id) or session_id
    if tab_label:
        return f"{base} -- {tab_label}"
    return base


def _run_worker_with_progress(
    main_app: "MainApp",
    worker,
    *,
    progress_title: str,
    cancel_label: str,
    target: str,
    selection_id: str,
    selection_title: str,
    selection_extra: dict,
) -> None:
    progress = QProgressDialog(
        "Preparing...", cancel_label, 0, 0, main_app.window,
    )
    progress.setWindowTitle(progress_title)
    progress.setMinimumDuration(0)
    progress.setAutoClose(False)
    progress.setAutoReset(False)

    worker.progress.connect(lambda msg: progress.setLabelText(msg))
    worker.succeeded.connect(
        lambda result: _on_worker_succeeded(
            main_app, target, result,
            selection_id=selection_id,
            selection_title=selection_title,
            selection_extra=selection_extra,
            progress=progress,
        )
    )
    worker.failed.connect(
        lambda err: _on_worker_failed(main_app, target, err, progress)
    )
    # Cancel doesn't actually abort in-flight requests (the requests
    # call is blocking); but it closes the dialog so the user isn't
    # stuck staring at it.
    progress.canceled.connect(progress.close)

    # Stash the worker on the MainApp so it isn't garbage-collected
    # mid-flight; we'll drop the reference in the result handlers.
    if not hasattr(main_app, "_active_integration_workers"):
        main_app._active_integration_workers = []
    main_app._active_integration_workers.append(worker)

    worker.start()
    progress.show()


def _on_worker_succeeded(
    main_app: "MainApp",
    target: str,
    result,
    *,
    selection_id: str,
    selection_title: str,
    selection_extra: dict,
    progress: QProgressDialog,
) -> None:
    progress.close()
    # Update recents (most recent first, dedup by id, trim).
    if target == "notion":
        cfg_section = main_app.config.notion
    elif target == "confluence":
        cfg_section = main_app.config.confluence
    elif target == "onenote":
        cfg_section = main_app.config.onenote
    else:
        cfg_section = main_app.config.obsidian
    cfg_section.recents = _push_recent(
        cfg_section.recents, selection_id, selection_title, selection_extra,
    )
    # Persist favorites + recents (favorites were updated in run_*_export).
    try:
        main_app.config.save()
    except Exception:
        # Persistence is best-effort here; the export itself succeeded.
        pass

    if target == "obsidian":
        # page_url is the obsidian:// URI; QDesktopServices delegates
        # to the OS handler (ShellExecute on Windows / xdg-open on
        # Linux / LSOpen on macOS) which dispatches to the installed
        # Obsidian client.
        if selection_extra.get("open_after_save") and result.page_url:
            QDesktopServices.openUrl(QUrl(result.page_url))
    elif target == "onenote":
        # The COM client already called NavigateTo when
        # open_after_save was set, so the OneNote window is on the
        # right page. If a clickable onenote:// hyperlink is
        # available we still openUrl it -- harmless redundancy that
        # gives the user a clickable reference in the toast.
        if result.page_url:
            QDesktopServices.openUrl(QUrl(result.page_url))
    elif result.page_url:
        # Open the new page in the user's default browser. Aaron's
        # requirement: trigger the user's default browser to open the
        # page exported.
        QDesktopServices.openUrl(QUrl(result.page_url))

    # Status-bar toast. The result.message text already calls out the
    # sibling-creation behavior so the user knows nothing was replaced.
    main_app.window.status(result.message, timeout_ms=6000)
    _drop_worker(main_app, target)


def _on_worker_failed(
    main_app: "MainApp", target: str, err: str, progress: QProgressDialog,
) -> None:
    progress.close()
    pretty = {
        "notion": "Notion",
        "confluence": "Confluence",
        "obsidian": "Obsidian",
        "onenote": "OneNote",
    }.get(target, target.title())
    QMessageBox.warning(
        main_app.window, f"Save to {pretty}",
        f"The save failed:\n\n{err}",
    )
    _drop_worker(main_app, target)




def _drop_worker(main_app: "MainApp", target: str) -> None:
    workers = getattr(main_app, "_active_integration_workers", [])
    main_app._active_integration_workers = [
        w for w in workers if not w.isFinished()
    ]


def _push_recent(
    existing: list[dict],
    page_id: str,
    page_title: str,
    extra: dict,
) -> list[dict]:
    when = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    new_entry = {
        "id": page_id,
        "title": page_title,
        "used_at": when,
    }
    if extra:
        new_entry["extra"] = dict(extra)
    deduped = [e for e in existing if e.get("id") != page_id]
    deduped.insert(0, new_entry)
    return deduped[:_RECENTS_LIMIT]
