"""New session dialog -- title prompt plus per-session 'Keep recording' override.

Optionally exposes a "Pick from Calendar..." button that lets the user
pre-create a session against an upcoming Outlook meeting. When a meeting
is picked, the dialog reports it back via NewSessionResult.calendar_meeting
so the caller can seed live_notes.md from the invite.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..integrations import outlook_calendar


@dataclass
class NewSessionResult:
    title: str
    retain_audio: bool
    # When the user picked an Outlook meeting (either via the tray notification
    # imminent-meeting flow or via the Pick from Calendar... button), this
    # carries it so the caller can seed live_notes.md from the invite.
    calendar_meeting: Optional["outlook_calendar.MeetingInfo"] = None


class NewSessionDialog(QDialog):
    def __init__(
        self,
        *,
        retain_audio_default: bool = False,
        title_prefill: str = "",
        prefill_note: str = "",
        calendar_meeting: Optional["outlook_calendar.MeetingInfo"] = None,
        allow_calendar_pick: bool = True,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("New Session")
        self.setModal(True)
        self.resize(440, 240)
        self._selected_meeting: Optional[outlook_calendar.MeetingInfo] = calendar_meeting

        layout = QVBoxLayout(self)
        if prefill_note:
            prefill_label = QLabel(prefill_note, self)
            prefill_label.setWordWrap(True)
            layout.addWidget(prefill_label)
        layout.addWidget(QLabel("Session title:"))
        self._title_edit = QLineEdit(self)
        self._title_edit.setPlaceholderText("e.g. 1:1 with Manager, Standup, Customer Call")
        if title_prefill:
            self._title_edit.setText(title_prefill)
        layout.addWidget(self._title_edit)

        # Pick-from-calendar button -- visible only when Outlook is reachable
        # AND the caller hasn't already pre-filled from a calendar event
        # (the tray-notification path supplies calendar_meeting directly).
        self._pick_btn: Optional[QPushButton] = None
        if allow_calendar_pick and calendar_meeting is None and outlook_calendar.is_available():
            pick_row = QHBoxLayout()
            self._pick_btn = QPushButton("Pick from Calendar...", self)
            self._pick_btn.setToolTip(
                "Choose an upcoming Outlook meeting; its subject, attendees, "
                "and agenda will pre-fill this session."
            )
            self._pick_btn.clicked.connect(self._on_pick_calendar)
            pick_row.addWidget(self._pick_btn)
            pick_row.addStretch(1)
            layout.addLayout(pick_row)
            self._pick_status = QLabel("", self)
            self._pick_status.setWordWrap(True)
            layout.addWidget(self._pick_status)
        else:
            self._pick_status = None  # type: ignore[assignment]

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
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            bool(self._title_edit.text().strip())
        )

    def _on_accept(self) -> None:
        if self._title_edit.text().strip():
            self.accept()

    def result_value(self) -> NewSessionResult:
        return NewSessionResult(
            title=self._title_edit.text().strip(),
            retain_audio=self._retain_checkbox.isChecked(),
            calendar_meeting=self._selected_meeting,
        )

    def _on_pick_calendar(self) -> None:
        # Imported here so the static import graph stays Qt-only.
        from .calendar_picker_dialog import CalendarPickerDialog

        picker = CalendarPickerDialog(parent=self)
        if picker.exec() != QDialog.DialogCode.Accepted:
            return
        meeting = picker.selected_meeting()
        if meeting is None:
            return
        self._selected_meeting = meeting
        self._title_edit.setText(meeting.subject)
        self._title_edit.setFocus()
        if self._pick_status is not None:
            try:
                start = meeting.start_time.strftime("%H:%M")
            except Exception:
                start = "?"
            self._pick_status.setText(
                f"Pre-filled from \"{meeting.subject}\" (starts {start}). "
                f"Attendees + agenda will appear in My Notes."
            )
