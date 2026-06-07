"""Previous-notes browser: list of archived synthesis versions on the
left, markdown-rendered preview on the right.

Archived versions are ``notes-YYYYMMDD-HHMM.md`` files in the session
directory, written by ``TranscriptStore.save_notes(archive_existing=
True)`` each time a new synthesis lands (either via Paste Response
Back or Send-to-LLM automation). The user can:

* Click any version to preview it (markdown-rendered).
* Restore: replaces the current ``notes.md`` with the selected
  archive, automatically archiving the current version first so
  nothing is lost.
* Delete: permanently removes the archive file (with confirmation).

This widget owns no persistent state -- the session_view passes the
list of archive paths in via ``set_archives(paths)``, and the widget
emits signals for the user-requested actions.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .markdown_preview import MarkdownPreview


class PreviousNotesWidget(QWidget):
    """Replaces the Previous Notes tab's old plaintext dump."""

    # session_id, archive_path
    restore_requested = pyqtSignal(str, Path)
    delete_requested = pyqtSignal(str, Path)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._session_id: str = ""
        self._archives: list[Path] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setChildrenCollapsible(False)

        # Left pane: version list.
        left = QWidget(self)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)
        self._heading = QLabel("Archived versions", left)
        self._heading.setStyleSheet("font-weight: 600;")
        left_layout.addWidget(self._heading)
        self._list = QListWidget(left)
        self._list.itemSelectionChanged.connect(self._on_selection_changed)
        left_layout.addWidget(self._list, 1)
        # Action row beneath the list.
        action_row = QHBoxLayout()
        action_row.setSpacing(6)
        self._restore_btn = QPushButton("Restore to current", left)
        self._restore_btn.setToolTip(
            "Replace the Synthesis tab with the selected archive. The "
            "current Synthesis is archived first so nothing is lost."
        )
        self._restore_btn.setEnabled(False)
        self._restore_btn.clicked.connect(self._on_restore_clicked)
        action_row.addWidget(self._restore_btn)
        self._delete_btn = QPushButton("Delete", left)
        self._delete_btn.setToolTip(
            "Permanently delete the selected archive file. Asks for "
            "confirmation."
        )
        self._delete_btn.setEnabled(False)
        self._delete_btn.clicked.connect(self._on_delete_clicked)
        action_row.addWidget(self._delete_btn)
        action_row.addStretch(1)
        left_layout.addLayout(action_row)
        splitter.addWidget(left)

        # Right pane: markdown preview.
        right = QWidget(self)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)
        self._preview_heading = QLabel("Preview", right)
        self._preview_heading.setStyleSheet("font-weight: 600;")
        right_layout.addWidget(self._preview_heading)
        self._preview = MarkdownPreview(right)
        right_layout.addWidget(self._preview, 1)
        splitter.addWidget(right)

        # Sensible default split: 30/70 list/preview.
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([200, 600])

        outer.addWidget(splitter, 1)
        self._empty_state()

    # ------------------------------------------------------------------
    # Public API

    def set_session_id(self, session_id: str) -> None:
        self._session_id = session_id

    def find_target(self) -> QWidget:
        """Return the markdown preview pane so Ctrl+F can search it.

        The archive list on the left is short by design and uses Qt's
        own keyboard navigation; the right-side preview holds the
        actual body content the user wants to grep through.
        """
        return self._preview

    def apply_fonts(self, preview_font) -> None:
        """Push the resolved preview font to the inner preview pane.

        Read-only widget; no editor side. Called by SessionView's
        apply_fonts as part of the cross-surface font refresh."""
        self._preview.setFont(preview_font)

    def set_heading_numbering(self, enabled: bool) -> None:
        """Toggle preview heading numbering (#92) on the inner
        preview. Re-rendering the currently-displayed archive (if
        any) is handled by the caller's setMarkdown on the next
        archive selection -- which is the common path in this
        read-only viewer."""
        try:
            self._preview.set_heading_numbering(enabled)
        except AttributeError:
            pass

    def select_archive_by_name(self, archive_name: str) -> bool:
        """Pick the list row whose Path basename matches `archive_name`.

        Used by the cross-session search dialog when the user
        activates a Previous Notes hit -- we need to surface the
        specific archive that contained the match. Returns True when
        a match is selected, False otherwise (caller can fall back
        to whatever was previously selected).
        """
        for i, path in enumerate(self._archives):
            if path.name == archive_name:
                self._list.setCurrentRow(i)
                return True
        return False

    def set_archives(self, paths: list[Path]) -> None:
        """Populate the version list. Paths come from
        ``TranscriptStore.list_previous_notes()`` -- already sorted
        newest-first. Re-renders the list and selects the first item
        (most recent archive) so the preview pane has something."""
        # Preserve current selection by path if it still exists.
        prior_selected = self._selected_path()
        self._archives = list(paths)
        self._list.blockSignals(True)
        self._list.clear()
        for p in self._archives:
            item = QListWidgetItem(_pretty_label(p), self._list)
            item.setData(Qt.ItemDataRole.UserRole, str(p))
            item.setToolTip(str(p))
        self._list.blockSignals(False)
        if not self._archives:
            self._empty_state()
            return
        # Restore selection if the prior selected path is still in the
        # list; otherwise select the most recent.
        target_idx = 0
        if prior_selected is not None:
            for i, p in enumerate(self._archives):
                if p == prior_selected:
                    target_idx = i
                    break
        self._list.setCurrentRow(target_idx)
        self._restore_btn.setEnabled(True)
        self._delete_btn.setEnabled(True)

    # ------------------------------------------------------------------
    # Internal

    def _selected_path(self) -> Optional[Path]:
        item = self._list.currentItem()
        if item is None:
            return None
        path_str = item.data(Qt.ItemDataRole.UserRole)
        return Path(path_str) if path_str else None

    def _on_selection_changed(self) -> None:
        path = self._selected_path()
        if path is None:
            self._preview.clear()
            self._preview.setPlaceholderText("(no archive selected)")
            return
        try:
            body = path.read_text(encoding="utf-8")
        except OSError as exc:
            self._preview.setPlainText(f"(unreadable: {exc})")
            return
        # QTextBrowser.setMarkdown handles the common Markdown subset
        # well enough for synthesized meeting notes. For images and
        # embedded resources we don't try to be clever -- archives
        # rarely contain them.
        self._preview.setMarkdown(body)

    def _on_restore_clicked(self) -> None:
        path = self._selected_path()
        if path is None or not self._session_id:
            return
        confirm = QMessageBox.question(
            self,
            "Restore archived synthesis",
            "Replace the current Synthesis with this archive?\n\n"
            "The current Synthesis is archived first, so you can swap "
            "back if needed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self.restore_requested.emit(self._session_id, path)

    def _on_delete_clicked(self) -> None:
        path = self._selected_path()
        if path is None or not self._session_id:
            return
        confirm = QMessageBox.question(
            self,
            "Delete archived synthesis",
            f"Permanently delete this archive?\n\n{path.name}\n\n"
            "This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self.delete_requested.emit(self._session_id, path)

    def _empty_state(self) -> None:
        self._list.clear()
        self._preview.clear()
        # The session has no archived synthesis versions yet -- explain
        # what would put them here. The list stays empty until a new
        # synthesis lands and the prior one gets archived.
        self._preview.setMarkdown(
            "_No archived synthesis versions for this session yet._\n\n"
            "Every time a fresh synthesis lands -- either via "
            "Paste Response Back or Send to Claude.ai -- the prior "
            "Synthesis is archived here so you can compare or roll "
            "back."
        )
        self._restore_btn.setEnabled(False)
        self._delete_btn.setEnabled(False)


def _pretty_label(path: Path) -> str:
    """Render a notes-YYYYMMDD-HHMM.md filename as a human date+time.

    Falls back to the bare filename if the pattern doesn't match (older
    archives or files written by another path)."""
    name = path.name
    # Expected: notes-YYYYMMDD-HHMM.md
    if name.startswith("notes-") and name.endswith(".md"):
        stem = name[len("notes-") : -len(".md")]
        try:
            dt = datetime.strptime(stem, "%Y%m%d-%H%M")
            return dt.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
    return name
