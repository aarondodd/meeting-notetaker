"""QThread that runs Notion / Confluence export off the UI thread (#79).

Image uploads + page creation each block on network; running them
inline freezes the app. The worker emits ``progress`` strings during
the run + a final ``finished`` carrying the ExportResult or the
exception.

Lives outside ui/ so the integrations package owns its own
threading; SessionView just constructs it and connects to the
signals.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal

from .export import (
    ExportAttachment,
    ExportResult,
    export_to_confluence,
    export_to_notion,
)
from .obsidian_export import (
    ObsidianPublishOptions,
    ObsidianSessionInfo,
    export_to_obsidian,
)


class _ExportWorkerBase(QThread):
    progress = pyqtSignal(str)
    succeeded = pyqtSignal(object)  # ExportResult
    failed = pyqtSignal(str)

    def _emit_progress(self, msg: str) -> None:
        self.progress.emit(msg)


class NotionExportWorker(_ExportWorkerBase):
    def __init__(
        self,
        *,
        client,
        parent_id: str,
        title: str,
        markdown_body: str,
        session_dir: Path,
        attachments: Optional[list[ExportAttachment]] = None,
        number_headings: bool = False,
        include_toc: bool = False,
        toc_max_depth: int = 3,
    ) -> None:
        super().__init__()
        self._client = client
        self._parent_id = parent_id
        self._title = title
        self._markdown_body = markdown_body
        self._session_dir = session_dir
        self._attachments = attachments or []
        self._number_headings = number_headings
        self._include_toc = include_toc
        self._toc_max_depth = toc_max_depth

    def run(self) -> None:
        try:
            result = export_to_notion(
                client=self._client,
                parent_id=self._parent_id,
                title=self._title,
                markdown_body=self._markdown_body,
                session_dir=self._session_dir,
                attachments=self._attachments,
                progress=self._emit_progress,
                number_headings=self._number_headings,
                include_toc=self._include_toc,
                toc_max_depth=self._toc_max_depth,
            )
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(result)


class ObsidianExportWorker(_ExportWorkerBase):
    """Issue #96. Filesystem write runs fast but the worker exists
    for consistency with the other export paths + so the progress
    dialog has a signal to subscribe to."""

    def __init__(
        self,
        *,
        session: ObsidianSessionInfo,
        body: str,
        options: ObsidianPublishOptions,
        session_dir: Path,
        attachments: Optional[list[ExportAttachment]] = None,
        location_template_name: str = "year_month",
        location_template_custom: str = "",
    ) -> None:
        super().__init__()
        self._session = session
        self._body = body
        self._options = options
        self._session_dir = session_dir
        self._attachments = attachments or []
        self._location_template_name = location_template_name
        self._location_template_custom = location_template_custom

    def run(self) -> None:
        try:
            result = export_to_obsidian(
                session=self._session,
                body=self._body,
                options=self._options,
                session_dir=self._session_dir,
                attachments=self._attachments,
                progress=self._emit_progress,
                location_template_name=self._location_template_name,
                location_template_custom=self._location_template_custom,
            )
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(result)


class ConfluenceExportWorker(_ExportWorkerBase):
    def __init__(
        self,
        *,
        client,
        parent_id: str,
        space_id: str,
        title: str,
        markdown_body: str,
        session_dir: Path,
        attachments: Optional[list[ExportAttachment]] = None,
        number_headings: bool = False,
        include_toc: bool = False,
        toc_max_depth: int = 3,
    ) -> None:
        super().__init__()
        self._client = client
        self._parent_id = parent_id
        self._space_id = space_id
        self._title = title
        self._markdown_body = markdown_body
        self._session_dir = session_dir
        self._attachments = attachments or []
        self._number_headings = number_headings
        self._include_toc = include_toc
        self._toc_max_depth = toc_max_depth

    def run(self) -> None:
        try:
            result = export_to_confluence(
                client=self._client,
                parent_id=self._parent_id,
                space_id=self._space_id,
                title=self._title,
                markdown_body=self._markdown_body,
                session_dir=self._session_dir,
                attachments=self._attachments,
                progress=self._emit_progress,
                number_headings=self._number_headings,
                include_toc=self._include_toc,
                toc_max_depth=self._toc_max_depth,
            )
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(result)
