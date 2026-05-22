"""Per-session four-pane view: transcript + my-notes + synthesis + previous-notes."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QTextCursor
from PyQt6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QTabWidget,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..models.session import (
    STATE_COMPLETE,
    STATE_ERROR,
    STATE_NEW,
    STATE_PAUSED,
    STATE_PROCESSING,
    STATE_RECORDING,
    Session,
)
from ..models.transcript import (
    TranscriptSegment,
    format_segment,
    label_for,
    rewrite_user_label,
)
from ..utils.export import build_print_markdown, default_export_filename
from ..utils.paths import session_dir
from .attendee_sidebar import AttendeeSidebar
from .live_notes_widget import LiveNotesWidget


class SessionView(QWidget):
    """Right-hand pane shown when a session is selected."""

    start_clicked = pyqtSignal(str)               # session_id
    pause_clicked = pyqtSignal(str)
    resume_clicked = pyqtSignal(str)
    stop_clicked = pyqtSignal(str)
    generate_prompt_clicked = pyqtSignal(str)
    paste_notes_clicked = pyqtSignal(str)
    # Synthesis Automation: emitted instead of the Generate/Paste pair
    # when the user has the feature enabled in Settings. Carries the
    # session id and the LLM target key ("claude" / "copilot").
    send_to_llm_clicked = pyqtSignal(str, str)      # session_id, target
    copy_tab_clicked = pyqtSignal(str, str)        # session_id, tab_id
    retain_audio_toggled = pyqtSignal(str, bool)   # session_id, value
    live_notes_changed = pyqtSignal(str, str)      # session_id, body
    # Emitted when the Synthesis tab body changes through inline editing
    # (not via Paste Response Back). The controller writes notes.md
    # without archiving on every save -- archiving only happens on the
    # wholesale Paste-Response-Back replacement.
    synthesis_notes_changed = pyqtSignal(str, str)  # session_id, body
    # Emitted when the user clicks Review Speakers. The handler walks
    # through every detected cluster in the session's diarization.json,
    # showing example transcript lines so the user can confirm /
    # rename / forget each one. Feeds corrections back to the speaker
    # store for the self-learning loop.
    review_speakers_clicked = pyqtSignal(str)  # session_id
    # Click-to-tag for in-meeting speaker anchoring. The sidebar emits
    # (session_id, name) per click; the controller persists a SpeakerTag
    # and the post-meeting refiner uses tags to constrain the clusterer.
    tag_speaker_clicked = pyqtSignal(str, str)            # session_id, name
    remove_last_tag_clicked = pyqtSignal(str, str)        # session_id, name

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._session: Optional[Session] = None
        self._provisional_segments: dict[tuple[str, float], int] = {}
        # Maps (source, t_start) -> line index in the transcript view.
        self._user_name = ""
        self._raw_transcript_text = ""
        # Synthesis Automation state. set_automation_enabled() flips
        # both at construction (via main_window) and when Settings is
        # closed; the click handler reads _automation_target to know
        # which LLM to route to.
        self._automation_enabled = False
        self._automation_target = ""
        self._live_notes_save_timer = QTimer(self)
        self._live_notes_save_timer.setSingleShot(True)
        self._live_notes_save_timer.setInterval(800)
        self._live_notes_save_timer.timeout.connect(self._flush_live_notes)
        self._suppress_live_notes_signal = False
        # Mirror of the above for the editable Synthesis tab. notes.md is
        # the latest LLM synthesis (or the user's edit of it); save on
        # debounce, no archive (Paste Response Back is the only path
        # that archives the prior version).
        self._notes_save_timer = QTimer(self)
        self._notes_save_timer.setSingleShot(True)
        self._notes_save_timer.setInterval(800)
        self._notes_save_timer.timeout.connect(self._flush_notes)
        self._suppress_notes_signal = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # Header
        header = QHBoxLayout()
        self._title_label = QLabel("(no session)", self)
        title_font = QFont()
        title_font.setPointSize(13)
        title_font.setBold(True)
        self._title_label.setFont(title_font)
        header.addWidget(self._title_label, 1)
        self._state_label = QLabel("", self)
        header.addWidget(self._state_label)
        layout.addLayout(header)

        # Transport row
        controls = QHBoxLayout()
        self._start_btn = QPushButton("Start", self)
        self._start_btn.clicked.connect(self._on_start)
        controls.addWidget(self._start_btn)
        self._pause_btn = QPushButton("Pause", self)
        self._pause_btn.clicked.connect(self._on_pause)
        controls.addWidget(self._pause_btn)
        self._resume_btn = QPushButton("Resume", self)
        self._resume_btn.clicked.connect(self._on_resume)
        controls.addWidget(self._resume_btn)
        self._stop_btn = QPushButton("Stop", self)
        self._stop_btn.clicked.connect(self._on_stop)
        controls.addWidget(self._stop_btn)
        controls.addStretch(1)
        self._retain_checkbox = QCheckBox("Keep audio for this session", self)
        self._retain_checkbox.toggled.connect(self._on_retain_toggled)
        controls.addWidget(self._retain_checkbox)
        layout.addLayout(controls)

        # Synthesis row
        synthesis = QHBoxLayout()
        self._generate_btn = QPushButton("Generate Synthesis Prompt", self)
        self._generate_btn.clicked.connect(self._on_generate_prompt)
        synthesis.addWidget(self._generate_btn)
        self._paste_btn = QPushButton("Paste Response Back...", self)
        self._paste_btn.clicked.connect(self._on_paste_notes)
        synthesis.addWidget(self._paste_btn)
        # Synthesis Automation: single Send button that replaces the
        # Generate + Paste pair when settings.synthesis.automation_enabled
        # is on. Toggled via set_automation_enabled() at construction +
        # whenever Settings is closed. Copy button stays visible
        # regardless of the toggle (Aaron's call -- the manual copy
        # path is still useful when the extension isn't reachable).
        self._send_btn = QPushButton("Send to Claude.ai", self)
        self._send_btn.setToolTip(
            "Send the synthesis prompt to the configured web LLM via "
            "the Meeting Notetaker browser extension. The response "
            "lands in the Synthesis tab automatically."
        )
        self._send_btn.clicked.connect(self._on_send_to_llm)
        self._send_btn.setVisible(False)
        synthesis.addWidget(self._send_btn)
        self._copy_btn = QPushButton("Copy", self)
        self._copy_btn.setToolTip(
            "Copy the active tab's contents to the clipboard. The button "
            "label updates to reflect which tab is active."
        )
        self._copy_btn.clicked.connect(self._on_copy_active_tab)
        synthesis.addWidget(self._copy_btn)
        self._print_btn = QPushButton("Print...", self)
        self._print_btn.setToolTip(
            "Send the active tab (My Notes or Synthesis) to a physical "
            "printer via the system print dialog. For a PDF copy, use the "
            "Export PDF button instead -- it preserves images and "
            "clickable links, which the Windows Print to PDF driver "
            "rasterizes away."
        )
        self._print_btn.clicked.connect(self._on_print)
        synthesis.addWidget(self._print_btn)
        self._export_pdf_btn = QPushButton("Export PDF...", self)
        self._export_pdf_btn.setToolTip(
            "Save the active tab (My Notes or Synthesis) directly to a "
            "PDF. Images and links are preserved (the Print path through "
            "Windows Print to PDF is lossy)."
        )
        self._export_pdf_btn.clicked.connect(self._on_export_pdf)
        synthesis.addWidget(self._export_pdf_btn)
        # Speaker review button. Hidden until the session has a
        # diarization.json on disk (set_session enables it).
        self._review_speakers_btn = QPushButton("Review Speakers...", self)
        self._review_speakers_btn.setToolTip(
            "Walk through each detected speaker with example transcript "
            "lines. Rename a mislabeled cluster or forget one. Updates "
            "the on-disk transcript and feeds corrections back to the "
            "speaker store so future meetings auto-recognize the same "
            "voices."
        )
        self._review_speakers_btn.clicked.connect(self._on_review_speakers)
        self._review_speakers_btn.setVisible(False)
        synthesis.addWidget(self._review_speakers_btn)
        synthesis.addStretch(1)
        layout.addLayout(synthesis)

        # Transcript / My Notes / Synthesis / Previous tabs
        self._tabs = QTabWidget(self)
        self._transcript_view = QPlainTextEdit(self)
        self._transcript_view.setReadOnly(True)
        self._transcript_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self._transcript_view.setFont(mono)
        self._tabs.addTab(self._transcript_view, "Transcript")

        self._live_notes_editor = LiveNotesWidget(self)
        self._live_notes_editor.setPlaceholderText(
            "Take notes here during the meeting using Markdown. Sections (Attendees / "
            "Agenda / Notes / Action Items) auto-seed on first open. Saved continuously. "
            "Included in the synthesis prompt; attendee names are extracted from the "
            "bulleted list. Click Preview to render Markdown, Edit to resume writing."
        )
        self._live_notes_editor.textChanged.connect(self._on_live_notes_changed)
        self._tabs.addTab(self._live_notes_editor, "My Notes")

        # Synthesis is editable (v0.5). Uses LiveNotesWidget for the same
        # Markdown edit/preview toggle + image-paste affordances as the
        # My Notes tab, with debounced save back to notes.md so tweaks
        # to the LLM-generated note persist.
        self._notes_view = LiveNotesWidget(self)
        self._notes_view.setPlaceholderText(
            "The LLM-generated synthesis lands here when you click Paste "
            "Response Back. The tab is editable -- tweak the rendered "
            "output before sharing. Click Edit to switch to the Markdown "
            "source, Preview to read it back rendered. Saved continuously."
        )
        self._notes_view.textChanged.connect(self._on_notes_changed)
        self._tabs.addTab(self._notes_view, "Synthesis")

        self._previous_view = QPlainTextEdit(self)
        self._previous_view.setReadOnly(True)
        self._tabs.addTab(self._previous_view, "Previous Notes")

        # Horizontal container holding the tab widget on the left and the
        # click-to-tag attendee sidebar on the right. The sidebar is
        # hidden by default; visibility is driven by recording state +
        # active tab (see `_refresh_sidebar_visibility`). When hidden it
        # occupies zero width, so the editor pane reclaims the space
        # without resizing the main window.
        body_row = QHBoxLayout()
        body_row.setContentsMargins(0, 0, 0, 0)
        body_row.setSpacing(0)
        body_row.addWidget(self._tabs, 1)
        self._attendee_sidebar = AttendeeSidebar(self)
        self._attendee_sidebar.setVisible(False)
        self._attendee_sidebar.tag_clicked.connect(self._on_attendee_tag_clicked)
        self._attendee_sidebar.remove_last_requested.connect(
            self._on_attendee_remove_last_clicked
        )
        body_row.addWidget(self._attendee_sidebar, 0)
        layout.addLayout(body_row, 1)
        self._tabs.currentChanged.connect(self._on_tab_changed)

        self._set_buttons_for_state(STATE_NEW, has_transcript=False, has_notes=False)
        self.set_session(None, transcript="", notes="", previous_notes_paths=[])

    # ---- public API --------------------------------------------------------

    def set_session(
        self,
        session: Optional[Session],
        *,
        transcript: str,
        notes: str,
        previous_notes_paths: list,
        live_notes: str = "",
    ) -> None:
        # Flush any pending live-notes / synthesis save before swapping out.
        if self._live_notes_save_timer.isActive():
            self._live_notes_save_timer.stop()
            self._flush_live_notes()
        if self._notes_save_timer.isActive():
            self._notes_save_timer.stop()
            self._flush_notes()
        self._session = session
        self._provisional_segments.clear()
        if session is None:
            self._title_label.setText("(no session)")
            self._state_label.setText("")
            self._raw_transcript_text = ""
            self._transcript_view.setPlainText("")
            self._notes_view.set_session_dir(None)
            self._set_notes_text("")
            self._previous_view.setPlainText("")
            self._live_notes_editor.set_session_dir(None)
            self._set_live_notes_text("")
            self._retain_checkbox.setChecked(False)
            self._retain_checkbox.setEnabled(False)
            self._set_buttons_for_state(STATE_NEW, has_transcript=False, has_notes=False)
            # Clear sidebar state on session deselect; counts will be
            # re-seeded by the controller on the next select.
            self._attendee_sidebar.set_counts({})
            self._attendee_sidebar.setVisible(False)
            return
        self._title_label.setText(session.title)
        self._state_label.setText(_pretty_state(session.state))
        self._raw_transcript_text = transcript
        self._transcript_view.setPlainText(rewrite_user_label(transcript, self._user_name))
        sdir = session_dir(session.id)
        self._notes_view.set_session_dir(sdir)
        self._set_notes_text(notes)
        # Synthesis defaults to preview mode (read-first UX); the user can
        # flip to Edit on demand. Empty body stays in edit so a fresh
        # paste-back lands directly in the editable buffer.
        self._notes_view.set_preview_mode(bool(notes.strip()))
        self._live_notes_editor.set_session_dir(sdir)
        self._set_live_notes_text(live_notes)
        self._retain_checkbox.setEnabled(True)
        self._retain_checkbox.blockSignals(True)
        self._retain_checkbox.setChecked(session.retain_audio)
        self._retain_checkbox.blockSignals(False)
        self._previous_view.setPlainText(_summarize_previous(previous_notes_paths))
        self._set_buttons_for_state(
            session.state,
            has_transcript=session.has_transcript or bool(transcript.strip()),
            has_notes=session.has_notes or bool(notes.strip()),
        )
        self._review_speakers_btn.setVisible(
            (sdir / "diarization.json").exists()
        )
        # The sidebar sits idle (hidden) until the controller seeds the
        # attendee list and the session enters STATE_RECORDING.
        self._refresh_sidebar_visibility()

    def update_state(self, state: str) -> None:
        if self._session is None:
            return
        self._session.state = state
        self._state_label.setText(_pretty_state(state))
        self._set_buttons_for_state(
            state,
            has_transcript=self._session.has_transcript or bool(self._transcript_view.toPlainText().strip()),
            has_notes=self._session.has_notes or bool(self._notes_view.toPlainText().strip()),
        )
        self._refresh_sidebar_visibility()

    # ---- click-to-tag attendee sidebar -------------------------------------

    def set_attendee_names(self, names: list[str]) -> None:
        """Refresh the sidebar's attendee list. Called by the controller
        whenever the live_notes '# Attendees' section changes."""
        self._attendee_sidebar.set_attendees(names)

    def set_speaker_tag_counts(self, counts: dict[str, int]) -> None:
        """Refresh the sidebar's per-name tag-count badges."""
        self._attendee_sidebar.set_counts(counts)

    def _on_attendee_tag_clicked(self, name: str) -> None:
        if self._session is None:
            return
        self.tag_speaker_clicked.emit(self._session.id, name)

    def _on_attendee_remove_last_clicked(self, name: str) -> None:
        if self._session is None:
            return
        self.remove_last_tag_clicked.emit(self._session.id, name)

    def _refresh_sidebar_visibility(self) -> None:
        """Sidebar shows only while actively recording AND viewing
        Transcript or My Notes. Hides on Synthesis / Previous Notes
        even mid-recording -- those tabs are read-only review surfaces."""
        if self._session is None or self._session.state not in (
            STATE_RECORDING, STATE_PAUSED,
        ):
            self._attendee_sidebar.setVisible(False)
            return
        current = self._tabs.currentWidget()
        on_transcript_or_notes = current in (
            self._transcript_view, self._live_notes_editor,
        )
        self._attendee_sidebar.setVisible(on_transcript_or_notes)

    def update_batch_progress(self, pct: int) -> None:
        """Reflect background batch-refinement progress in the state label."""
        if self._session is None:
            return
        if self._session.state == STATE_PROCESSING:
            self._state_label.setText(f"Refining transcript -- {pct}%")

    def append_segment(self, segment: TranscriptSegment) -> None:
        """Append a finalized segment; provisional segments use append_provisional."""
        line = format_segment(segment, self._user_name)
        cursor = self._transcript_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if self._transcript_view.toPlainText():
            cursor.insertText("\n")
        cursor.insertText(line)
        self._transcript_view.setTextCursor(cursor)
        self._transcript_view.ensureCursorVisible()
        # Track the raw (un-rewritten) form so we can re-render on name change.
        raw_line = format_segment(segment, "")
        if self._raw_transcript_text:
            self._raw_transcript_text += "\n"
        self._raw_transcript_text += raw_line

    def append_provisional(self, segment: TranscriptSegment) -> None:
        """Append a provisional segment that may be rewritten when the next overlap arrives."""
        # For v0.2 we currently treat provisional as final; future revisions can store the
        # line index in self._provisional_segments and overwrite on update.
        self.append_segment(segment)

    def set_transcript_text(self, text: str) -> None:
        """Replace the transcript view's contents. `text` should be the raw on-disk form."""
        self._raw_transcript_text = text
        self._transcript_view.setPlainText(rewrite_user_label(text, self._user_name))

    def set_title(self, new_title: str) -> None:
        """Update the displayed session title in place after a rename.

        No-op when no session is currently bound. Mutates the bound
        Session's `title` field so subsequent calls (e.g. the synthesis
        prompt render) see the new value without needing a full
        set_session() round-trip.
        """
        if self._session is None:
            return
        cleaned = (new_title or "").strip()
        if not cleaned:
            return
        self._session.title = cleaned
        self._title_label.setText(cleaned)

    def set_created_at(self, new_created_at_iso: str) -> None:
        """Update the bound session's created_at after a timestamp edit.

        No-op when no session is currently bound. Mirrors set_title():
        keeps the in-memory Session dataclass in sync so the synthesis
        prompt + print header pick up the new date without a full
        set_session() round-trip (which would discard in-flight live
        notes / synthesis edits).
        """
        if self._session is None:
            return
        cleaned = (new_created_at_iso or "").strip()
        if not cleaned:
            return
        self._session.created_at = cleaned

    def set_user_name(self, name: str) -> None:
        """Update the display label for the user's mic and refresh the transcript view."""
        new_name = (name or "").strip()
        if new_name == self._user_name:
            return
        self._user_name = new_name
        self._transcript_view.setPlainText(
            rewrite_user_label(self._raw_transcript_text, self._user_name)
        )

    def set_notes_text(self, text: str) -> None:
        """Replace the Synthesis body. Used by Paste-Response-Back and reload.

        Bypasses the debounce-and-emit path so this call doesn't trigger a
        synthesis_notes_changed signal back to the controller -- only
        user-typed edits should drive that save loop.
        """
        self._set_notes_text(text)
        # Flip to preview mode when there's actually content to read,
        # otherwise keep the editor focused (matching set_session).
        self._notes_view.set_preview_mode(bool(text.strip()))

    def set_previous_notes(self, paths: list) -> None:
        self._previous_view.setPlainText(_summarize_previous(paths))

    def current_live_notes(self) -> str:
        """Return the current live-notes editor body. Flushes pending saves."""
        if self._live_notes_save_timer.isActive():
            self._live_notes_save_timer.stop()
            self._flush_live_notes()
        return self._live_notes_editor.toPlainText()

    def flush_pending_live_notes(self) -> None:
        """Force any debounced live-notes save to commit immediately."""
        if self._live_notes_save_timer.isActive():
            self._live_notes_save_timer.stop()
            self._flush_live_notes()

    def flush_pending_notes(self) -> None:
        """Force any debounced synthesis-tab save to commit immediately."""
        if self._notes_save_timer.isActive():
            self._notes_save_timer.stop()
            self._flush_notes()

    def _set_live_notes_text(self, text: str) -> None:
        self._suppress_live_notes_signal = True
        try:
            self._live_notes_editor.setPlainText(text)
        finally:
            self._suppress_live_notes_signal = False

    def _set_notes_text(self, text: str) -> None:
        self._suppress_notes_signal = True
        try:
            self._notes_view.setPlainText(text)
        finally:
            self._suppress_notes_signal = False

    def _on_notes_changed(self) -> None:
        if self._suppress_notes_signal:
            return
        if self._session is None:
            return
        self._notes_save_timer.start()

    def _flush_notes(self) -> None:
        if self._session is None:
            return
        body = self._notes_view.toPlainText()
        self.synthesis_notes_changed.emit(self._session.id, body)

    def _on_live_notes_changed(self) -> None:
        if self._suppress_live_notes_signal:
            return
        if self._session is None:
            return
        self._live_notes_save_timer.start()

    def _flush_live_notes(self) -> None:
        if self._session is None:
            return
        body = self._live_notes_editor.toPlainText()
        self.live_notes_changed.emit(self._session.id, body)

    # ---- internal handlers -------------------------------------------------

    def _on_start(self) -> None:
        if self._session:
            self.start_clicked.emit(self._session.id)

    def _on_pause(self) -> None:
        if self._session:
            self.pause_clicked.emit(self._session.id)

    def _on_resume(self) -> None:
        if self._session:
            self.resume_clicked.emit(self._session.id)

    def _on_stop(self) -> None:
        if self._session:
            self.stop_clicked.emit(self._session.id)

    def _on_generate_prompt(self) -> None:
        if self._session:
            self.generate_prompt_clicked.emit(self._session.id)

    def _on_paste_notes(self) -> None:
        if self._session:
            self.paste_notes_clicked.emit(self._session.id)

    def _on_send_to_llm(self) -> None:
        if self._session and self._automation_target:
            self.send_to_llm_clicked.emit(
                self._session.id, self._automation_target
            )

    def set_automation_enabled(self, enabled: bool, target_key: str = "claude") -> None:
        """Swap between manual (Generate + Paste) and automated (Send)
        synthesis buttons. Copy / Print / Export PDF stay visible
        regardless -- the user keeps the manual escape hatches.

        target_key drives the Send button label so the user knows
        where they're sending without opening Settings."""
        self._automation_enabled = enabled
        self._automation_target = target_key if enabled else ""
        self._generate_btn.setVisible(not enabled)
        self._paste_btn.setVisible(not enabled)
        self._send_btn.setVisible(enabled)
        # Label reflects the configured target.
        if enabled:
            try:
                from ..automation.targets import get_target

                target = get_target(target_key)
                self._send_btn.setText(f"Send to {target.label}")
                if not target.implemented:
                    self._send_btn.setEnabled(False)
                    self._send_btn.setToolTip(
                        f"{target.label} automation is not yet "
                        "implemented. Pick a different target in "
                        "Settings, or toggle automation off to use the "
                        "manual Generate / Paste flow."
                    )
            except ValueError:
                self._send_btn.setText("Send to LLM")
        # Visibility change alone is enough; the next state update
        # from the controller will refresh enabled-state via the
        # _set_buttons_for_state path. If we're already in a state
        # where the transcript is ready, force a refresh now so the
        # button enables without waiting for the next state event.
        if self._session is not None:
            self._set_buttons_for_state(
                self._session.state,
                has_transcript=bool(self._raw_transcript_text),
                has_notes=bool(self._session.has_notes if self._session else False),
            )

    def _on_copy_active_tab(self) -> None:
        if not self._session:
            return
        tab_id = self._active_tab_id()
        if tab_id is None:
            return
        self.copy_tab_clicked.emit(self._session.id, tab_id)

    def _active_tab_id(self) -> Optional[str]:
        current = self._tabs.currentWidget()
        if current is self._transcript_view:
            return "transcript"
        if current is self._live_notes_editor:
            return "live_notes"
        if current is self._notes_view:
            return "notes"
        if current is self._previous_view:
            return "previous"
        return None

    def active_tab_text(self) -> str:
        """Return the active tab's text in a clipboard-friendly form."""
        tab_id = self._active_tab_id()
        if tab_id == "transcript":
            return self._transcript_view.toPlainText()
        if tab_id == "live_notes":
            return self._live_notes_editor.toPlainText()
        if tab_id == "notes":
            return self._notes_view.toPlainText()
        if tab_id == "previous":
            return self._previous_view.toPlainText()
        return ""

    def active_tab_label(self) -> str:
        """Display label for the active tab, used in toasts + Copy button."""
        tab_id = self._active_tab_id()
        return {
            "transcript": "Transcript",
            "live_notes": "My Notes",
            "notes": "Synthesis",
            "previous": "Previous Notes",
        }.get(tab_id or "", "")

    def _on_review_speakers(self) -> None:
        if self._session:
            self.review_speakers_clicked.emit(self._session.id)

    def set_has_diarization(self, has: bool) -> None:
        """Show or hide the Review Speakers button.

        Called by the app layer after refinement completes (button on)
        and when a session is loaded that has a diarization.json on
        disk (also on). Hidden in all other cases.
        """
        self._review_speakers_btn.setVisible(has)

    def _on_retain_toggled(self, checked: bool) -> None:
        if self._session:
            self._session.retain_audio = checked
            self.retain_audio_toggled.emit(self._session.id, checked)

    def _set_buttons_for_state(self, state: str, *, has_transcript: bool, has_notes: bool) -> None:
        is_new = state == STATE_NEW
        is_recording = state == STATE_RECORDING
        is_paused = state == STATE_PAUSED
        is_processing = state == STATE_PROCESSING
        is_complete = state in (STATE_COMPLETE, STATE_ERROR)

        has_session = self._session is not None
        self._start_btn.setEnabled(has_session and (is_new or is_complete))
        self._pause_btn.setEnabled(has_session and is_recording)
        self._resume_btn.setEnabled(has_session and is_paused)
        self._stop_btn.setEnabled(has_session and (is_recording or is_paused))
        # Generate/paste are available as soon as a transcript exists. The
        # batch-refinement pass after Stop runs in the background and is
        # explicitly NOT a gate on synthesis -- the live transcript is good
        # enough to act on, and any later regenerate will pick up the
        # refined version automatically.
        can_synthesize = has_session and has_transcript and not is_recording
        self._generate_btn.setEnabled(can_synthesize)
        self._paste_btn.setEnabled(has_session and (has_transcript or has_notes) and not is_recording)
        # Send button mirrors Generate's gating; the bridge connectivity
        # gating happens at the controller layer (it surfaces a dialog
        # if the extension isn't reachable, rather than silently
        # disabling the button -- the user might have just not loaded
        # the extension yet and they're more likely to investigate via
        # a click + error message than via a greyed-out button).
        if self._automation_enabled and self._automation_target:
            from ..automation.targets import get_target

            try:
                implemented = get_target(self._automation_target).implemented
            except ValueError:
                implemented = False
            self._send_btn.setEnabled(can_synthesize and implemented)
        self._update_copy_button(has_session=has_session)
        self._update_print_button(has_session=has_session, has_notes=has_notes)

    def _on_tab_changed(self, _index: int) -> None:
        has_session = self._session is not None
        has_notes = bool(
            self._session and (self._session.has_notes or self._notes_view.toPlainText().strip())
        )
        self._update_copy_button(has_session=has_session)
        self._update_print_button(has_session=has_session, has_notes=has_notes)
        self._refresh_sidebar_visibility()

    def _update_copy_button(self, *, has_session: bool) -> None:
        """Label + enabled state track the active tab."""
        label = self.active_tab_label()
        if not has_session or not label:
            self._copy_btn.setText("Copy")
            self._copy_btn.setEnabled(False)
            return
        self._copy_btn.setText(f"Copy {label}")
        # Copy is meaningful any time the tab has any text content.
        self._copy_btn.setEnabled(bool(self.active_tab_text().strip()))

    def _update_print_button(self, *, has_session: bool, has_notes: bool) -> None:
        """Print + Export PDF only matter on My Notes / Synthesis tabs."""
        if not has_session:
            self._print_btn.setEnabled(False)
            self._export_pdf_btn.setEnabled(False)
            return
        current = self._tabs.currentWidget()
        if current is self._live_notes_editor:
            self._print_btn.setEnabled(True)
            self._export_pdf_btn.setEnabled(True)
        elif current is self._notes_view:
            self._print_btn.setEnabled(has_notes)
            self._export_pdf_btn.setEnabled(has_notes)
        else:
            self._print_btn.setEnabled(False)
            self._export_pdf_btn.setEnabled(False)

    def _build_print_document(self):
        """Render the active tab into a QTextDocument bound to the session dir.

        Returns (doc, tab_label) or (None, "") if the active tab can't be
        printed. Uses PrintTextDocument so that relative image refs like
        `images/foo.png` resolve to real files on every QPrinter
        loadResource() call -- QTextDocument's own setBaseUrl is only
        honored on the first call, which produced broken-image icons in
        printed PDFs.
        """
        if self._session is None:
            return None, ""
        from .print_document import PrintTextDocument

        current = self._tabs.currentWidget()
        if current is self._live_notes_editor:
            markdown_source = self._live_notes_editor.toPlainText()
            tab_label = "My Notes"
        elif current is self._notes_view:
            markdown_source = self._notes_view.toPlainText()
            tab_label = "Synthesis"
        else:
            return None, ""

        # Parse the session's created_at into a datetime for the header,
        # converted from stored UTC to the user's local timezone so the
        # printed header matches what they see elsewhere in the app.
        # Falls through silently if the stored string is unparseable;
        # the header just renders without the date.
        session_when = None
        if self._session.created_at:
            from datetime import datetime
            try:
                session_when = datetime.fromisoformat(
                    self._session.created_at.replace("Z", "+00:00")
                ).astimezone()
            except ValueError:
                session_when = None

        printable = build_print_markdown(
            session_title=self._session.title,
            tab_label=tab_label,
            session_date=session_when,
            body=markdown_source,
        )

        sdir = session_dir(self._session.id)
        doc = PrintTextDocument(sdir, parent=self)
        doc.setMarkdown(printable)
        return doc, tab_label

    def _on_print(self) -> None:
        """Print the active tab via QPrinter."""
        doc, tab_label = self._build_print_document()
        if doc is None or self._session is None:
            return
        from PyQt6.QtPrintSupport import QPrintDialog, QPrinter

        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setDocName(f"{self._session.title} -- {tab_label}")
        dialog = QPrintDialog(printer, self)
        dialog.setWindowTitle(f"Print -- {self._session.title} -- {tab_label}")
        if dialog.exec() != QPrintDialog.DialogCode.Accepted:
            return
        doc.print(printer)

    def _on_export_pdf(self) -> None:
        """Save the active tab as a PDF via Qt's native PDF backend.

        Qt's PDF writer preserves images (via direct embedding) and link
        annotations (Markdown `[text](url)` becomes a clickable PDF
        annotation), where the Windows Print-to-PDF driver typically
        rasterizes both away.
        """
        doc, tab_label = self._build_print_document()
        if doc is None or self._session is None:
            return
        from PyQt6.QtWidgets import QFileDialog, QMessageBox
        from PyQt6.QtPrintSupport import QPrinter

        suggested_name = default_export_filename(
            self._session.title, tab_label, ".pdf"
        )
        suggested_path = str(session_dir(self._session.id) / suggested_name)
        path_str, _filter = QFileDialog.getSaveFileName(
            self,
            f"Export {tab_label} as PDF",
            suggested_path,
            "PDF documents (*.pdf)",
        )
        if not path_str:
            return
        target = Path(path_str)
        if target.suffix.lower() != ".pdf":
            target = target.with_suffix(".pdf")

        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer.setOutputFileName(str(target))
        printer.setDocName(f"{self._session.title} -- {tab_label}")
        try:
            doc.print(printer)
        except Exception as exc:
            QMessageBox.warning(self, "Export PDF", f"Could not write PDF: {exc}")
            return
        self.window().statusBar().showMessage(
            f"Exported PDF to {target.name}", 5000
        )


def _pretty_state(state: str) -> str:
    pretty = {
        STATE_NEW: "New",
        STATE_RECORDING: "Recording",
        STATE_PAUSED: "Paused",
        STATE_PROCESSING: "Refining transcript -- you can synthesize now",
        STATE_COMPLETE: "Complete",
        STATE_ERROR: "Error",
    }
    return pretty.get(state, state.title())


def _summarize_previous(paths: list) -> str:
    if not paths:
        return "(no archived notes for this session)"
    lines = ["Archived notes for this session:", ""]
    for p in paths:
        try:
            body = p.read_text(encoding="utf-8")
        except OSError:
            body = "(unreadable)"
        lines.append(f"=== {p.name} ===")
        lines.append(body.strip())
        lines.append("")
    return "\n".join(lines).rstrip()
