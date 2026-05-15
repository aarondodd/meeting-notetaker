"""Generate-prompt + paste-notes-back dialogs.

GeneratePromptDialog: picks a template, renders against the transcript,
copies to clipboard via pyperclip, shows the rendered prompt for preview.

PasteNotesDialog: large multiline text input + 'Save' button. On save,
TranscriptStore.save_notes() archives any prior notes.md as
notes-YYYYMMDD-HHMM.md before writing the new body.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

import pyperclip
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..utils import prompts as prompts_mod
from ..utils.prompts import PromptTemplate


class GeneratePromptDialog(QDialog):
    def __init__(
        self,
        *,
        session_title: str,
        session_date: datetime,
        transcript: str,
        templates: Iterable[PromptTemplate],
        live_notes: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Generate Synthesis Prompt")
        self.setModal(True)
        self.resize(720, 560)

        self._session_title = session_title
        self._session_date = session_date
        self._transcript = transcript
        self._live_notes = live_notes
        self._templates = list(templates)

        layout = QVBoxLayout(self)

        picker_row = QHBoxLayout()
        picker_row.addWidget(QLabel("Template:"))
        self._picker = QComboBox(self)
        for tpl in self._templates:
            self._picker.addItem(tpl.display_name, tpl)
        self._picker.currentIndexChanged.connect(self._refresh_preview)
        picker_row.addWidget(self._picker, 1)
        layout.addLayout(picker_row)

        layout.addWidget(QLabel("Preview (copied to clipboard on 'Copy & Close'):"))
        self._preview = QPlainTextEdit(self)
        self._preview.setReadOnly(True)
        self._preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self._preview, 1)

        button_row = QHBoxLayout()
        self._copy_button = QPushButton("Copy to Clipboard", self)
        self._copy_button.clicked.connect(self._copy_to_clipboard)
        button_row.addWidget(self._copy_button)
        self._copy_close_button = QPushButton("Copy && Close", self)
        self._copy_close_button.clicked.connect(self._copy_and_close)
        button_row.addWidget(self._copy_close_button)
        button_row.addStretch(1)
        cancel = QPushButton("Close", self)
        cancel.clicked.connect(self.reject)
        button_row.addWidget(cancel)
        layout.addLayout(button_row)

        if self._templates:
            self._refresh_preview()
        else:
            self._preview.setPlainText(
                "No prompt templates found.\n"
                "Add Markdown files to %APPDATA%/MeetingNotetaker/prompts/ "
                "and re-open this dialog."
            )
            self._copy_button.setEnabled(False)
            self._copy_close_button.setEnabled(False)

    def _refresh_preview(self) -> None:
        tpl = self._picker.currentData()
        if tpl is None:
            return
        rendered = prompts_mod.render(
            tpl,
            session_title=self._session_title,
            session_date=self._session_date,
            transcript=self._transcript,
            live_notes=self._live_notes,
        )
        self._preview.setPlainText(rendered)

    def _copy_to_clipboard(self) -> None:
        pyperclip.copy(self._preview.toPlainText())

    def _copy_and_close(self) -> None:
        self._copy_to_clipboard()
        self.accept()


class PasteNotesDialog(QDialog):
    def __init__(self, *, current_notes: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Paste Notes from LLM")
        self.setModal(True)
        self.resize(720, 560)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Paste the response you got back from Claude or Copilot:"))
        self._editor = QPlainTextEdit(self)
        self._editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._editor.setPlainText(current_notes)
        layout.addWidget(self._editor, 1)

        self._archive_checkbox = QCheckBox("Archive existing notes.md before saving (recommended)", self)
        self._archive_checkbox.setChecked(True)
        layout.addWidget(self._archive_checkbox)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def body(self) -> str:
        return self._editor.toPlainText()

    @property
    def archive_existing(self) -> bool:
        return self._archive_checkbox.isChecked()
