"""Pick from Calendar dialog -- choose an upcoming Outlook meeting.

Launched from New Session via the "Pick from Calendar..." button. Lists
today's remaining meetings (start time + subject + duration). On accept
the chosen MeetingInfo is exposed via selected_meeting() so the caller
can pre-fill the session title + seed live_notes from the invite.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..integrations.outlook_calendar import MeetingInfo, fetch_remaining_today


class CalendarPickerDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Pick from Calendar")
        self.setModal(True)
        self.resize(560, 360)
        self._meetings: list[MeetingInfo] = []
        self._selected: Optional[MeetingInfo] = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Choose a meeting from today's remaining calendar. The session title, "
            "attendees, and agenda will be pre-filled on the new session.",
            self,
        ))

        self._list = QListWidget(self)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.itemDoubleClicked.connect(lambda _it: self._on_accept())
        layout.addWidget(self._list, 1)

        self._status = QLabel("", self)
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        self._ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_btn.setEnabled(False)
        self._ok_btn.setText("Use Selected")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._list.itemSelectionChanged.connect(self._on_selection_changed)
        self._populate()

    # ---- public API --------------------------------------------------------

    def selected_meeting(self) -> Optional[MeetingInfo]:
        return self._selected

    # ---- internal ----------------------------------------------------------

    def _populate(self) -> None:
        self._status.setText("Loading from Outlook...")
        try:
            self._meetings = fetch_remaining_today()
        except Exception as exc:
            self._meetings = []
            self._status.setText(f"Could not read Outlook calendar: {exc}")
            return

        if not self._meetings:
            self._status.setText(
                "No meetings found on your calendar for the rest of today. "
                "Outlook may not be running, or there genuinely are no more "
                "meetings today."
            )
            return

        self._status.clear()
        for m in self._meetings:
            item = QListWidgetItem(_format_meeting_row(m))
            item.setData(Qt.ItemDataRole.UserRole, m.entry_id)
            details = []
            if m.location:
                details.append(f"Location: {m.location}")
            if m.attendees:
                names = ", ".join(a.display for a in m.attendees if a.display)
                if names:
                    details.append(f"Attendees: {names}")
            if details:
                item.setToolTip("\n".join(details))
            self._list.addItem(item)

    def _on_selection_changed(self) -> None:
        self._ok_btn.setEnabled(bool(self._list.selectedItems()))

    def _on_accept(self) -> None:
        items = self._list.selectedItems()
        if not items:
            return
        entry_id = items[0].data(Qt.ItemDataRole.UserRole)
        self._selected = next(
            (m for m in self._meetings if m.entry_id == entry_id), None
        )
        self.accept()


def _format_meeting_row(m: MeetingInfo) -> str:
    try:
        start = m.start_time.strftime("%H:%M")
    except Exception:
        start = "??:??"
    try:
        duration_min = max(
            0, int((m.end_time - m.start_time).total_seconds() // 60)
        )
        dur = f"  ({duration_min} min)" if duration_min else ""
    except Exception:
        dur = ""
    return f"{start}  {m.subject}{dur}"
