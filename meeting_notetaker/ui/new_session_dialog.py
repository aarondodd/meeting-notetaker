"""New session dialog -- title prompt plus per-session 'Keep recording' override."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)


@dataclass
class NewSessionResult:
    title: str
    retain_audio: bool


class NewSessionDialog(QDialog):
    def __init__(self, *, retain_audio_default: bool = False, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New Session")
        self.setModal(True)
        self.resize(420, 180)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Session title:"))
        self._title_edit = QLineEdit(self)
        self._title_edit.setPlaceholderText("e.g. 1:1 with Manager, Standup, Customer Call")
        layout.addWidget(self._title_edit)

        self._retain_checkbox = QCheckBox("Keep the audio recording after transcription", self)
        self._retain_checkbox.setChecked(retain_audio_default)
        self._retain_checkbox.setToolTip(
            "Overrides the global default for this session only. "
            "Audio files live under the session folder; transcripts are kept regardless."
        )
        layout.addWidget(self._retain_checkbox)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._title_edit.textChanged.connect(
            lambda: buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
                bool(self._title_edit.text().strip())
            )
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)

    def _on_accept(self) -> None:
        if self._title_edit.text().strip():
            self.accept()

    def result_value(self) -> NewSessionResult:
        return NewSessionResult(
            title=self._title_edit.text().strip(),
            retain_audio=self._retain_checkbox.isChecked(),
        )
