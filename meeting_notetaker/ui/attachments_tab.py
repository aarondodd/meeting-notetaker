"""Attachments tab -- per-session file attach + preview surface.

Layout (matches issue #29 spec):

    +----------------------+----------------------+
    |  [Add file...]  drop |                      |
    |       hint           |                      |
    +----------------------+   Preview pane       |
    |                      |  (selected file)     |
    |  Attachment list     |                      |
    |  (selectable rows)   |                      |
    |                      |                      |
    +----------------------+----------------------+

Left side: vertical QSplitter where the top pane is fixed-height
(the Add button + drop hint) and the bottom pane (the list)
consumes the remaining vertical space when the window resizes.
Outer horizontal QSplitter divides left (list) from right
(preview).

Drag-drop accepts file URLs anywhere on the tab; the visible drop
hint is just an affordance. Right-clicking any list row offers
Delete / Rename / Save as / Open externally. Single-click previews;
double-click opens externally.

Mutations call AttachmentsStore directly + refresh the list. The
parent session_view is notified via signal so MainApp can re-paint
any indicators that care.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..models.attachments import (
    AttachmentRecord,
    AttachmentsStore,
    SOURCE_DROP,
    SOURCE_MANUAL,
)
from ..utils.office_preview import (
    convert_office_to_pdf,
    is_office_extension,
)
from .attachment_preview import AttachmentPreview


class AttachmentsTab(QWidget):
    """The full Attachments-tab surface."""

    # Emitted when the user adds, deletes, or renames anything so
    # MainApp can refresh any dependent indicators (e.g. a session-
    # list badge for "this session has attachments"). Payload is the
    # current session_id so the parent can ignore stale signals.
    attachments_changed = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._session_id: str = ""
        self._store: Optional[AttachmentsStore] = None
        self.setAcceptDrops(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._outer_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._outer_splitter.setChildrenCollapsible(False)
        outer.addWidget(self._outer_splitter, 1)

        # ---- left side: vertical splitter (controls / list) ----
        left = QSplitter(Qt.Orientation.Vertical, self._outer_splitter)
        left.setChildrenCollapsible(False)

        # Top: Add button + drop hint, fixed-ish height.
        top_widget = QFrame(left)
        top_widget.setFrameShape(QFrame.Shape.StyledPanel)
        top_widget.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed,
        )
        top_layout = QVBoxLayout(top_widget)
        top_layout.setContentsMargins(6, 6, 6, 6)
        top_layout.setSpacing(4)
        self._add_btn = QPushButton("Add file...", top_widget)
        self._add_btn.clicked.connect(self._on_add_files)
        top_layout.addWidget(self._add_btn)
        drop_hint = QLabel(
            "Or drop files anywhere on this tab. Sources are copied; "
            "the original is left in place.",
            top_widget,
        )
        drop_hint.setWordWrap(True)
        drop_hint.setStyleSheet("color: gray;")
        top_layout.addWidget(drop_hint)

        # Bottom: list of attachments.
        bottom_widget = QWidget(left)
        bottom_layout = QVBoxLayout(bottom_widget)
        bottom_layout.setContentsMargins(0, 4, 0, 0)
        bottom_layout.setSpacing(0)
        self._list = QListWidget(bottom_widget)
        self._list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection,
        )
        self._list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu,
        )
        self._list.customContextMenuRequested.connect(self._on_context_menu)
        self._list.itemSelectionChanged.connect(self._on_selection_changed)
        self._list.itemDoubleClicked.connect(self._on_open_externally)
        bottom_layout.addWidget(self._list, 1)

        left.addWidget(top_widget)
        left.addWidget(bottom_widget)
        # The top pane sticks at its sizeHint; only the list expands
        # when the user resizes the window vertically.
        left.setStretchFactor(0, 0)
        left.setStretchFactor(1, 1)
        # Give the controls a reasonable starting size.
        left.setSizes([90, 400])

        # ---- right side: preview ----
        self._preview = AttachmentPreview(self._outer_splitter)

        self._outer_splitter.addWidget(left)
        self._outer_splitter.addWidget(self._preview)
        self._outer_splitter.setStretchFactor(0, 0)
        self._outer_splitter.setStretchFactor(1, 1)
        self._outer_splitter.setSizes([300, 600])

        # Empty state.
        self.set_session(None)

    # ---- public API ----
    def set_session(self, session_id: Optional[str]) -> None:
        """Switch the tab to display the given session's attachments.

        None clears + disables the tab (no session loaded yet).
        """
        self._session_id = session_id or ""
        if not self._session_id:
            self._store = None
            self._add_btn.setEnabled(False)
            self._list.clear()
            self._preview.clear()
            return
        self._add_btn.setEnabled(True)
        try:
            self._store = AttachmentsStore(self._session_id)
        except Exception:
            self._store = None
        self._refresh_list()

    def import_files(
        self,
        paths: list[Path],
        *,
        source: str = SOURCE_MANUAL,
    ) -> int:
        """Add multiple files in one batch + refresh.

        v0.7.0 UX tweak: when any of the files is an Office
        document, the conversion-to-PDF preview is pre-generated
        immediately (rather than on the user's first preview click)
        so the preview pane is ready when they click. A modal
        progress dialog shows "Attaching..." with an animated
        progress bar while this runs.

        Non-Office files are processed synchronously and quickly
        (just a file copy); the dialog appears briefly and closes.
        Office files extend the wait for the COM round-trip.
        """
        if self._store is None:
            return 0
        clean_paths = [Path(p) for p in paths if Path(p).exists()]
        if not clean_paths:
            return 0
        # Run the import on a worker thread with a modal progress
        # dialog so the user has feedback during Office conversion
        # (which can take 1-2s per file on a cold Word/Excel start).
        worker = _AttachmentImportWorker(
            self._session_id, clean_paths, source,
        )
        progress = QProgressDialog(
            self._attaching_message(clean_paths),
            None, 0, 100, self,
        )
        progress.setWindowTitle("Attaching")
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setCancelButton(None)
        progress.setValue(0)

        results: dict[str, object] = {"added": 0}

        def _on_progress(pct: int) -> None:
            progress.setValue(pct)

        def _on_done(added: int) -> None:
            results["added"] = added
            progress.close()
            worker.deleteLater()

        worker.progress_changed.connect(_on_progress)
        worker.finished_with_count.connect(_on_done)
        progress.show()
        worker.start()
        # Block on the QProgressDialog by spinning the event loop.
        # exec() returns when progress.close() is called in
        # _on_done. The dialog has no Cancel button so this is a
        # determinate wait.
        progress.exec()
        added = int(results["added"])
        if added:
            self._refresh_list()
            self.attachments_changed.emit(self._session_id)
        return added

    @staticmethod
    def _attaching_message(paths: list[Path]) -> str:
        if len(paths) == 1:
            return f"Attaching {paths[0].name}..."
        return f"Attaching {len(paths)} files..."

    def splitter_state_base64(self) -> str:
        """Serialize the outer splitter state so MainApp can persist
        the user's preferred horizontal split between launches.
        Mirrors the pattern in main_window save/restore."""
        import base64
        try:
            return base64.b64encode(
                bytes(self._outer_splitter.saveState())
            ).decode("ascii")
        except Exception:
            return ""

    def restore_splitter_state_base64(self, payload: str) -> None:
        if not payload:
            return
        import base64
        from PyQt6.QtCore import QByteArray
        try:
            self._outer_splitter.restoreState(
                QByteArray(base64.b64decode(payload))
            )
        except Exception:
            pass

    # ---- drag-drop ----
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if not self._store:
            event.ignore()
            return
        mime = event.mimeData()
        if mime is not None and mime.hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        mime = event.mimeData()
        if mime is None or not mime.hasUrls():
            event.ignore()
            return
        paths: list[Path] = []
        for url in mime.urls():
            if url.isLocalFile():
                p = Path(url.toLocalFile())
                if p.exists() and p.is_file():
                    paths.append(p)
        event.acceptProposedAction()
        if paths:
            self.import_files(paths, source=SOURCE_DROP)

    # ---- internals ----
    def _refresh_list(self) -> None:
        prior_id = self._selected_id()
        self._list.clear()
        if self._store is None:
            self._preview.clear()
            return
        records = self._store.list()
        for rec in records:
            text = f"{rec.display_name}    -    {_format_size(rec.size)}"
            item = QListWidgetItem(text, self._list)
            item.setData(Qt.ItemDataRole.UserRole, rec.id)
            item.setToolTip(
                f"{rec.display_name}\n{rec.mime or 'unknown type'}\n"
                f"added {rec.added_at}"
            )
        if records:
            # Reselect prior if it's still there; else select first.
            if prior_id:
                for i in range(self._list.count()):
                    if self._list.item(i).data(Qt.ItemDataRole.UserRole) == prior_id:
                        self._list.setCurrentRow(i)
                        return
            self._list.setCurrentRow(0)
        else:
            self._preview.clear()

    def _selected_id(self) -> Optional[str]:
        item = self._list.currentItem()
        if item is None:
            return None
        data = item.data(Qt.ItemDataRole.UserRole)
        return str(data) if data else None

    def _selected_record(self) -> Optional[AttachmentRecord]:
        if self._store is None:
            return None
        att_id = self._selected_id()
        return self._store.get(att_id) if att_id else None

    def _on_selection_changed(self) -> None:
        if self._store is None:
            return
        att_id = self._selected_id()
        if not att_id:
            self._preview.clear()
            return
        path = self._store.file_path(att_id)
        if path is not None and path.exists():
            self._preview.load_file(path)

    def _on_add_files(self) -> None:
        if self._store is None:
            return
        paths_str, _filter = QFileDialog.getOpenFileNames(
            self, "Add attachment(s)", "",
            "All files (*.*)",
        )
        if not paths_str:
            return
        self.import_files([Path(p) for p in paths_str], source=SOURCE_MANUAL)

    def _on_context_menu(self, pos) -> None:
        rec = self._selected_record()
        if rec is None:
            return
        menu = QMenu(self._list)
        rename_action = menu.addAction("Rename...")
        save_as_action = menu.addAction("Save as...")
        open_external_action = menu.addAction("Open externally")
        menu.addSeparator()
        delete_action = menu.addAction("Delete...")
        action = menu.exec(self._list.viewport().mapToGlobal(pos))
        if action is rename_action:
            self._on_rename(rec)
        elif action is save_as_action:
            self._on_save_as(rec)
        elif action is open_external_action:
            self._on_open_externally()
        elif action is delete_action:
            self._on_delete(rec)

    def _on_rename(self, rec: AttachmentRecord) -> None:
        new_name, ok = QInputDialog.getText(
            self, "Rename attachment",
            "Display name:",
            QLineEdit.EchoMode.Normal,
            rec.display_name,
        )
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == rec.display_name:
            return
        try:
            self._store.rename(rec.id, new_name)
        except Exception as exc:
            QMessageBox.warning(self, "Rename attachment", str(exc))
            return
        self._refresh_list()
        self.attachments_changed.emit(self._session_id)

    def _on_save_as(self, rec: AttachmentRecord) -> None:
        # Suggest the display name; user picks destination.
        suggested = rec.display_name or rec.stored_name
        dst_str, _filter = QFileDialog.getSaveFileName(
            self, "Save attachment as", suggested,
            "All files (*.*)",
        )
        if not dst_str:
            return
        try:
            self._store.save_as(rec.id, Path(dst_str))
        except Exception as exc:
            QMessageBox.warning(self, "Save attachment", str(exc))

    def _on_open_externally(self, _item: Optional[QListWidgetItem] = None) -> None:
        rec = self._selected_record()
        if rec is None or self._store is None:
            return
        path = self._store.file_path(rec.id)
        if path is None or not path.exists():
            QMessageBox.warning(
                self, "Open externally",
                "The attachment file is missing from disk.",
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _on_delete(self, rec: AttachmentRecord) -> None:
        confirm = QMessageBox.question(
            self, "Delete attachment",
            f"Delete \"{rec.display_name}\" from this session? "
            "The file is removed from disk; the session's "
            "transcripts and notes are untouched.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        if self._store is None:
            return
        # Clear the preview first so the right pane doesn't keep
        # showing a now-deleted file. The actual file-handle release
        # is handled by PdfPreviewWidget loading from an in-memory
        # buffer rather than a file path -- without that, Windows
        # refused the unlink with PermissionError [WinError 32].
        self._preview.clear()
        self._store.delete(rec.id)
        self._refresh_list()
        self.attachments_changed.emit(self._session_id)


class _AttachmentImportWorker(QThread):
    """Off-UI-thread copy + Office-preview pre-generation.

    Walks the source paths, copies each into the AttachmentsStore,
    then for Office types runs convert_office_to_pdf so the cache
    is warm when the user clicks the preview. Reports cumulative
    progress (0..100) across the batch.

    Progress weighting: copy is fast vs. conversion, so we treat
    the copy as 20% of each file's slot and the optional Office
    conversion as 80%. Files that aren't Office finish their slot
    at 20% instantly (the worker fast-forwards the remaining 80%).
    """

    progress_changed = pyqtSignal(int)
    finished_with_count = pyqtSignal(int)

    def __init__(
        self, session_id: str, paths: list[Path], source: str,
    ) -> None:
        super().__init__()
        self._session_id = session_id
        self._paths = list(paths)
        self._source = source

    def run(self) -> None:  # type: ignore[override]
        store = AttachmentsStore(self._session_id)
        added = 0
        total = len(self._paths)
        if total == 0:
            self.progress_changed.emit(100)
            self.finished_with_count.emit(0)
            return
        for idx, p in enumerate(self._paths):
            slot_start = int(idx * 100 / total)
            slot_end = int((idx + 1) * 100 / total)
            # 20% of the slot for the file copy.
            copy_target = slot_start + int((slot_end - slot_start) * 0.2)
            try:
                rec = store.add_file(p, source=self._source)
                added += 1
            except (FileNotFoundError, ValueError):
                # Skip; advance progress so the bar still moves.
                self.progress_changed.emit(slot_end)
                continue
            self.progress_changed.emit(copy_target)
            # If Office, run the conversion now so the preview cache
            # is warm by the time the user clicks the file.
            # CRITICAL: convert the STORED path (inside
            # attachments_dir), not the source path. The cache key
            # is (path, mtime, size); the preview pane will look up
            # by the stored path on click, so converting the
            # source-side path doesn't warm the right cache entry.
            ext = p.suffix.lower().lstrip(".")
            if is_office_extension(ext):
                try:
                    stored_path = store.file_path(rec.id)
                    if stored_path is not None:
                        convert_office_to_pdf(stored_path)
                except Exception:
                    pass
            self.progress_changed.emit(slot_end)
        # Always end at 100 so the dialog can autodismiss cleanly.
        self.progress_changed.emit(100)
        self.finished_with_count.emit(added)


def _format_size(n: int) -> str:
    nf = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if nf < 1024:
            return f"{nf:.0f} {unit}"
        nf /= 1024
    return f"{nf:.0f} TB"
