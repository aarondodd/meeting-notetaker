"""Save to Obsidian picker dialog (issue #96).

Shows the user the planned filename + target subfolder + which
toggles will apply, plus a live preview of the YAML frontmatter
that will land at the top of the new note.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..integrations.obsidian_export import (
    ObsidianPublishOptions,
    ObsidianSessionInfo,
    build_frontmatter,
    sanitize_obsidian_stem,
)


@dataclass
class ObsidianPickerResult:
    options: ObsidianPublishOptions
    # Empty until the user chooses on a re-publish; the caller
    # re-runs export_to_obsidian with the choice baked into options.
    on_conflict: str = "save_as_new"


class ObsidianSavePicker(QDialog):
    """Form-style dialog. No tree view in v1 -- the location
    template + custom subdir field cover the common cases without
    forcing the user to navigate the vault tree."""

    def __init__(
        self,
        *,
        vault_root: Path,
        vault_name: str,
        default_subdir: str,
        default_filename_stem: str,
        attachment_count: int,
        session: ObsidianSessionInfo,
        write_frontmatter: bool,
        wikilink_attendees: bool,
        wikilink_series: bool,
        include_classification: bool,
        daily_note_backlink: bool,
        open_after_save: bool,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Save to Obsidian")
        self.setMinimumWidth(560)
        self._vault_root = vault_root
        self._vault_name = vault_name
        self._session = session
        self._write_frontmatter = write_frontmatter
        self._wikilink_attendees = wikilink_attendees
        self._wikilink_series = wikilink_series
        self._include_classification = include_classification

        root = QVBoxLayout(self)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        root.addLayout(form)

        form.addRow(QLabel("Vault:"), QLabel(str(vault_root)))

        subdir_row = QWidget()
        subdir_layout = QHBoxLayout(subdir_row)
        subdir_layout.setContentsMargins(0, 0, 0, 0)
        self._subdir_edit = QLineEdit(default_subdir)
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._pick_subdir)
        subdir_layout.addWidget(self._subdir_edit, 1)
        subdir_layout.addWidget(browse_btn)
        form.addRow("Subfolder:", subdir_row)

        self._filename_edit = QLineEdit(default_filename_stem)
        form.addRow("Filename (no .md):", self._filename_edit)

        self._attach_chk = QCheckBox(
            f"Include {attachment_count} attachment(s)"
            if attachment_count else "Include attachments"
        )
        self._attach_chk.setChecked(False)
        self._attach_chk.setEnabled(attachment_count > 0)
        form.addRow("", self._attach_chk)

        self._daily_chk = QCheckBox(
            "Append a backlink to today's daily note"
        )
        self._daily_chk.setChecked(daily_note_backlink)
        form.addRow("", self._daily_chk)

        self._open_chk = QCheckBox("Open in Obsidian after save")
        self._open_chk.setChecked(open_after_save)
        form.addRow("", self._open_chk)

        # Frontmatter preview.
        root.addWidget(QLabel("Frontmatter preview:"))
        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setMinimumHeight(160)
        font = self._preview.font()
        font.setStyleHint(font.StyleHint.Monospace)
        font.setFamily("monospace")
        self._preview.setFont(font)
        root.addWidget(self._preview)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Save")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._refresh_preview()
        self._filename_edit.textChanged.connect(self._refresh_preview)

    def _pick_subdir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose vault subfolder", str(self._vault_root),
        )
        if not chosen:
            return
        chosen_path = Path(chosen)
        try:
            rel = chosen_path.resolve().relative_to(self._vault_root.resolve())
        except ValueError:
            QMessageBox.warning(
                self, "Save to Obsidian",
                "The chosen folder is outside the configured vault.",
            )
            return
        self._subdir_edit.setText(str(rel).replace("\\", "/"))

    def _on_accept(self) -> None:
        stem = self._filename_edit.text().strip()
        if not stem:
            QMessageBox.warning(
                self, "Save to Obsidian", "Filename can't be empty.",
            )
            return
        cleaned = sanitize_obsidian_stem(stem)
        if cleaned != stem:
            self._filename_edit.setText(cleaned)
        self.accept()

    def _refresh_preview(self) -> None:
        if not self._write_frontmatter:
            self._preview.setPlainText("(frontmatter disabled in Settings)")
            return
        options = self._build_options()
        block = build_frontmatter(self._session, options)
        self._preview.setPlainText(block.strip() or "(no frontmatter)")

    def _build_options(self) -> ObsidianPublishOptions:
        return ObsidianPublishOptions(
            vault_root=self._vault_root,
            vault_name=self._vault_name,
            target_subdir=self._subdir_edit.text().strip(),
            filename_stem=sanitize_obsidian_stem(
                self._filename_edit.text().strip(),
                fallback=self._session.session_id,
            ),
            write_frontmatter=self._write_frontmatter,
            wikilink_attendees=self._wikilink_attendees,
            wikilink_series=self._wikilink_series,
            include_classification=self._include_classification,
            include_attachments=self._attach_chk.isChecked(),
            daily_note_backlink=self._daily_chk.isChecked(),
            open_after_save=self._open_chk.isChecked(),
        )

    def result_options(self) -> ObsidianPublishOptions:
        return self._build_options()
