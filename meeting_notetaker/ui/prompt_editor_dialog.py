"""In-app prompt editor (#89).

Replaces the "Open Prompts Folder" -> external editor workflow with a
dialog that lets the user:

  * Browse all available prompt templates in a left pane.
  * Edit the active body in a monospace editor (right pane).
  * Save -- archives the prior body to prompts/_archive/<name>/ so
    every save is reversible.
  * Revert -- pick a previous version from a per-prompt history list
    and restore it (the just-replaced body is itself archived).
  * New -- create a blank prompt or duplicate the currently-selected
    one as a starting point.
  * Delete -- remove a custom prompt (bundled prompts re-seed on
    next launch; the user is warned before deleting a bundled one).

The dialog is non-modal-friendly but used modally today via exec().
On close, the SessionView + Settings prompt pickers refresh from
list_templates() so the dropdown reflects the current set.

Variable hint panel surfaces the placeholders the render() function
substitutes ({{transcript}}, {{live_notes}}, etc.) so users editing
a template aren't guessing at the schema.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QFontDatabase, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..utils import prompts as prompts_mod


# Placeholder schema the editor surfaces in the variable-hint panel.
# Kept in sync with prompts.render() by convention; a regression
# test pins both sides agree.
PROMPT_PLACEHOLDERS: tuple[tuple[str, str], ...] = (
    ("{{session_title}}", "Session title from New Session dialog"),
    ("{{date}}", "Session date + time (YYYY-MM-DD HH:MM)"),
    ("{{transcript}}", "Full transcript body (timestamps + speaker labels)"),
    ("{{live_notes}}", "User's My Notes content; (none) when empty"),
    ("{{attendees}}", "Comma-separated attendee list parsed from live notes"),
    ("{{user_name}}", "Your configured name (or 'Me' when unset)"),
)


class PromptEditorDialog(QDialog):
    """Multi-prompt editor with archive + revert.

    Usage:

        dlg = PromptEditorDialog(parent=main_window)
        dlg.exec()
        # After close, refresh prompt pickers from list_templates().
    """

    def __init__(self, *, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Prompts")
        self.resize(900, 620)
        self._current_name: Optional[str] = None
        self._dirty: bool = False

        outer = QVBoxLayout(self)
        intro = QLabel(
            "Edit synthesis prompt templates. Saved changes archive the "
            "prior body so you can revert. The active body shows in the "
            "session view's prompt picker and in Settings.",
            self,
        )
        intro.setWordWrap(True)
        outer.addWidget(intro)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        outer.addWidget(splitter, stretch=1)

        # ---- Left pane: prompt list + actions ----
        left = QWidget(splitter)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("<b>Prompts</b>", left))
        self._prompt_list = QListWidget(left)
        self._prompt_list.currentItemChanged.connect(self._on_prompt_selected)
        left_layout.addWidget(self._prompt_list, stretch=1)
        list_btns = QHBoxLayout()
        self._new_btn = QPushButton("New...", left)
        self._new_btn.clicked.connect(self._on_new_clicked)
        self._duplicate_btn = QPushButton("Duplicate...", left)
        self._duplicate_btn.clicked.connect(self._on_duplicate_clicked)
        self._delete_btn = QPushButton("Delete...", left)
        self._delete_btn.clicked.connect(self._on_delete_clicked)
        list_btns.addWidget(self._new_btn)
        list_btns.addWidget(self._duplicate_btn)
        list_btns.addWidget(self._delete_btn)
        left_layout.addLayout(list_btns)
        splitter.addWidget(left)

        # ---- Right pane: editor + revert history ----
        right = QWidget(splitter)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self._editor_header = QLabel("<b>(no prompt selected)</b>", right)
        right_layout.addWidget(self._editor_header)
        self._editor = QPlainTextEdit(right)
        self._editor.setPlaceholderText(
            "Select or create a prompt to begin editing."
        )
        self._editor.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth,
        )
        self._set_monospace_font(self._editor)
        self._editor.textChanged.connect(self._on_editor_changed)
        right_layout.addWidget(self._editor, stretch=2)

        # ---- Variable hint panel ----
        hint_frame = QFrame(right)
        hint_frame.setFrameShape(QFrame.Shape.StyledPanel)
        hint_layout = QVBoxLayout(hint_frame)
        hint_layout.addWidget(QLabel("<b>Available variables</b>", hint_frame))
        hint_text_parts = []
        for token, desc in PROMPT_PLACEHOLDERS:
            hint_text_parts.append(
                f"<code>{token}</code> &nbsp;-- {desc}"
            )
        hint_label = QLabel("<br>".join(hint_text_parts), hint_frame)
        hint_label.setTextFormat(Qt.TextFormat.RichText)
        hint_label.setWordWrap(True)
        hint_layout.addWidget(hint_label)
        right_layout.addWidget(hint_frame)

        # ---- Previous versions ----
        history_label = QLabel(
            "<b>Previous versions</b> (newest first; double-click to revert)",
            right,
        )
        right_layout.addWidget(history_label)
        self._history_list = QListWidget(right)
        self._history_list.setMaximumHeight(110)
        self._history_list.itemDoubleClicked.connect(self._on_history_doubleclick)
        right_layout.addWidget(self._history_list)
        revert_btn_row = QHBoxLayout()
        self._revert_btn = QPushButton("Revert to selected", right)
        self._revert_btn.clicked.connect(self._on_revert_clicked)
        self._revert_btn.setEnabled(False)
        revert_btn_row.addStretch(1)
        revert_btn_row.addWidget(self._revert_btn)
        right_layout.addLayout(revert_btn_row)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)

        # ---- Footer: status + Save / Close ----
        footer = QHBoxLayout()
        self._status_label = QLabel("", self)
        footer.addWidget(self._status_label, stretch=1)
        self._save_btn = QPushButton("Save", self)
        self._save_btn.clicked.connect(self._on_save_clicked)
        self._save_btn.setEnabled(False)
        self._close_btn = QPushButton("Close", self)
        self._close_btn.clicked.connect(self._on_close_clicked)
        footer.addWidget(self._save_btn)
        footer.addWidget(self._close_btn)
        outer.addLayout(footer)

        # Ctrl+S shortcut for save.
        save_shortcut = QShortcut(QKeySequence("Ctrl+S"), self)
        save_shortcut.activated.connect(self._on_save_clicked)

        self._reload_prompt_list()

    # ---- list management -------------------------------------------------

    def _reload_prompt_list(self) -> None:
        """Repopulate the left list. Preserves the current selection
        if the named prompt still exists; otherwise selects the first
        row."""
        self._prompt_list.blockSignals(True)
        previously_selected = self._current_name
        self._prompt_list.clear()
        templates = prompts_mod.list_templates()
        for tpl in templates:
            label = tpl.name
            if prompts_mod.is_bundled_prompt(tpl.name):
                label += "   (bundled)"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, tpl.name)
            self._prompt_list.addItem(item)
        self._prompt_list.blockSignals(False)
        if not templates:
            self._current_name = None
            self._editor_header.setText("<b>(no prompts -- create one)</b>")
            self._editor.clear()
            self._editor.setReadOnly(True)
            return
        # Restore selection by name; fall back to first row.
        target_row = 0
        if previously_selected is not None:
            for i in range(self._prompt_list.count()):
                if self._prompt_list.item(i).data(Qt.ItemDataRole.UserRole) == previously_selected:
                    target_row = i
                    break
        self._prompt_list.setCurrentRow(target_row)

    def _on_prompt_selected(
        self,
        current: Optional[QListWidgetItem],
        _previous: Optional[QListWidgetItem],
    ) -> None:
        if self._dirty and self._current_name is not None:
            # Save-or-discard prompt before leaving an edited prompt.
            choice = QMessageBox.question(
                self,
                "Unsaved changes",
                f"You have unsaved changes to {self._current_name!r}. "
                "Save before switching?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
            )
            if choice == QMessageBox.StandardButton.Save:
                self._save_current()
            elif choice == QMessageBox.StandardButton.Cancel:
                # Revert the list selection. Block signals to avoid
                # re-entering this handler.
                self._prompt_list.blockSignals(True)
                for i in range(self._prompt_list.count()):
                    item = self._prompt_list.item(i)
                    if item.data(Qt.ItemDataRole.UserRole) == self._current_name:
                        self._prompt_list.setCurrentItem(item)
                        break
                self._prompt_list.blockSignals(False)
                return
            # Discard falls through and loads the new prompt.

        if current is None:
            self._current_name = None
            self._editor.clear()
            self._editor.setReadOnly(True)
            self._save_btn.setEnabled(False)
            self._delete_btn.setEnabled(False)
            self._duplicate_btn.setEnabled(False)
            self._editor_header.setText("<b>(no prompt selected)</b>")
            self._reload_history()
            return
        name = current.data(Qt.ItemDataRole.UserRole)
        self._current_name = name
        tpl = prompts_mod.get_template(name)
        if tpl is None:
            self._editor.clear()
            return
        bundled_marker = (
            "   <i>(bundled default)</i>"
            if prompts_mod.is_bundled_prompt(name) else ""
        )
        self._editor_header.setText(f"<b>{name}.md</b>{bundled_marker}")
        self._editor.blockSignals(True)
        self._editor.setPlainText(tpl.body)
        self._editor.blockSignals(False)
        self._editor.setReadOnly(False)
        self._dirty = False
        self._save_btn.setEnabled(False)
        self._delete_btn.setEnabled(True)
        self._duplicate_btn.setEnabled(True)
        self._status_label.setText("")
        self._reload_history()

    def _reload_history(self) -> None:
        self._history_list.clear()
        self._revert_btn.setEnabled(False)
        if self._current_name is None:
            return
        try:
            archived = prompts_mod.list_archived_versions(self._current_name)
        except prompts_mod.PromptError:
            return
        for ap in archived:
            line_count = ap.body.count("\n") + (1 if ap.body else 0)
            label = f"{ap.saved_at_display}   ({line_count} lines)"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, str(ap.path))
            item.setToolTip(
                ap.body[:200] + ("..." if len(ap.body) > 200 else "")
            )
            self._history_list.addItem(item)
        self._history_list.currentItemChanged.connect(self._on_history_selected)

    def _on_history_selected(
        self,
        current: Optional[QListWidgetItem],
        _previous: Optional[QListWidgetItem],
    ) -> None:
        self._revert_btn.setEnabled(current is not None)

    # ---- editor change tracking -----------------------------------------

    def _on_editor_changed(self) -> None:
        if self._current_name is None:
            return
        self._dirty = True
        self._save_btn.setEnabled(True)
        self._status_label.setText("Unsaved changes.")

    # ---- save / revert ---------------------------------------------------

    def _save_current(self) -> bool:
        if self._current_name is None:
            return False
        body = self._editor.toPlainText()
        try:
            prompts_mod.save_prompt(self._current_name, body)
        except prompts_mod.PromptError as exc:
            QMessageBox.critical(self, "Save failed", exc.reason)
            return False
        self._dirty = False
        self._save_btn.setEnabled(False)
        self._status_label.setText(f"Saved {self._current_name}.md")
        self._reload_history()
        return True

    def _on_save_clicked(self) -> None:
        self._save_current()

    def _on_revert_clicked(self) -> None:
        if self._current_name is None:
            return
        current_item = self._history_list.currentItem()
        if current_item is None:
            return
        archive_path = Path(current_item.data(Qt.ItemDataRole.UserRole))
        if self._dirty:
            choice = QMessageBox.question(
                self,
                "Discard unsaved changes?",
                "You have unsaved changes in the editor. Reverting "
                "will discard them (the just-replaced body is "
                "archived as part of the revert, so it stays "
                "recoverable). Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if choice != QMessageBox.StandardButton.Yes:
                return
        try:
            prompts_mod.restore_archived_version(self._current_name, archive_path)
        except prompts_mod.PromptError as exc:
            QMessageBox.critical(self, "Revert failed", exc.reason)
            return
        # Reload the editor + history to show the restored body.
        tpl = prompts_mod.get_template(self._current_name)
        if tpl is not None:
            self._editor.blockSignals(True)
            self._editor.setPlainText(tpl.body)
            self._editor.blockSignals(False)
        self._dirty = False
        self._save_btn.setEnabled(False)
        self._status_label.setText(f"Reverted to {current_item.text().split('   ')[0]}.")
        self._reload_history()

    def _on_history_doubleclick(self, item: QListWidgetItem) -> None:
        self._history_list.setCurrentItem(item)
        self._on_revert_clicked()

    # ---- new / duplicate / delete ---------------------------------------

    def _prompt_for_name(self, title: str, label: str) -> Optional[str]:
        name, ok = QInputDialog.getText(self, title, label)
        if not ok:
            return None
        try:
            return prompts_mod.validate_prompt_name(name)
        except prompts_mod.PromptError as exc:
            QMessageBox.warning(self, "Invalid name", exc.reason)
            return None

    def _on_new_clicked(self) -> None:
        name = self._prompt_for_name(
            "New Prompt",
            "Name (letters, digits, dash, underscore):",
        )
        if name is None:
            return
        try:
            prompts_mod.create_prompt(name)
        except prompts_mod.PromptError as exc:
            QMessageBox.warning(self, "Cannot create prompt", exc.reason)
            return
        self._current_name = name
        self._reload_prompt_list()

    def _on_duplicate_clicked(self) -> None:
        if self._current_name is None:
            return
        new_name = self._prompt_for_name(
            "Duplicate Prompt",
            f"New name (copying from {self._current_name!r}):",
        )
        if new_name is None:
            return
        try:
            prompts_mod.duplicate_prompt(self._current_name, new_name)
        except prompts_mod.PromptError as exc:
            QMessageBox.warning(self, "Cannot duplicate", exc.reason)
            return
        self._current_name = new_name
        self._reload_prompt_list()

    def _on_delete_clicked(self) -> None:
        if self._current_name is None:
            return
        if prompts_mod.is_bundled_prompt(self._current_name):
            msg = (
                f"{self._current_name!r} is a bundled default. Deleting "
                "removes your customizations; the bundled body will "
                "reappear the next time the app starts. Your edited "
                "body is archived first so you can recover it. Continue?"
            )
        else:
            msg = (
                f"Delete {self._current_name!r}? The current body is "
                "archived first so you can recover it from the archive "
                "directory."
            )
        choice = QMessageBox.question(
            self, "Delete prompt", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if choice != QMessageBox.StandardButton.Yes:
            return
        try:
            prompts_mod.delete_prompt(self._current_name)
        except prompts_mod.PromptError as exc:
            QMessageBox.critical(self, "Delete failed", exc.reason)
            return
        self._current_name = None
        self._reload_prompt_list()

    # ---- close handling --------------------------------------------------

    def _on_close_clicked(self) -> None:
        if self._dirty:
            choice = QMessageBox.question(
                self,
                "Unsaved changes",
                "You have unsaved changes. Save before closing?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
            )
            if choice == QMessageBox.StandardButton.Save:
                if not self._save_current():
                    return
            elif choice == QMessageBox.StandardButton.Cancel:
                return
        self.accept()

    # ---- helpers ---------------------------------------------------------

    @staticmethod
    def _set_monospace_font(editor: QPlainTextEdit) -> None:
        """Use the OS's preferred fixed-width font for the editor."""
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        # Bump readability slightly above default.
        font.setPointSize(max(10, font.pointSize()))
        editor.setFont(font)
