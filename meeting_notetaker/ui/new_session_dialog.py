"""New session dialog -- title prompt plus per-session 'Keep recording' override.

Optionally exposes a "Pick from Calendar..." button that lets the user
pre-create a session against an upcoming Outlook meeting. When a meeting
is picked, the dialog reports it back via NewSessionResult.calendar_meeting
so the caller can seed live_notes.md from the invite.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
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


# Debounce between a title keystroke and the auto-suggest call. Short
# enough to feel immediate when the user finishes typing; long enough
# to coalesce a fast typing burst into one suggest pass.
_SUGGEST_DEBOUNCE_MS = 300


@dataclass
class NewSessionResult:
    title: str
    retain_audio: bool
    # Per-session override of transcription.capture_only_mode. None means
    # use the global Settings value; True/False forces it for this session
    # only. The override doesn't persist past Stop.
    capture_only_override: Optional[bool] = None
    # When the user picked an Outlook meeting (either via the tray notification
    # imminent-meeting flow or via the Pick from Calendar... button), this
    # carries it so the caller can seed live_notes.md from the invite.
    calendar_meeting: Optional["outlook_calendar.MeetingInfo"] = None
    # Series the new session belongs to. Empty string = no series.
    # Non-empty values may be a new name (will be created on apply)
    # or the name of an existing series (will be linked). The caller
    # is responsible for routing through ClassificationStore.
    # get_or_create_series + assign_series.
    series_name: str = ""


class NewSessionDialog(QDialog):
    def __init__(
        self,
        *,
        retain_audio_default: bool = False,
        capture_only_default: bool = False,
        title_prefill: str = "",
        prefill_note: str = "",
        calendar_meeting: Optional["outlook_calendar.MeetingInfo"] = None,
        allow_calendar_pick: bool = True,
        series_names: Optional[list[str]] = None,
        suggest_series: Optional[Callable[[str], Optional[str]]] = None,
        initial_series_name: str = "",
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

        # Series picker. Editable so the user can type a brand-new
        # name; an existing pick links via ClassificationStore.
        # get_or_create_series in MainApp's accept handler. First
        # entry is the "(none)" sentinel with empty data; remaining
        # entries are existing series alphabetized.
        #
        # Auto-suggest: when ``suggest_series`` is provided, the
        # dialog runs the callable on the current title (debounced)
        # and seeds the combobox with the result -- unless the user
        # has already touched the combo (tracked via the activated
        # signal, which fires only on user actions, not programmatic
        # setCurrentIndex / setCurrentText). Once touched, further
        # title edits leave the combobox alone.
        layout.addWidget(QLabel("Series:"))
        self._series_picker = QComboBox(self)
        self._series_picker.setEditable(True)
        self._series_picker.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._series_picker.setToolTip(
            "Group this session with others that follow the same "
            "recurring pattern. Pick an existing series, type a new "
            "name to create one, or leave (none) to skip. The dialog "
            "auto-suggests based on similar session titles in your "
            "history; your pick always wins."
        )
        self._series_picker.addItem("(none)", "")
        for name in (series_names or []):
            self._series_picker.addItem(name, name)
        # Sentinel: True once the user has interacted with the
        # picker; suppresses further auto-suggest overrides so a
        # title edit after manual selection doesn't fight the user.
        self._series_user_touched = False
        # When the dialog is constructed with an explicit
        # initial_series_name (carries a pre-existing choice -- e.g.
        # a calendar-prefilled session that already had a series
        # assigned), set it and freeze further auto-fills.
        if initial_series_name:
            self._set_series_text(initial_series_name)
            self._series_user_touched = True
        self._series_picker.activated.connect(self._on_series_activated)
        # Editable comboboxes emit editTextChanged for keystrokes in
        # the line edit; we treat that as user-touched too so the
        # auto-fill doesn't stomp on a name the user is mid-typing.
        self._series_picker.editTextChanged.connect(
            self._on_series_edit_text_changed,
        )
        layout.addWidget(self._series_picker)

        # Auto-suggest plumbing: debounced timer fires after title
        # edits settle, then calls suggest_series(current_title) and
        # applies the result if the user hasn't touched the picker.
        self._suggest_series_fn = suggest_series
        self._suggest_timer = QTimer(self)
        self._suggest_timer.setSingleShot(True)
        self._suggest_timer.setInterval(_SUGGEST_DEBOUNCE_MS)
        self._suggest_timer.timeout.connect(self._run_series_suggest)
        if suggest_series is not None:
            self._title_edit.textChanged.connect(self._on_title_changed_for_suggest)
            # Title may already carry a prefill (calendar path). Kick
            # the suggest once on open so the picker reflects the
            # first guess; suppressed if the dialog was constructed
            # with an explicit initial_series_name.
            if title_prefill and not initial_series_name:
                # Defer one event-loop tick so the suggest runs after
                # the dialog's widgets are fully wired.
                QTimer.singleShot(0, self._run_series_suggest)

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

        # Per-session override of capture-only mode. Defaults to the
        # global Settings value; flipping it for this session only is the
        # supported way to capture quietly when concurrent meetings are
        # competing for the Whisper model.
        self._capture_only_checkbox = QCheckBox(
            "Capture-only (skip live transcript for this session)", self
        )
        self._capture_only_checkbox.setChecked(capture_only_default)
        self._capture_only_checkbox.setToolTip(
            "When on, no live transcription runs during recording. The "
            "WAV is still captured and the post-Stop refinement still "
            "produces a full transcript. Useful when other sessions are "
            "already processing and you don't need a live pane."
        )
        layout.addWidget(self._capture_only_checkbox)
        # Remember the global default so we can report None when the user
        # didn't actually deviate from it; that lets the caller keep the
        # config-driven path for sessions that weren't overridden.
        self._capture_only_default = capture_only_default

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
        capture_checked = self._capture_only_checkbox.isChecked()
        capture_override: Optional[bool] = (
            None if capture_checked == self._capture_only_default else capture_checked
        )
        # Series: editable combobox carries either the user's typed
        # value or the picked label. Empty + the "(none)" sentinel
        # both report as empty string -- the caller treats both the
        # same way (no series assignment).
        series_name = self._series_picker.currentText().strip()
        if series_name == "(none)":
            series_name = ""
        return NewSessionResult(
            title=self._title_edit.text().strip(),
            retain_audio=self._retain_checkbox.isChecked(),
            capture_only_override=capture_override,
            calendar_meeting=self._selected_meeting,
            series_name=series_name,
        )

    # ---- series picker plumbing -----------------------------------

    def _set_series_text(self, name: str) -> None:
        """Set the picker to ``name`` -- selecting the matching row
        if one exists, otherwise dropping the text directly into the
        line edit. Does NOT trip the user-touched sentinel (only
        user-initiated activations / typing do)."""
        if not name:
            self._series_picker.setCurrentIndex(0)
            return
        idx = self._series_picker.findText(name)
        if idx >= 0:
            self._series_picker.setCurrentIndex(idx)
        else:
            self._series_picker.setEditText(name)

    def _on_series_activated(self, _index: int) -> None:
        """Fires only when the user picks a row from the dropdown,
        not when setCurrentIndex is called programmatically. Freezes
        the auto-fill so the user's pick stands."""
        self._series_user_touched = True

    def _on_series_edit_text_changed(self, text: str) -> None:
        """Fires on every keystroke in the editable line edit AND
        when setEditText runs programmatically. Distinguish by
        checking whether the timer's most recent suggest applied the
        same text -- if so it's our doing and we don't flip the
        sentinel. Anything else is the user typing."""
        if getattr(self, "_last_autofill", None) == text:
            return
        self._series_user_touched = True

    def _on_title_changed_for_suggest(self, _text: str) -> None:
        """Restart the debounce timer. The actual suggest runs in
        _run_series_suggest after _SUGGEST_DEBOUNCE_MS quiet ms."""
        self._suggest_timer.start()

    def _run_series_suggest(self) -> None:
        """Call the injected suggest callable and apply the result
        if the user hasn't touched the picker yet."""
        if self._series_user_touched or self._suggest_series_fn is None:
            return
        title = self._title_edit.text().strip()
        if not title:
            return
        try:
            suggestion = self._suggest_series_fn(title)
        except Exception:
            # Defensive: a buggy suggest must not break the dialog.
            return
        if not suggestion:
            return
        self._last_autofill = suggestion
        self._set_series_text(suggestion)

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
