"""Per-session four-pane view: transcript + my-notes + synthesis + previous-notes."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import bisect
import re

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
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
from .previous_notes_widget import PreviousNotesWidget
from .screencap_sidebar import ScreencapSidebar
from .slides_widget import SlidesWidget
from .transcript_player_bar import TranscriptPlayerBar


# How many milliseconds before the clicked transcript line to seek to.
# Aaron asked for "just before that line's timestamp (~10s)" so the
# listen-back catches the lead-in. Pulled out so it's easy to tune.
_TRANSCRIPT_SEEK_LEAD_MS = 10_000


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
    # Previous-notes pane actions. Restore swaps an archive into the
    # current notes.md; Delete removes the archive file. Both are
    # handled by MainApp (which owns the on-disk store).
    restore_previous_notes_clicked = pyqtSignal(str, Path)   # session_id, archive_path
    delete_previous_notes_clicked = pyqtSignal(str, Path)    # session_id, archive_path
    # User picked a different synthesis prompt template for this
    # session. Persisted to the session's metadata.json so the choice
    # survives reloads, and used by both the automation Send flow and
    # the manual Generate dialog as the default. Empty string means
    # "use the bundled default template."
    prompt_template_changed = pyqtSignal(str, str)            # session_id, template_name
    # Click-to-tag for in-meeting speaker anchoring. The sidebar emits
    # (session_id, name) per click; the controller persists a SpeakerTag
    # and the post-meeting refiner uses tags to constrain the clusterer.
    tag_speaker_clicked = pyqtSignal(str, str)            # session_id, name
    remove_last_tag_clicked = pyqtSignal(str, str)        # session_id, name
    # Screen-capture lifecycle. start_screen_capture_clicked carries the
    # session id; MainApp shows the first-time popup (if needed) and
    # launches the region picker. stop_screen_capture_clicked tears the
    # session's capture state down. The two sidebar signals fire from
    # the My Notes pane's Capture / Insert buttons; both implicitly
    # operate on the currently-armed region for the active session.
    start_screen_capture_clicked = pyqtSignal(str)        # session_id
    stop_screen_capture_clicked = pyqtSignal(str)         # session_id
    screencap_capture_clicked = pyqtSignal(str)           # session_id
    screencap_insert_clicked = pyqtSignal(str)            # session_id
    # Right-click on a Slides thumbnail / full view: delete the file.
    delete_screenshot_clicked = pyqtSignal(str, Path)     # session_id, path
    # Transcript-pane playback control. The bar fires these for the
    # session id MainApp tracks; the seek signal also fires when the
    # user clicks a transcript line (with the line's start - 10s).
    transcript_play_clicked = pyqtSignal(str)             # session_id
    transcript_pause_clicked = pyqtSignal(str)            # session_id
    transcript_seek_ms_requested = pyqtSignal(str, int)   # session_id, ms

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
        # Per-session "synthesis automation is mid-flight" state. We
        # only need a single slot because the SessionView shows one
        # session at a time; MainApp tracks which session_id owns it
        # so re-entering a session while its synthesis is still
        # running keeps the indicator on.
        self._synth_in_progress_session_id: Optional[str] = None
        # Current (chrome_running, bridge_connected) combined state.
        # Set by MainApp via set_synthesis_connection_state on each
        # 5-second poll tick + on bridge connect/disconnect. Defaults
        # to NOT_RUNNING so the Send button enables until proven
        # otherwise -- the launch-on-Send path handles the case
        # where Chrome really isn't up.
        self._synth_connection_state = None  # SynthesisConnectionState, set by app
        # Screen-capture armed state. True once the user clicked Start
        # Screen Capture and drew a region; flipped back when they
        # click Stop or the recording ends. Drives both the toggle
        # button text and the My Notes sidebar enablement.
        self._screencap_armed = False
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
        # Screen-capture toggle. Disabled unless the session is in
        # RECORDING or PAUSED (set_buttons_for_state drives this); the
        # button text flips between "Start Screen Capture" and "Stop
        # Screen Capture" as the user toggles.
        self._screen_capture_btn = QPushButton("Start Screen Capture", self)
        self._screen_capture_btn.setToolTip(
            "Draw a region on screen, then Capture / Insert from the "
            "My Notes sidebar grabs a screenshot of it. Only available "
            "while recording."
        )
        self._screen_capture_btn.clicked.connect(self._on_screen_capture_toggle)
        controls.addWidget(self._screen_capture_btn)
        controls.addStretch(1)
        self._retain_checkbox = QCheckBox("Keep audio for this session", self)
        self._retain_checkbox.toggled.connect(self._on_retain_toggled)
        controls.addWidget(self._retain_checkbox)
        layout.addLayout(controls)

        # Synthesis row
        synthesis = QHBoxLayout()
        # Per-session prompt template picker. Reflects the bundled +
        # user-edited template list from the prompts folder; the
        # selection persists in the session's metadata.json. Both the
        # automation Send flow and the manual Generate dialog read
        # this as their default. Population happens via
        # set_prompt_templates() once the prompts module is loaded.
        synthesis.addWidget(QLabel("Prompt:", self))
        self._prompt_template_picker = QComboBox(self)
        self._prompt_template_picker.setToolTip(
            "Synthesis prompt template for this session. Each meeting "
            "can use a different template (e.g. one-on-one vs standup). "
            "The selection is remembered across app restarts. Edit or "
            "add templates via Settings > Open Prompts Folder."
        )
        self._prompt_template_picker.currentIndexChanged.connect(
            self._on_prompt_template_changed
        )
        # Reasonable cap on width; long template names truncate with
        # ellipsis but the dropdown shows the full text.
        self._prompt_template_picker.setMaximumWidth(200)
        synthesis.addWidget(self._prompt_template_picker)
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

        # Synthesis-in-progress banner. Shown only while a Send-to-LLM
        # call is mid-flight; hidden otherwise. Lives between the button
        # row and the tabs so it's visible no matter which tab is
        # active, and the Send button (above) is disabled in lockstep
        # so a double-click can't launch a second synthesis tab.
        self._synth_banner = QLabel("", self)
        self._synth_banner.setVisible(False)
        self._synth_banner.setStyleSheet(
            "QLabel { "
            "background: #fef3c7; "
            "color: #92400e; "
            "border: 1px solid #fbbf24; "
            "border-radius: 4px; "
            "padding: 6px 10px; "
            "font-weight: 500; "
            "}"
        )
        layout.addWidget(self._synth_banner)

        # My Notes / Synthesis / Previous Notes / Transcript. Transcript
        # is the rightmost tab as of v0.6.5: the user-curated and synthesis
        # tabs are what people read after a meeting; the raw transcript
        # is a reference rather than a starting point.
        self._tabs = QTabWidget(self)

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

        # Slides: per-session captured screenshots. Thumbnail grid +
        # full-view nav with right-click Copy / Delete / Open. Sits
        # between Synthesis and Previous Notes so reference material
        # is one tab away from both the notes-in-progress and the
        # synthesis a user is reviewing.
        self._slides_view = SlidesWidget(self)
        self._slides_view.delete_requested.connect(self._on_screenshot_delete_requested)
        self._tabs.addTab(self._slides_view, "Slides")

        # Previous Notes: list of archived synthesis versions + a
        # markdown-rendered preview of the selected one, with
        # Restore / Delete actions. Replaces the v0.6.2 plaintext
        # dump of every archive concatenated together.
        self._previous_view = PreviousNotesWidget(self)
        self._previous_view.restore_requested.connect(
            self._on_previous_restore_requested
        )
        self._previous_view.delete_requested.connect(
            self._on_previous_delete_requested
        )
        self._tabs.addTab(self._previous_view, "Previous Notes")

        # Transcript pane: editor + a playback toolbar below. Wrapped
        # in a QWidget so the QTabWidget treats them as one tab.
        transcript_page = QWidget(self)
        transcript_layout = QVBoxLayout(transcript_page)
        transcript_layout.setContentsMargins(0, 0, 0, 0)
        transcript_layout.setSpacing(4)
        self._transcript_view = _ClickableTranscriptView(transcript_page)
        self._transcript_view.setReadOnly(True)
        self._transcript_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self._transcript_view.setFont(mono)
        # An empty transcript pane could read as a bug. Set placeholder
        # text that explains the expected state: the transcript only
        # populates after the recording is stopped and the batch pass
        # has finished. With capture-only mode being the v0.6.5 default,
        # this is the normal state during a recording.
        self._transcript_view.setPlaceholderText(
            "Transcription will appear here once the recording is stopped "
            "and the post-meeting transcription pass has finished.\n\n"
            "Live transcription is off by default in v0.6.5; toggle it in "
            "Settings if you'd rather see lines arrive in real time during "
            "the meeting."
        )
        self._transcript_view.line_clicked.connect(self._on_transcript_line_clicked)
        transcript_layout.addWidget(self._transcript_view, 1)
        self._player_bar = TranscriptPlayerBar(transcript_page)
        self._player_bar.play_clicked.connect(self._on_player_bar_play_clicked)
        self._player_bar.pause_clicked.connect(self._on_player_bar_pause_clicked)
        self._player_bar.seek_ms_requested.connect(self._on_player_bar_seek_requested)
        transcript_layout.addWidget(self._player_bar, 0)
        self._tabs.addTab(transcript_page, "Transcript")
        # Per-line timestamp index. Built each time the transcript text
        # changes; consumed by the position-driven highlight and the
        # click-to-seek path. Each tuple is (start_ms, block_number).
        self._transcript_timestamps: list[tuple[int, int]] = []
        self._current_highlight_block: Optional[int] = None

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
        # Right column: screen-capture sidebar stacked above the
        # attendee-tag sidebar. Both are visible only when My Notes is
        # the active tab; _refresh_sidebar_visibility flips them as a
        # group.
        right_column = QWidget(self)
        right_column_layout = QVBoxLayout(right_column)
        right_column_layout.setContentsMargins(0, 0, 0, 0)
        right_column_layout.setSpacing(0)
        self._screencap_sidebar = ScreencapSidebar(right_column)
        self._screencap_sidebar.capture_clicked.connect(self._on_screencap_capture)
        self._screencap_sidebar.insert_clicked.connect(self._on_screencap_insert)
        right_column_layout.addWidget(self._screencap_sidebar)
        self._attendee_sidebar = AttendeeSidebar(right_column)
        self._attendee_sidebar.tag_clicked.connect(self._on_attendee_tag_clicked)
        self._attendee_sidebar.remove_last_requested.connect(
            self._on_attendee_remove_last_clicked
        )
        right_column_layout.addWidget(self._attendee_sidebar)
        right_column_layout.addStretch(1)
        self._right_column = right_column
        self._right_column.setVisible(False)
        body_row.addWidget(self._right_column, 0)
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
        # Re-evaluate whether the new session has a synthesis still
        # in flight; if not, hide the banner. (The display state is a
        # function of session_id + in-progress-id, so swapping sessions
        # always changes the banner visibility correctly.)
        if session is None or self._synth_in_progress_session_id != session.id:
            self._synth_banner.setVisible(False)
            self._synth_banner.setText("")
        else:
            self._synth_banner.setText("⏳  Waiting for Claude.ai response...")
            self._synth_banner.setVisible(True)
        if session is None:
            self._title_label.setText("(no session)")
            self._state_label.setText("")
            self._raw_transcript_text = ""
            self._transcript_view.setPlainText("")
            self._refresh_transcript_timestamps()
            self.set_player_enabled(False)
            self._notes_view.set_session_dir(None)
            self._set_notes_text("")
            self._previous_view.set_session_id("")
            self._previous_view.set_archives([])
            self._live_notes_editor.set_session_dir(None)
            self._set_live_notes_text("")
            self._retain_checkbox.setChecked(False)
            self._retain_checkbox.setEnabled(False)
            self._set_buttons_for_state(STATE_NEW, has_transcript=False, has_notes=False)
            # Clear sidebar state on session deselect; counts will be
            # re-seeded by the controller on the next select.
            self._attendee_sidebar.set_counts({})
            self._right_column.setVisible(False)
            self._slides_view.set_screenshots([])
            self.set_screencap_armed(False)
            return
        self._title_label.setText(session.title)
        self._state_label.setText(_pretty_state(session.state))
        self._raw_transcript_text = transcript
        self._transcript_view.setPlainText(rewrite_user_label(transcript, self._user_name))
        self._refresh_transcript_timestamps()
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
        self._previous_view.set_session_id(session.id)
        self._previous_view.set_archives(previous_notes_paths)
        # Seed the Slides tab. MainApp also pushes updates here after
        # each successful capture / insert / delete via
        # refresh_screenshots() so the grid stays current.
        self.refresh_screenshots()
        # Switching to a different session drops any armed capture
        # state; the region the user drew in one meeting doesn't carry
        # into another.
        self.set_screencap_armed(False)
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
        Transcript or My Notes. Hides on Synthesis / Previous Notes /
        Slides even mid-recording -- those tabs are read-only review
        surfaces. The right column wraps both the screencap sidebar
        and the attendee sidebar; they show / hide together so the
        column doesn't shrink to just one widget mid-recording."""
        if self._session is None or self._session.state not in (
            STATE_RECORDING, STATE_PAUSED,
        ):
            self._right_column.setVisible(False)
            return
        current = self._tabs.currentWidget()
        on_transcript_or_notes = current in (
            self._transcript_view, self._live_notes_editor,
        )
        self._right_column.setVisible(on_transcript_or_notes)
        # The screencap sidebar belongs to My Notes only; hide it on
        # the Transcript tab even though the column is shown for the
        # attendee tag controls.
        self._screencap_sidebar.setVisible(current is self._live_notes_editor)

    # ---- screen-capture API used by MainApp ------------------------------

    def set_screencap_armed(self, armed: bool) -> None:
        """Flip the toggle button + sidebar enable state.

        MainApp calls this after the user confirms a region (-> True)
        or clicks Stop Screen Capture / the recording ends (-> False).
        Keeping the visible state in one place avoids the toggle button
        and the sidebar drifting out of sync.
        """
        self._screencap_armed = armed
        self._screencap_sidebar.set_armed(armed)
        self._screen_capture_btn.setText(
            "Stop Screen Capture" if armed else "Start Screen Capture"
        )
        self._refresh_screencap_button_enabled()

    def is_screencap_armed(self) -> bool:
        return self._screencap_armed

    def refresh_screenshots(self) -> None:
        """Reload the Slides tab from disk for the current session."""
        if self._session is None:
            self._slides_view.set_screenshots([])
            return
        from ..utils.paths import list_screenshots  # noqa: PLC0415
        self._slides_view.set_screenshots(list_screenshots(self._session.id))

    def insert_screenshot_markdown(self, relative_path: str) -> None:
        """Drop an image-ref into My Notes at the current cursor.

        Called by MainApp after a successful Insert: the screenshot has
        landed on disk and the editor needs the markdown link so the
        Preview shows the captured image inline with the surrounding
        notes. relative_path is anchored at the session dir so it
        round-trips through the editor's setSearchPaths.
        """
        ref = f"![screenshot]({relative_path})\n"
        # toPlainText() returns the source-mode text; insert via the
        # editor's QTextCursor so undo/redo work like a typed paste.
        editor = self._live_notes_editor._editor  # noqa: SLF001
        cursor = editor.textCursor()
        cursor.insertText(ref)
        editor.setTextCursor(cursor)

    def _on_screen_capture_toggle(self) -> None:
        if self._session is None:
            return
        if self._screencap_armed:
            self.stop_screen_capture_clicked.emit(self._session.id)
        else:
            self.start_screen_capture_clicked.emit(self._session.id)

    def _on_screencap_capture(self) -> None:
        if self._session is None:
            return
        self.screencap_capture_clicked.emit(self._session.id)

    def _on_screencap_insert(self) -> None:
        if self._session is None:
            return
        self.screencap_insert_clicked.emit(self._session.id)

    def _on_screenshot_delete_requested(self, path: Path) -> None:
        if self._session is None:
            return
        self.delete_screenshot_clicked.emit(self._session.id, path)

    def _refresh_screencap_button_enabled(self) -> None:
        """Enabled only while RECORDING or PAUSED, OR while armed.

        The 'OR armed' branch is the Stop-Screen-Capture-after-recording
        edge case: if the user hit Stop before disarming, the toggle
        still needs to be clickable so they can disarm it cleanly.
        """
        if self._session is None:
            self._screen_capture_btn.setEnabled(False)
            return
        live = self._session.state in (STATE_RECORDING, STATE_PAUSED)
        self._screen_capture_btn.setEnabled(live or self._screencap_armed)

    # ---- transcript playback API used by MainApp -------------------------

    def set_player_enabled(self, enabled: bool) -> None:
        """Master enable for the player bar.

        MainApp calls this with True when the active session has
        retained audio (the player has something to play) and False
        otherwise.
        """
        self._player_bar.set_enabled_state(enabled)
        if not enabled:
            self._clear_transcript_highlight()

    def set_player_total_ms(self, total_ms: int) -> None:
        self._player_bar.set_total_ms(total_ms)

    def set_player_position_ms(self, ms: int) -> None:
        self._player_bar.set_position_ms(ms)
        # Highlight the transcript line that owns this timestamp. We
        # do this on every position update so the highlight follows
        # playback in real time. Skip when the user is mid-drag --
        # otherwise the highlight thrashes around the slider thumb.
        if self._player_bar.is_user_dragging():
            return
        self._refresh_transcript_highlight(ms)

    def set_player_is_playing(self, playing: bool) -> None:
        self._player_bar.set_is_playing(playing)

    def _on_player_bar_play_clicked(self) -> None:
        if self._session is None:
            return
        self.transcript_play_clicked.emit(self._session.id)

    def _on_player_bar_pause_clicked(self) -> None:
        if self._session is None:
            return
        self.transcript_pause_clicked.emit(self._session.id)

    def _on_player_bar_seek_requested(self, ms: int) -> None:
        if self._session is None:
            return
        self.transcript_seek_ms_requested.emit(self._session.id, int(ms))

    def _on_transcript_line_clicked(self, block_number: int) -> None:
        if self._session is None:
            return
        start_ms = _start_ms_for_block(self._transcript_timestamps, block_number)
        if start_ms is None:
            return
        # Seek a few seconds before the clicked line so the listen-back
        # catches the lead-in (Aaron's "~10s before that line").
        target = max(0, int(start_ms) - _TRANSCRIPT_SEEK_LEAD_MS)
        self.transcript_seek_ms_requested.emit(self._session.id, target)

    def _refresh_transcript_timestamps(self) -> None:
        """Rebuild the timestamp index from the transcript view text.

        Called whenever set_transcript_text / set_session updates the
        displayed text. Click-to-seek and position-driven highlight
        both consume this list.
        """
        text = self._transcript_view.toPlainText()
        self._transcript_timestamps = _parse_transcript_timestamps(text)
        self._clear_transcript_highlight()

    def _refresh_transcript_highlight(self, position_ms: int) -> None:
        block_number = _block_for_position_ms(
            self._transcript_timestamps, position_ms,
        )
        if block_number is None:
            self._clear_transcript_highlight()
            return
        if block_number == self._current_highlight_block:
            return
        self._current_highlight_block = block_number
        # Build a single ExtraSelection that highlights the entire
        # block. The selection's format paints behind the text without
        # disrupting the cursor (the view is read-only anyway).
        from PyQt6.QtWidgets import QTextEdit  # noqa: PLC0415
        sel = QTextEdit.ExtraSelection()
        fmt = QTextCharFormat()
        fmt.setBackground(QColor(255, 240, 160))
        fmt.setProperty(QTextCharFormat.Property.FullWidthSelection, True)
        sel.format = fmt
        cursor = QTextCursor(self._transcript_view.document().findBlockByNumber(block_number))
        cursor.clearSelection()
        sel.cursor = cursor
        self._transcript_view.setExtraSelections([sel])
        # Scroll the highlight into view if it's offscreen.
        view_cursor = self._transcript_view.textCursor()
        view_cursor.setPosition(cursor.position())
        self._transcript_view.setTextCursor(view_cursor)
        self._transcript_view.ensureCursorVisible()

    def _clear_transcript_highlight(self) -> None:
        self._current_highlight_block = None
        self._transcript_view.setExtraSelections([])

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
        # New line means a new candidate timestamp for the click-to-
        # seek index. Cheap to rebuild on every append (the regex is
        # one match per line); the index is small.
        self._refresh_transcript_timestamps()

    def append_provisional(self, segment: TranscriptSegment) -> None:
        """Append a provisional segment that may be rewritten when the next overlap arrives."""
        # For v0.2 we currently treat provisional as final; future revisions can store the
        # line index in self._provisional_segments and overwrite on update.
        self.append_segment(segment)

    def set_transcript_text(self, text: str) -> None:
        """Replace the transcript view's contents. `text` should be the raw on-disk form."""
        self._raw_transcript_text = text
        self._transcript_view.setPlainText(rewrite_user_label(text, self._user_name))
        self._refresh_transcript_timestamps()

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
        self._refresh_transcript_timestamps()

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
        self._previous_view.set_archives(paths)

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
            # Optimistically mark in-progress so a double-click can't
            # fire two synthesis tabs. The controller will call
            # set_synthesis_in_progress with the same id; idempotent.
            self.set_synthesis_in_progress(self._session.id, True, status_text=None)
            self.send_to_llm_clicked.emit(
                self._session.id, self._automation_target
            )

    def set_synthesis_in_progress(
        self, session_id: str, in_progress: bool, *, status_text: Optional[str] = None
    ) -> None:
        """Track + display whether a synthesis-automation call is mid-
        flight for ``session_id``.

        While in progress: shows the yellow banner above the tabs,
        disables the Send button (preventing parallel tabs from a
        double-click), and updates the banner text if ``status_text``
        is provided (used by the controller to surface "pasting",
        "response streaming", etc as they come back from the bridge).

        Off: clears the banner + re-enables Send.

        The state is keyed by session_id so switching away to another
        session and back doesn't lose the indicator -- MainApp keeps
        the tracking; SessionView only renders for the currently-
        displayed session.
        """
        if in_progress:
            self._synth_in_progress_session_id = session_id
        elif self._synth_in_progress_session_id == session_id:
            self._synth_in_progress_session_id = None
        # Only render if the in-progress session is the one currently
        # displayed. Switching to a different session while a synthesis
        # is mid-flight elsewhere should not show the banner here.
        showing_this = (
            self._session is not None and self._session.id == session_id
        )
        if showing_this and self._synth_in_progress_session_id == session_id:
            text = status_text or "Waiting for Claude.ai response..."
            self._synth_banner.setText(f"⏳  {text}")
            self._synth_banner.setVisible(True)
            self._send_btn.setEnabled(False)
        else:
            # Either we're not the displayed session, or the synthesis
            # finished -- hide the banner and let the next state
            # refresh re-enable the Send button.
            self._synth_banner.setVisible(False)
            self._synth_banner.setText("")
            if self._session is not None:
                self._set_buttons_for_state(
                    self._session.state,
                    has_transcript=bool(self._raw_transcript_text),
                    has_notes=bool(self._session.has_notes),
                )

    def set_prompt_templates(self, template_names: list[str], selected: str = "") -> None:
        """Populate the prompt template picker.

        ``template_names`` should be the list of available templates
        (from prompts module). ``selected`` is the currently-saved
        choice for this session ("" = use default). Caller is
        expected to compute that from session metadata before invoking.

        Block signals during population so the currentIndexChanged
        emit doesn't fire spurious save events at app-startup.
        """
        self._prompt_template_picker.blockSignals(True)
        self._prompt_template_picker.clear()
        # First entry is always "(default)" -- empty string in data
        # role -- so leaving the picker untouched on a new session
        # uses the default template without forcing the user to pick.
        self._prompt_template_picker.addItem("(default)", "")
        for name in template_names:
            if not name or name == "default":
                # We surface the bundled default via the "(default)"
                # entry above; skip the literal "default" template
                # name to avoid the user seeing two entries that
                # mean the same thing.
                continue
            self._prompt_template_picker.addItem(name, name)
        # Restore selection.
        target_idx = 0  # (default)
        for i in range(self._prompt_template_picker.count()):
            if self._prompt_template_picker.itemData(i) == selected:
                target_idx = i
                break
        self._prompt_template_picker.setCurrentIndex(target_idx)
        self._prompt_template_picker.blockSignals(False)

    def selected_prompt_template(self) -> str:
        """The currently-selected template's data value (empty == default)."""
        return self._prompt_template_picker.currentData() or ""

    def _on_prompt_template_changed(self, _idx: int) -> None:
        if self._session is None:
            return
        name = self._prompt_template_picker.currentData() or ""
        self.prompt_template_changed.emit(self._session.id, name)

    def set_synthesis_connection_state(self, state) -> None:
        """Update the SessionView's view of the synthesis connection
        state. MainApp's poll loop calls this every 5 seconds plus on
        bridge connect/disconnect transitions. Re-evaluates the Send
        button enable state immediately."""
        self._synth_connection_state = state
        if self._session is not None:
            self._set_buttons_for_state(
                self._session.state,
                has_transcript=bool(self._raw_transcript_text),
                has_notes=bool(self._session.has_notes),
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
            # The new PreviousNotesWidget renders one archive at a time
            # in a QTextBrowser preview; expose that as the "tab text"
            # for Copy / Print etc. Returns empty string if no archive
            # is selected, which is fine -- Copy is a no-op then.
            try:
                return self._previous_view._preview.toPlainText()  # noqa: SLF001
            except AttributeError:
                return ""
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

    def _on_previous_restore_requested(self, session_id: str, path: Path) -> None:
        self.restore_previous_notes_clicked.emit(session_id, path)

    def _on_previous_delete_requested(self, session_id: str, path: Path) -> None:
        self.delete_previous_notes_clicked.emit(session_id, path)

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
        self._refresh_screencap_button_enabled()
        # Generate/paste are available as soon as a transcript exists. The
        # batch-refinement pass after Stop runs in the background and is
        # explicitly NOT a gate on synthesis -- the live transcript is good
        # enough to act on, and any later regenerate will pick up the
        # refined version automatically.
        can_synthesize = has_session and has_transcript and not is_recording
        self._generate_btn.setEnabled(can_synthesize)
        self._paste_btn.setEnabled(has_session and (has_transcript or has_notes) and not is_recording)
        # Send button: gated by FOUR conditions. All must be true.
        #
        #   1. There's a transcript to synthesize (can_synthesize).
        #   2. The target LLM has a working content-script adapter
        #      (Copilot is stub-only in v0.6.3 so its target.implemented
        #      is False; Claude is True).
        #   3. There's no synthesis already in flight for THIS session
        #      (prevents double-click → two tabs).
        #   4. The connection state allows it: NOT_RUNNING (we'll
        #      launch Chrome on click) or RUNNING_CONNECTED (normal
        #      flow). RUNNING_DISCONNECTED disables because the
        #      extension is broken and a click would just fail.
        if self._automation_enabled and self._automation_target:
            from ..automation.targets import get_target

            try:
                implemented = get_target(self._automation_target).implemented
            except ValueError:
                implemented = False
            self_id = self._session.id if self._session else ""
            in_progress_here = (
                self._synth_in_progress_session_id is not None
                and self._synth_in_progress_session_id == self_id
            )
            connection_ok = (
                self._synth_connection_state is None
                or self._synth_connection_state.send_button_enabled()
            )
            self._send_btn.setEnabled(
                can_synthesize
                and implemented
                and not in_progress_here
                and connection_ok
            )
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
        doc.clamp_images_to_printer(printer)
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
            doc.clamp_images_to_printer(printer)
            doc.print(printer)
        except Exception as exc:
            QMessageBox.warning(self, "Export PDF", f"Could not write PDF: {exc}")
            return
        self.window().statusBar().showMessage(
            f"Exported PDF to {target.name}", 5000
        )


_TIMESTAMP_RE = re.compile(r"^\[(\d+):(\d{2}):(\d{2})\]")


def _parse_transcript_timestamps(text: str) -> list[tuple[int, int]]:
    """Return [(start_ms, block_number), ...] for every line whose
    leading bracket is a HH:MM:SS timestamp.

    Lines without a leading timestamp (status messages, blank lines)
    are silently skipped; they don't contribute a seek anchor. The
    list is monotonic in start_ms because the transcript writer
    emits segments in chronological order; we rely on that to do
    O(log N) bisect lookups.
    """
    out: list[tuple[int, int]] = []
    for block_number, line in enumerate(text.splitlines()):
        match = _TIMESTAMP_RE.match(line)
        if match is None:
            continue
        hours, minutes, seconds = (int(g) for g in match.groups())
        start_ms = ((hours * 3600) + (minutes * 60) + seconds) * 1000
        out.append((start_ms, block_number))
    return out


def _block_for_position_ms(
    timestamps: list[tuple[int, int]], position_ms: int,
) -> Optional[int]:
    """Return the block number whose timestamp the position falls in.

    Bisect for the rightmost segment whose start_ms <= position_ms.
    Returns None if the timestamps list is empty or the position
    precedes every segment.
    """
    if not timestamps:
        return None
    keys = [t[0] for t in timestamps]
    idx = bisect.bisect_right(keys, position_ms) - 1
    if idx < 0:
        return None
    return timestamps[idx][1]


def _start_ms_for_block(
    timestamps: list[tuple[int, int]], block_number: int,
) -> Optional[int]:
    """Inverse: return the timestamp anchored at this block, or None."""
    for ms, blk in timestamps:
        if blk == block_number:
            return ms
    return None


class _ClickableTranscriptView(QPlainTextEdit):
    """QPlainTextEdit that emits line_clicked(block_number) on click.

    Distinct from a regular cursor selection: the user clicking a
    line in the transcript pane is a seek action, not text-selection.
    We let the parent dispatch the click through the normal
    mousePressEvent first so the cursor still moves; then emit a
    signal so the SessionView can compute the seek target.
    """

    line_clicked = pyqtSignal(int)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        super().mousePressEvent(event)
        if event.button() != Qt.MouseButton.LeftButton:
            return
        cursor = self.cursorForPosition(event.pos())
        block = cursor.block()
        if block.isValid():
            self.line_clicked.emit(block.blockNumber())


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
