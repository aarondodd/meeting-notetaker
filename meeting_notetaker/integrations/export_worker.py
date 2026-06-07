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
        skip_h1: bool = False,
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
        self._skip_h1 = skip_h1
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
                skip_h1=self._skip_h1,
                toc_max_depth=self._toc_max_depth,
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
        skip_h1: bool = False,
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
        self._skip_h1 = skip_h1
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
                skip_h1=self._skip_h1,
                toc_max_depth=self._toc_max_depth,
            )
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(result)
