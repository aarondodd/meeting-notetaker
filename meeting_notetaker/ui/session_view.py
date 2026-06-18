"""Per-session four-pane view: transcript + my-notes + synthesis + previous-notes."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import bisect
import logging
import re

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QPlainTextEdit,
    QToolButton,
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
from ..utils.export import (
    build_print_markdown,
    default_export_document_title,
    default_export_filename,
    export_initial_save_path,
)
from ..utils.paths import session_dir
from ..models.highlights import HighlightSet
from .attachments_tab import AttachmentsTab
from .appendix_tray import AppendixTray
from .attendee_details_drawer import AttendeeDetailsDrawer
from .attendee_sidebar import AttendeeSidebar
from .classification_bar import ClassificationBar
from .find_bar import FindBar
from .highlight_bar import HighlightBar
from .live_notes_widget import LiveNotesWidget
from .previous_notes_widget import PreviousNotesWidget
from .scaled_image_label import ScaledImageLabel
from .screencap_sidebar import ScreencapSidebar
from .slides_widget import SlidesWidget
from .transcript_player_bar import TranscriptPlayerBar


log = logging.getLogger(__name__)


# How many milliseconds before the clicked transcript line to seek to.
# Aaron asked for "just before that line's timestamp (~10s)" so the
# listen-back catches the lead-in. Pulled out so it's easy to tune.
_TRANSCRIPT_SEEK_LEAD_MS = 10_000


class SessionView(QWidget):
    """Right-hand pane shown when a session is selected."""

    start_clicked = pyqtSignal(str)               # session_id
    # pause_clicked / resume_clicked dropped in v0.6.5 -- the
    # session recording is now a fixed Start -> Stop block.
    stop_clicked = pyqtSignal(str)
    generate_prompt_clicked = pyqtSignal(str)
    paste_notes_clicked = pyqtSignal(str)
    # Synthesis Automation: emitted instead of the Generate/Paste pair
    # when the user has the feature enabled in Settings. Carries the
    # session id and the LLM target key ("claude" / "copilot").
    send_to_llm_clicked = pyqtSignal(str, str)      # session_id, target
    # Issue #90: sidekick button that opens the SessionPromptEditDialog
    # with the rendered prompt for one-shot editing before dispatch.
    # Same enable gate as the Send/Generate pair; MainApp does the
    # render + dialog + downstream routing in one handler. Carries the
    # session id; the target (claude / copilot) is resolved by MainApp
    # from the automation toggle the same way send_to_llm_clicked does.
    edit_and_send_clicked = pyqtSignal(str)         # session_id
    # Issue #80: empty-state affordance on the Transcript tab. MainApp
    # opens the ImportTranscriptDialog and writes the result to
    # raw.transcript.md. Carries the session id for symmetry with the
    # other per-session signals.
    import_transcript_clicked = pyqtSignal(str)    # session_id
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
    # Issue #79: experimental Notion / Confluence export. Each signal
    # fires with (session_id, tab_label, markdown_body) so MainApp
    # owns the picker dialog + worker thread + URL-open without
    # SessionView importing the integration modules.
    export_to_notion_requested = pyqtSignal(str, str, str)
    export_to_confluence_requested = pyqtSignal(str, str, str)
    export_to_obsidian_requested = pyqtSignal(str, str, str)
    # Emitted just before _right_column.setVisible(...) toggles, with
    # the upcoming visibility as the payload (#102 bug 3 follow-up).
    # MainWindow uses it to checkpoint the main splitter sizes BEFORE
    # the toggle triggers a layout redistribute, then restores them on
    # the next event-loop tick so the user's preferred split survives
    # Start Recording.
    right_column_will_toggle = pyqtSignal(bool)
    # Click-to-tag for in-meeting speaker anchoring. The sidebar emits
    # (session_id, name) per click; the controller persists a SpeakerTag
    # and the post-meeting refiner uses tags to constrain the clusterer.
    tag_speaker_clicked = pyqtSignal(str, str)            # session_id, name
    remove_last_tag_clicked = pyqtSignal(str, str)        # session_id, name
    # Classification chip-row signals (v0.7.0+); MainApp persists the
    # mutations via ClassificationStore + repaints the bar.
    add_topic_requested = pyqtSignal(str, str)            # session_id, name
    remove_topic_requested = pyqtSignal(str, int)         # session_id, topic_id
    accept_topic_requested = pyqtSignal(str, int)         # session_id, topic_id
    set_series_requested = pyqtSignal(str, str)           # session_id, series_name ("" clears)
    # Highlight bar mutations (v0.7.0+). The bar carries the whole
    # HighlightSet to keep the signal-fired writes atomic.
    highlights_changed = pyqtSignal(str, object)          # session_id, HighlightSet
    # Attachments tab forwards (issue #29) -- MainApp persists +
    # refreshes derived state on change.
    attachments_changed = pyqtSignal(str)                 # session_id
    # User clicked Edit... on the Appendix tray; MainApp opens the
    # AppendixEditDialog. Kept as a signal so the tray + dialog
    # imports don't leak into session_view's import surface.
    appendix_edit_requested = pyqtSignal(str)             # session_id
    attachments_split_changed = pyqtSignal(str)           # session_id, base64 splitter state forwarded via signal
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
    screencap_auto_toggled = pyqtSignal(str, bool)        # session_id, enabled
    # v0.7.2 (issue #51 Phase 3): user clicked a contact in the
    # attendee-details drawer; MainApp opens the Address Book
    # filtered to that contact.
    contact_clicked_in_drawer = pyqtSignal(int)           # contact_id
    # Right-click on a Slides thumbnail / full view: delete the file.
    # session_id, list[Path]. List shape (#110) so the Slides grid's
    # multi-select Delete sends one signal carrying every selected
    # path; single-image full-view callers wrap with [path].
    delete_screenshot_clicked = pyqtSignal(str, list)
    # Transcript-pane playback control. The bar fires these for the
    # session id MainApp tracks; the seek signal also fires when the
    # user clicks a transcript line (with the line's start - 10s).
    transcript_play_clicked = pyqtSignal(str)             # session_id
    transcript_pause_clicked = pyqtSignal(str)            # session_id
    transcript_seek_ms_requested = pyqtSignal(str, int)   # session_id, ms
    # User dragged the Transcript playback splitter; MainApp persists
    # the new top-pane percentage (10-90) to Config.
    transcript_playback_split_changed = pyqtSignal(int)   # top_pct

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._session: Optional[Session] = None
        # #109: background export state. Same shape per target so a
        # second click no-ops (status bar hint) rather than piling
        # up. Worker reference held so we can wait() + deleteLater()
        # after finish.
        self._pdf_export_in_flight: bool = False
        self._pdf_export_worker = None  # type: Optional[_PdfExportWorker]
        self._word_export_in_flight: bool = False
        self._word_export_worker = None  # type: Optional[_WordExportWorker]
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
        # Cached session-contact list for the PDF rich-table option
        # (issue #51 Phase 5). Populated by set_session_contacts;
        # _build_print_document reads it to decide whether to swap
        # the bullet list with a Markdown table.
        self._session_contacts: list = []
        # User's Settings-saved AppendixInclusion defaults for the
        # per-tab Export PDF + Print flow. None -> dialog uses
        # Settings > Export default folder (v0.7.5). MainApp pushes
        # the config string here via set_export_default_folder; the
        # per-tab Export PDF flow uses it as the dialog's initial
        # location. Empty == no default configured (legacy fallback
        # to the session dir).
        self._export_default_folder: str = ""
        # "every populated section on". MainApp.set_appendix_export_defaults
        # plumbs the saved config into this field.
        self._appendix_export_defaults = None
        # #92 outline transforms. MainApp pushes the config values via
        # set_export_outline_options on startup + Settings save.
        self._export_heading_numbering: bool = False
        self._export_toc: bool = False
        self._export_toc_max_depth: int = 3
        # #94: when True AND Word COM is available, "Save as PDF..."
        # routes through Word for native TOC + bookmarks instead of
        # Qt's PDF backend + pypdf post-process.
        self._use_word_for_pdf: bool = False
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
        # v0.7.0 UI tweak: one button that toggles between Start /
        # Stop based on session state. Matches the convention used
        # elsewhere (the Screen Capture button does the same).
        # Disabled when the session already has a recording on disk
        # to prevent the user from accidentally overwriting it.
        self._record_btn = QPushButton("Start Recording", self)
        self._record_btn.clicked.connect(self._on_record_toggle)
        controls.addWidget(self._record_btn)
        # Pause + Resume were removed in v0.6.5 to keep recordings
        # wall-clock-continuous. With pause, mic.wav and sys.wav
        # could go out of sync (especially under WASAPI loopback,
        # which delivers samples idiosyncratically when no audio is
        # playing). The recording is now a fixed start -> stop block
        # with all silences / padding preserved.
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
        self._retain_checkbox = QCheckBox("Keep audio for this session", self)
        self._retain_checkbox.toggled.connect(self._on_retain_toggled)
        controls.addWidget(self._retain_checkbox)
        controls.addStretch(1)
        # v0.7.0 tweak #8: classification chips/buttons live on the
        # control row, to the right of Start/Screen Capture. The bar
        # widget itself is constructed later in __init__; we insert
        # a placeholder slot here and the actual addWidget call lives
        # right after construction.
        self._classification_bar_slot = controls
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
        # Split-button: main click runs the standard Generate /
        # Send action; the dropdown arrow exposes the "Edit prompt
        # before sending" path (#90). One button instead of a
        # sidekick keeps the row tight and matches the platform
        # convention (Save / Save As..., etc.). Same enable gate as
        # the standard click.
        self._generate_btn = QToolButton(self)
        self._generate_btn.setText("Generate Synthesis Prompt")
        self._generate_btn.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextOnly,
        )
        self._generate_btn.setPopupMode(
            QToolButton.ToolButtonPopupMode.MenuButtonPopup,
        )
        self._generate_btn.clicked.connect(self._on_generate_prompt)
        self._generate_menu = QMenu(self._generate_btn)
        self._generate_edit_action = self._generate_menu.addAction(
            "Edit prompt before generating...",
        )
        self._generate_edit_action.triggered.connect(self._on_edit_and_send)
        self._generate_btn.setMenu(self._generate_menu)
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
        # Same split-button shape as Generate: main click sends, the
        # dropdown arrow exposes the edit-before-send path.
        self._send_btn = QToolButton(self)
        self._send_btn.setText("Send to Claude.ai")
        self._send_btn.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextOnly,
        )
        self._send_btn.setPopupMode(
            QToolButton.ToolButtonPopupMode.MenuButtonPopup,
        )
        self._send_btn.setToolTip(
            "Send the synthesis prompt to the configured web LLM via "
            "the Meeting Notetaker browser extension. The response "
            "lands in the Synthesis tab automatically. Use the "
            "dropdown to edit the rendered prompt before sending."
        )
        self._send_btn.clicked.connect(self._on_send_to_llm)
        self._send_menu = QMenu(self._send_btn)
        self._send_edit_action = self._send_menu.addAction(
            "Edit prompt before sending...",
        )
        self._send_edit_action.triggered.connect(self._on_edit_and_send)
        self._send_btn.setMenu(self._send_menu)
        self._send_btn.setVisible(False)
        synthesis.addWidget(self._send_btn)
        self._copy_btn = QPushButton("Copy", self)
        self._copy_btn.setToolTip(
            "Copy the active tab's contents to the clipboard. The button "
            "label updates to reflect which tab is active."
        )
        self._copy_btn.clicked.connect(self._on_copy_active_tab)
        synthesis.addWidget(self._copy_btn)
        # Find-in-tab. Mirrors the Ctrl+F shortcut so a mouse-only
        # user has a visible affordance. Wired directly to the
        # same handler; if the active tab has no searchable text
        # (Slides) the handler no-ops cleanly.
        self._find_btn = QPushButton("Find...", self)
        self._find_btn.setToolTip(
            "Search within the active tab (Ctrl+F)"
        )
        self._find_btn.clicked.connect(self._open_find_bar)
        synthesis.addWidget(self._find_btn)
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
        # Issue #79: unified Save to... button. PDF always available;
        # Notion + Confluence menu items appear only when their
        # respective integrations are configured + verified. Button
        # label switched from "Export..." to "Save to..." on 2026-06-03
        # to match how users think about the destinations -- PDF is
        # "save as" a file format, Notion / Confluence are "save to"
        # a remote destination.
        self._export_btn = QToolButton(self)
        self._export_btn.setText("Save to...")
        self._export_btn.setToolTip(
            "Save the active tab (My Notes or Synthesis) as a PDF or "
            "to Notion / Confluence."
        )
        self._export_btn.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup,
        )
        self._export_menu = QMenu(self._export_btn)
        self._export_pdf_action = self._export_menu.addAction("Save as PDF...")
        self._export_pdf_action.triggered.connect(self._on_export_pdf)
        # #94: Save as Word (.docx). Inserts Word's native TOC field
        # that auto-populates the first time the user opens the file
        # in Word. Always visible -- the docx itself is cross-
        # platform, only the optional TOC-population step needs Word.
        self._export_word_action = self._export_menu.addAction(
            "Save as Word..."
        )
        self._export_word_action.triggered.connect(self._on_export_word)
        # Notion + Confluence actions; visibility re-evaluated whenever
        # the session changes or settings are saved.
        self._export_notion_action = self._export_menu.addAction(
            "Save to Notion..."
        )
        self._export_notion_action.triggered.connect(self._on_export_notion)
        self._export_confluence_action = self._export_menu.addAction(
            "Save to Confluence..."
        )
        self._export_confluence_action.triggered.connect(self._on_export_confluence)
        self._export_obsidian_action = self._export_menu.addAction(
            "Save to Obsidian..."
        )
        self._export_obsidian_action.triggered.connect(self._on_export_obsidian)
        # Hidden by default; MainApp toggles via set_integration_targets().
        self._export_notion_action.setVisible(False)
        self._export_confluence_action.setVisible(False)
        self._export_obsidian_action.setVisible(False)
        self._export_btn.setMenu(self._export_menu)
        synthesis.addWidget(self._export_btn)
        # Legacy alias so older test references to _export_pdf_btn don't
        # break -- the menu action carries the same handler.
        self._export_pdf_btn = self._export_btn
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
        # v0.7.2 (issue #51 Phase 3): wrap the My Notes editor in a
        # page that carries the attendee-details drawer above it.
        # The page becomes the tab widget; tab-comparison sites
        # below use _live_notes_page (the wrapper) rather than the
        # editor directly.
        self._live_notes_drawer = AttendeeDetailsDrawer(self)
        self._live_notes_drawer.contact_clicked.connect(self._on_drawer_contact_clicked)
        # Issue #64: Appendix tray below the editor mirrors the
        # attendee drawer pattern. Surfaces every auto-extracted
        # appendix (attendee details, topics, context, referenced
        # attachments) plus links and session attachments.
        self._live_notes_appendix = AppendixTray(self)
        self._live_notes_appendix.edit_requested.connect(
            self._on_appendix_edit_requested,
        )
        self._live_notes_page = QWidget(self)
        live_notes_layout = QVBoxLayout(self._live_notes_page)
        live_notes_layout.setContentsMargins(0, 0, 0, 0)
        live_notes_layout.setSpacing(2)
        live_notes_layout.addWidget(self._live_notes_drawer)
        live_notes_layout.addWidget(self._live_notes_editor, 1)
        live_notes_layout.addWidget(self._live_notes_appendix)
        self._tabs.addTab(self._live_notes_page, "My Notes")

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
        # Same wrap pattern for the Synthesis tab; the drawer
        # appears above the LLM-synthesized notes editor too.
        self._notes_drawer = AttendeeDetailsDrawer(self)
        self._notes_drawer.contact_clicked.connect(self._on_drawer_contact_clicked)
        # Same Appendix tray pattern below the Synthesis editor.
        self._notes_appendix = AppendixTray(self)
        self._notes_appendix.edit_requested.connect(
            self._on_appendix_edit_requested,
        )
        self._notes_page = QWidget(self)
        notes_layout = QVBoxLayout(self._notes_page)
        notes_layout.setContentsMargins(0, 0, 0, 0)
        notes_layout.setSpacing(2)
        notes_layout.addWidget(self._notes_drawer)
        notes_layout.addWidget(self._notes_view, 1)
        notes_layout.addWidget(self._notes_appendix)
        self._tabs.addTab(self._notes_page, "Synthesis")

        # Slides: per-session captured screenshots. Thumbnail grid +
        # full-view nav with right-click Copy / Delete / Open. Sits
        # between Synthesis and Previous Notes so reference material
        # is one tab away from both the notes-in-progress and the
        # synthesis a user is reviewing.
        self._slides_view = SlidesWidget(self)
        self._slides_view.delete_requested.connect(self._on_screenshot_delete_requested)
        # The Slides tab carries its own player bar. Forward its
        # signals up so MainApp's existing transcript_* handlers wire
        # one player to both bars.
        self._slides_view.play_clicked.connect(self._on_slides_play_clicked)
        self._slides_view.pause_clicked.connect(self._on_slides_pause_clicked)
        self._slides_view.seek_ms_requested.connect(self._on_slides_seek_requested)
        # Tab labelled "Screen Captures" for consistency with the
        # "Start Screen Capture" button + the screencap_sidebar UI.
        # Internal references in code still say "slides" (slot name,
        # _slides_view widget) -- that's just shorthand; the
        # user-facing string is what matters.
        self._tabs.addTab(self._slides_view, "Screen Captures")

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

        # Transcript pane has two layouts living in a QStackedWidget:
        #
        # Idle layout (default + paused):
        #   transcript editor full-width (click-to-seek + highlight)
        #
        # Playback layout (active while audio is playing OR position > 0
        # with screenshots present):
        #   QSplitter vertical:
        #     ScaledImageLabel showing the current screenshot
        #     transcript editor (re-parented from the idle page)
        #
        # The same _ClickableTranscriptView instance lives in both
        # layouts via re-parenting; that keeps the highlight + scroll
        # state intact as the layout flips. The player bar sits below
        # both layouts and is shared too. The vertical splitter's
        # default split is 70/30 (top/bottom) but the user can drag
        # the handle; the new percentage is emitted via
        # transcript_playback_split_changed so MainApp can persist it.
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
        self._transcript_view.setPlaceholderText(
            "Transcription will appear here once the recording is stopped "
            "and the post-meeting transcription pass has finished.\n\n"
            "Live transcription is off by default in v0.6.5; toggle it in "
            "Settings if you'd rather see lines arrive in real time during "
            "the meeting."
        )
        self._transcript_view.line_clicked.connect(self._on_transcript_line_clicked)

        # Idle layout: full-width transcript editor. The editor is
        # re-parented out into the playback splitter when playback
        # engages, so the placeholder reserves its slot here.
        self._transcript_idle_page = QWidget(transcript_page)
        idle_layout = QVBoxLayout(self._transcript_idle_page)
        idle_layout.setContentsMargins(0, 0, 0, 0)
        idle_layout.setSpacing(0)
        idle_layout.addWidget(self._transcript_view, 1)
        self._idle_editor_placeholder = QWidget(self._transcript_idle_page)
        self._idle_editor_placeholder.hide()
        idle_layout.addWidget(self._idle_editor_placeholder, 0)

        # Playback layout: image on top, transcript below.
        self._transcript_playback_page = QWidget(transcript_page)
        playback_layout = QVBoxLayout(self._transcript_playback_page)
        playback_layout.setContentsMargins(0, 0, 0, 0)
        playback_layout.setSpacing(0)
        self._transcript_playback_splitter = QSplitter(
            Qt.Orientation.Vertical, self._transcript_playback_page,
        )
        self._transcript_playback_splitter.setChildrenCollapsible(False)
        self._playback_image = ScaledImageLabel(self._transcript_playback_splitter)
        self._transcript_playback_splitter.addWidget(self._playback_image)
        # The editor is re-parented into this splitter when we swap
        # to playback layout. We add a placeholder so the splitter's
        # second slot is reserved; _enter_playback_layout swaps the
        # real editor in.
        self._playback_editor_placeholder = QWidget(self._transcript_playback_splitter)
        self._transcript_playback_splitter.addWidget(self._playback_editor_placeholder)
        # Stretch factors are a fallback if setSizes hasn't been
        # called yet (e.g. before the first _enter_playback_layout);
        # _apply_playback_split_pct does the real proportional sizing.
        self._transcript_playback_splitter.setStretchFactor(0, 7)
        self._transcript_playback_splitter.setStretchFactor(1, 3)
        self._transcript_playback_splitter.splitterMoved.connect(
            self._on_playback_splitter_moved
        )
        # Default top-pct (overridden by config via
        # set_transcript_playback_split_top_pct from MainApp at startup).
        self._playback_split_top_pct: int = 70
        playback_layout.addWidget(self._transcript_playback_splitter, 1)

        # Issue #80: empty-state affordance. Shown above the editor
        # when the session has no transcript on disk yet (e.g. a
        # newly-created session, or one where the user attended a
        # meeting they couldn't record). Hidden as soon as content
        # arrives -- either from a recording or from this very import.
        self._transcript_empty_row = QWidget(transcript_page)
        empty_row_layout = QHBoxLayout(self._transcript_empty_row)
        empty_row_layout.setContentsMargins(0, 0, 0, 6)
        empty_row_layout.setSpacing(8)
        empty_msg = QLabel(
            "No transcript yet. Record a meeting, or import one from "
            "another source (Teams export, clipboard).",
            self._transcript_empty_row,
        )
        empty_msg.setStyleSheet("color: palette(placeholder-text);")
        empty_row_layout.addWidget(empty_msg, 1)
        self._import_transcript_btn = QPushButton(
            "Import Transcript...", self._transcript_empty_row,
        )
        self._import_transcript_btn.setToolTip(
            "Bring in a transcript from a Teams export, a .txt/.md file, "
            "or the clipboard. Unlocks Send to Claude.ai and Save to... "
            "for this session."
        )
        self._import_transcript_btn.clicked.connect(self._on_import_transcript_clicked)
        empty_row_layout.addWidget(self._import_transcript_btn, 0)
        self._transcript_empty_row.setVisible(False)
        transcript_layout.addWidget(self._transcript_empty_row, 0)

        self._transcript_layout_stack = QStackedWidget(transcript_page)
        self._transcript_layout_stack.addWidget(self._transcript_idle_page)
        self._transcript_layout_stack.addWidget(self._transcript_playback_page)
        transcript_layout.addWidget(self._transcript_layout_stack, 1)

        self._player_bar = TranscriptPlayerBar(transcript_page)
        self._player_bar.play_clicked.connect(self._on_player_bar_play_clicked)
        self._player_bar.pause_clicked.connect(self._on_player_bar_pause_clicked)
        self._player_bar.seek_ms_requested.connect(self._on_player_bar_seek_requested)
        transcript_layout.addWidget(self._player_bar, 0)
        # v0.7.0 highlight bar: shaded markers + Start/End toggle +
        # Clear All. Sits directly under the player bar so the
        # markers align with the scrubber visually. Mutations
        # bubble up via highlights_changed for MainApp to persist.
        self._highlight_bar = HighlightBar(transcript_page)
        self._highlight_bar.highlights_changed.connect(self._on_highlights_changed)
        transcript_layout.addWidget(self._highlight_bar, 0)
        self._tabs.addTab(transcript_page, "Transcript")
        # Attachments tab (issue #29): per-session file attach +
        # preview. Added after Transcript per the spec.
        self._attachments_tab = AttachmentsTab(self)
        self._attachments_tab.attachments_changed.connect(
            self._on_attachments_changed,
        )
        self._tabs.addTab(self._attachments_tab, "Attachments")

        # Per-line timestamp index. Built each time the transcript text
        # changes; consumed by the position-driven highlight and the
        # click-to-seek path. Each tuple is (start_ms, block_number).
        self._transcript_timestamps: list[tuple[int, int]] = []
        self._current_highlight_block: Optional[int] = None
        # When the user clicks a transcript line we seek the player
        # to (line.t_start - 10s) for the lead-in. The position-tick
        # auto-highlight would then jump to whatever line is being
        # spoken 10s before the clicked one -- confusing visual
        # feedback. _pinned_highlight_block holds the clicked block;
        # _pinned_until_ms is the timestamp at which the auto-highlight
        # takes over again. Cleared when either fires.
        self._pinned_highlight_block: Optional[int] = None
        self._pinned_until_ms: int = 0
        # Screenshot offsets relative to recording start, sorted
        # ascending by offset. Populated by MainApp via
        # set_screenshot_offsets() on session select + after capture /
        # delete.
        self._screenshot_offsets: list[tuple[Path, int]] = []
        # Tracks the currently-shown screenshot in the playback top
        # pane so we only call set_image_path on actual changes.
        self._current_playback_screenshot: Optional[Path] = None

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
        self._screencap_sidebar.auto_capture_toggled.connect(
            self._on_screencap_auto_toggled
        )
        right_column_layout.addWidget(self._screencap_sidebar)
        self._attendee_sidebar = AttendeeSidebar(right_column)
        self._attendee_sidebar.tag_clicked.connect(self._on_attendee_tag_clicked)
        self._attendee_sidebar.remove_last_requested.connect(
            self._on_attendee_remove_last_clicked
        )
        # Stretch factor 1 so the attendee sidebar grows to fill the
        # remaining vertical space under the screencap sidebar. The
        # sidebar's internal QScrollArea has its own stretch factor of
        # 1, so the scroll viewport tracks the available height and only
        # actually scrolls when the attendee list overflows. The earlier
        # trailing addStretch(1) starved the sidebar of vertical room
        # and made the scrollbar appear even for short lists (#58).
        right_column_layout.addWidget(self._attendee_sidebar, 1)
        self._right_column = right_column
        self._right_column.setVisible(False)
        body_row.addWidget(self._right_column, 0)
        layout.addLayout(body_row, 1)
        # Classification bar (v0.7.0+): series + people + topics
        # chips for the active session. Slots in between body_row
        # and the find bar so it's always visible regardless of
        # which tab is active. Mutations bubble up to MainApp via
        # the *_requested signals.
        # v0.7.0 tweak #8: the classification bar (Series / People /
        # Topics) sits on the controls row, to the right of the
        # Start Recording + Screen Capture buttons. Saves a row of
        # vertical real estate and groups all session-level
        # affordances together. controls.addStretch was added at the
        # top of __init__ to push the bar to the right edge.
        self._classification_bar = ClassificationBar(self)
        self._classification_bar_slot.addWidget(self._classification_bar)
        # Within-tab find bar (Ctrl+F). Hidden by default; the
        # `Ctrl+F` shortcut wires _open_find_bar() to attach it to
        # whichever text widget is in the active tab. Sits at the
        # bottom of the SessionView so it spans the body width.
        self._find_bar = FindBar(self)
        layout.addWidget(self._find_bar)
        # The shortcut is scoped to this widget so it doesn't fire
        # when focus is in the session list (Ctrl+F there is reserved
        # by the system).
        from PyQt6.QtGui import QShortcut, QKeySequence  # noqa: PLC0415
        find_shortcut = QShortcut(QKeySequence.StandardKey.Find, self)
        find_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        find_shortcut.activated.connect(self._open_find_bar)

        self._tabs.currentChanged.connect(self._on_tab_changed)
        # Re-emit classification bar signals upward unchanged.
        self._classification_bar.add_topic_requested.connect(self.add_topic_requested.emit)
        self._classification_bar.remove_topic_requested.connect(self.remove_topic_requested.emit)
        self._classification_bar.accept_topic_requested.connect(self.accept_topic_requested.emit)
        self._classification_bar.set_series_requested.connect(self.set_series_requested.emit)

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
            self._screenshot_offsets = []
            self._refresh_transcript_timestamps()
            self.set_player_enabled(False)
            self._leave_playback_layout()
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
        self._state_label.setText(_pretty_state(
            session.state,
            has_live_transcript=(
                session.has_transcript or bool(transcript.strip())
            ),
        ))
        self._raw_transcript_text = transcript
        self._transcript_view.setPlainText(rewrite_user_label(transcript, self._user_name))
        self._refresh_transcript_timestamps()
        self._refresh_transcript_placeholder(session)
        sdir = session_dir(session.id)
        self._notes_view.set_session_dir(sdir)
        self._set_notes_text(notes)
        # Synthesis defaults to preview mode (read-first UX); the user can
        # flip to Edit on demand. Empty body stays in edit so a fresh
        # paste-back lands directly in the editable buffer.
        self._notes_view.set_preview_mode(bool(notes.strip()))
        self._live_notes_editor.set_session_dir(sdir)
        # Use the public setter so the preview-mode default is applied
        # consistently with the async post-load path (#67 followup).
        # The session-select prelude passes live_notes="" so this
        # initial call lands the editor in Edit; MainApp's content
        # worker calls set_live_notes_text again with the real body
        # off disk and the public setter flips to Preview when the
        # body has anything beyond the seeded template.
        self.set_live_notes_text(live_notes)
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
        self._refresh_transcript_placeholder(self._session)
        has_live_transcript = (
            self._session.has_transcript
            or bool(self._transcript_view.toPlainText().strip())
        )
        self._state_label.setText(_pretty_state(
            state, has_live_transcript=has_live_transcript,
        ))
        self._set_buttons_for_state(
            state,
            has_transcript=has_live_transcript,
            has_notes=self._session.has_notes or bool(self._notes_view.toPlainText().strip()),
        )
        self._refresh_sidebar_visibility()

    # ---- click-to-tag attendee sidebar -------------------------------------

    def set_attendee_names(self, names: list[str]) -> None:
        """Refresh the sidebar's attendee list. Called by the controller
        whenever the live_notes '# Attendees' section changes."""
        self._attendee_sidebar.set_attendees(names)

    def set_appendix_export_defaults(self, defaults) -> None:
        """Push the user's Settings-saved AppendixInclusion defaults
        into the SessionView so the per-tab Export PDF / Print
        flow pre-checks the right boxes. MainApp calls this on
        construction + after every Settings save. ``defaults`` may
        be None to fall back to the dialog's "all on" defaults."""
        self._appendix_export_defaults = defaults

    def set_export_default_folder(self, path: str) -> None:
        """Push Settings > Export default folder (v0.7.5) so the
        per-tab Export PDF dialog opens there instead of the session
        dir. Empty string means "no default configured" -- the PDF
        dialog falls back to the session dir, matching legacy
        behavior. MainApp calls this on construction + after every
        Settings save."""
        self._export_default_folder = path or ""

    def set_session_attachment_names(self, names) -> None:
        """Push the current session's attachment display names into
        the Appendix tray so the Session Attachments section stays
        in sync with AttachmentsStore (#64). MainApp calls this on
        session select + after attachments are added or removed.
        """
        names = list(names or [])
        self._session_attachment_names = names
        self._refresh_appendix_trays()

    def _refresh_appendix_trays(self) -> None:
        """Rebuild the AppendixData payload + push it into the trays.

        Reads the four LLM appendix sections from the sidecar
        (``notes.appendices.json``) so the strip-on-save toggle no
        longer empties the tray. Falls back to parsing notes.md for
        any section the sidecar doesn't carry (sessions that
        predate the sidecar migration). Links + session attachments
        are computed fresh from the in-memory editor text and the
        cached attachment list.
        """
        from ..utils.appendix_store import collect_for_session  # noqa: PLC0415
        notes_text = self._notes_view.toPlainText()
        live_notes_text = self._live_notes_editor.toPlainText()
        session_id = self._session.id if self._session is not None else None
        data = collect_for_session(
            session_id=session_id,
            notes_text=notes_text,
            live_notes_text=live_notes_text,
            session_attachments=getattr(
                self, "_session_attachment_names", [],
            ),
        )
        self._live_notes_appendix.set_data(data)
        self._notes_appendix.set_data(data)
        # Both editors also use this data for their preview-pane
        # transform so the rendered preview shows the "## Appendix
        # (auto-extracted)" section directly.
        self._live_notes_editor.set_appendix_data(data)
        self._notes_view.set_appendix_data(data)

    def set_session_contacts(self, contacts) -> None:
        """Push the resolved Contact list into the My Notes + Synthesis
        attendee-details drawers. Issue #51 Phase 3.

        MainApp calls this whenever the session's attendee links
        change (after a resolve pass) -- typically after
        _sync_attendees_to_people, after a calendar seed, or after
        the LLM appendix extraction fills in fields.

        Also cached on the session view for the PDF-export rich-table
        rule (#51 Phase 5): _build_print_document checks this list to
        decide whether the printable markdown's Attendees section
        should be a Markdown table or the original bullet list.
        """
        contacts = list(contacts or [])
        self._session_contacts = contacts
        self._live_notes_drawer.set_contacts(contacts)
        self._notes_drawer.set_contacts(contacts)
        # Drive the Preview-pane Attendees-table substitution (#56)
        # on both editors. The underlying markdown buffer is
        # untouched; the swap only applies to the rendered preview.
        self._live_notes_editor.set_session_contacts(contacts)
        self._notes_view.set_session_contacts(contacts)

    def _on_drawer_contact_clicked(self, contact_id: int) -> None:
        """Bridge a drawer-row click up to MainApp.

        Both drawers share this slot since they emit the same signal
        shape; MainApp opens the Address Book filtered to that
        contact.
        """
        self.contact_clicked_in_drawer.emit(contact_id)

    def set_speaker_tag_counts(self, counts: dict[str, int]) -> None:
        """Refresh the sidebar's per-name tag-count badges."""
        self._attendee_sidebar.set_counts(counts)

    def _on_attendee_tag_clicked(self, name: str) -> None:
        if self._session is None:
            return
        self.tag_speaker_clicked.emit(self._session.id, name)

    def _toggle_right_column(self, visible: bool) -> None:
        """Fire ``right_column_will_toggle`` BEFORE the visibility
        change so MainWindow can checkpoint the splitter sizes, then
        flip visibility. Idempotent / cheap when state matches.
        """
        if self._right_column.isVisible() == visible:
            return
        self.right_column_will_toggle.emit(visible)
        self._right_column.setVisible(visible)

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
            self._toggle_right_column(False)
            return
        current = self._tabs.currentWidget()
        # Comparison uses the page wrappers since v0.7.2 #51 Phase 3
        # wraps the editors in containers with the attendee drawer.
        on_transcript_or_notes = current in (
            self._transcript_view, self._live_notes_page,
        )
        self._toggle_right_column(on_transcript_or_notes)
        # The screencap sidebar belongs to My Notes only; hide it on
        # the Transcript tab even though the column is shown for the
        # attendee tag controls.
        self._screencap_sidebar.setVisible(current is self._live_notes_page)

    # ---- screen-capture API used by MainApp ------------------------------

    def set_screencap_armed(self, armed: bool) -> None:
        """Flip the toggle button + sidebar enable state.

        MainApp calls this after the user confirms a region (-> True)
        or clicks Stop Screen Capture / the recording ends (-> False).
        Keeping the visible state in one place avoids the toggle button
        and the sidebar drifting out of sync.

        On arm we also switch the active tab to My Notes -- that's
        where the screencap sidebar lives, and without the switch the
        user has to manually click into My Notes before the Capture /
        Insert / Auto-capture controls are visible (the sidebar's
        visibility is gated on `current tab is My Notes` in
        _refresh_sidebar_visibility, and arming alone doesn't trigger
        that refresh).
        """
        self._screencap_armed = armed
        self._screencap_sidebar.set_armed(armed)
        self._screen_capture_btn.setText(
            "Stop Screen Capture" if armed else "Start Screen Capture"
        )
        self._refresh_screencap_button_enabled()
        if armed and self._tabs.currentWidget() is not self._live_notes_page:
            # setCurrentWidget triggers _on_tab_changed which calls
            # _refresh_sidebar_visibility, surfacing the sidebar.
            self._tabs.setCurrentWidget(self._live_notes_page)

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

    def _on_screencap_auto_toggled(self, enabled: bool) -> None:
        if self._session is None:
            return
        self.screencap_auto_toggled.emit(self._session.id, enabled)

    def set_screencap_auto_interval(self, seconds: int) -> None:
        """Push the configured interval (Settings -> auto-capture) into
        the sidebar's helper text so the user sees 'every Ns'."""
        self._screencap_sidebar.set_auto_interval_seconds(seconds)

    def _on_screenshot_delete_requested(self, paths: list[Path]) -> None:
        if self._session is None or not paths:
            return
        self.delete_screenshot_clicked.emit(self._session.id, list(paths))

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
        """Master enable for the player bars.

        MainApp calls this with True when the active session has
        retained audio (the player has something to play) and False
        otherwise. Forwards to both the Transcript pane's bar AND the
        Slides tab's bar so a single state controls both surfaces.
        """
        self._player_bar.set_enabled_state(enabled)
        self._slides_view.set_player_enabled(enabled)
        if not enabled:
            self._clear_transcript_highlight()

    def set_player_loading_state(self, loading: bool) -> None:
        """Forward the AudioPlayer's "decode in flight" cue to both
        player bars (#61). The bars stay disabled while loading; the
        time label flips to "Loading audio...". Clears when set to
        False, but caller should follow up with set_player_enabled
        (or another set_player_loading_state(False)) to settle the
        idle vs ready labels."""
        self._player_bar.set_loading_state(loading)
        self._slides_view.set_player_loading_state(loading)

    def set_player_total_ms(self, total_ms: int) -> None:
        self._player_bar.set_total_ms(total_ms)
        self._slides_view.set_player_total_ms(total_ms)
        # Highlight bar uses the same time axis as the scrubber.
        self._highlight_bar.set_total_ms(total_ms)

    def set_player_position_ms(self, ms: int) -> None:
        self._player_bar.set_position_ms(ms)
        self._slides_view.set_player_position_ms(ms)
        self._highlight_bar.set_player_position(ms)
        # Highlight the transcript line that owns this timestamp. We
        # do this on every position update so the highlight follows
        # playback in real time. Skip when the user is mid-drag --
        # otherwise the highlight thrashes around the slider thumb.
        if self._player_bar.is_user_dragging():
            return
        self._refresh_transcript_highlight(ms)
        # Any non-zero position means the user has engaged playback
        # (played at some point, or click-to-seeked from the
        # transcript). Show the playback layout so the matching
        # screenshot is visible for that moment, even when audio
        # isn't actively playing.
        if ms > 0 and self._screenshot_offsets:
            self._enter_playback_layout()
        # Drive the playback layout's top image off the same position.
        # In idle layout this is a no-op (the helper short-circuits).
        self._refresh_playback_image(ms)

    def set_player_is_playing(self, playing: bool) -> None:
        """Flip the play/stop button labels.

        v0.6.5 update: this no longer drives the layout swap.
        Pause/Stop keeps the playback layout up so the user still sees
        the current-position screenshot; the layout reverts only when
        playback drains naturally (handled in
        revert_to_idle_layout()) or the session changes.
        """
        self._player_bar.set_is_playing(playing)
        self._slides_view.set_player_is_playing(playing)

    def revert_to_idle_layout(self) -> None:
        """Force the transcript pane back to the side-rail layout.

        MainApp calls this after natural end-of-playback so the user
        sees the rail again with the playhead reset to 0. Idempotent
        if already in idle layout.
        """
        self._leave_playback_layout()

    def _on_slides_play_clicked(self) -> None:
        if self._session is None:
            return
        self.transcript_play_clicked.emit(self._session.id)

    def _on_slides_pause_clicked(self) -> None:
        if self._session is None:
            return
        self.transcript_pause_clicked.emit(self._session.id)

    def _on_slides_seek_requested(self, ms: int) -> None:
        if self._session is None:
            return
        self.transcript_seek_ms_requested.emit(self._session.id, int(ms))

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
        # Pin the clicked line as the highlight until playback catches
        # up to its timestamp. Without the pin, the audio seek (10s
        # earlier) would drag the highlight back to an earlier line,
        # then walk forward to the clicked one over the next 10s --
        # confusing feedback for "I clicked here".
        self._pinned_highlight_block = block_number
        self._pinned_until_ms = int(start_ms)
        self._apply_highlight_to_block(block_number, scroll_into_view=True)
        # Seek a few seconds before the clicked line so the listen-back
        # catches the lead-in (Aaron's "~10s before that line").
        target = max(0, int(start_ms) - _TRANSCRIPT_SEEK_LEAD_MS)
        self.transcript_seek_ms_requested.emit(self._session.id, target)

    def _refresh_transcript_placeholder(self, session) -> None:
        """Set the Transcript tab's placeholder copy based on session state.

        Three audiences (#87):

          * Fresh session, no recording yet -- guide them to Start /
            Import.
          * Recording in progress -- "Live transcript appears here as
            speech is captured." (Or: "Capture-only mode is on..." when
            applicable; we keep the message simple.)
          * Recording finished but no segments returned -- "No speech
            detected. Use the My Notes tab; the Generate Synthesis
            Prompt button works from notes alone." This is the
            mic-only / quiet-narration / walkthrough case.
        """
        state = session.state
        if state in (STATE_COMPLETE, STATE_ERROR) and session.has_transcript:
            self._transcript_view.setPlaceholderText(
                "No speech detected in this recording.\n\n"
                "Add notes in the My Notes tab; Generate Synthesis Prompt "
                "drafts from notes alone when the transcript is empty."
            )
        elif state == STATE_NEW:
            self._transcript_view.setPlaceholderText(
                "No transcript yet. Start a recording or use "
                "File > Import Transcript to bring one in."
            )
        else:
            self._transcript_view.setPlaceholderText(
                "Transcript appears here once the recording finishes."
            )

    def _refresh_transcript_timestamps(self) -> None:
        """Rebuild the timestamp index from the transcript view text.

        Called whenever set_transcript_text / set_session updates the
        displayed text. Click-to-seek and the position-driven highlight
        consume this list.
        """
        text = self._transcript_view.toPlainText()
        self._transcript_timestamps = _parse_transcript_timestamps(text)
        self._clear_transcript_highlight()

    def _refresh_transcript_highlight(self, position_ms: int) -> None:
        """Update the highlighted line from the current playback position.

        If the user has clicked a line, the highlight stays pinned to
        that line until playback reaches the line's timestamp -- so
        the 10-second seek lead-in doesn't drag the visual focus
        backward.
        """
        # Pinned-block branch: keep showing the clicked line until
        # playback catches up to it. The +1ms slack avoids a
        # one-tick flicker right at the boundary.
        if self._pinned_highlight_block is not None:
            if position_ms + 1 < self._pinned_until_ms:
                # Re-apply in case the document changed underneath
                # us between the click and now.
                if self._current_highlight_block != self._pinned_highlight_block:
                    self._apply_highlight_to_block(
                        self._pinned_highlight_block, scroll_into_view=False,
                    )
                return
            # Pin's expired; fall through to normal auto-highlight.
            self._pinned_highlight_block = None
            self._pinned_until_ms = 0
        block_number = _block_for_position_ms(
            self._transcript_timestamps, position_ms,
        )
        if block_number is None:
            self._clear_transcript_highlight()
            return
        if block_number == self._current_highlight_block:
            return
        self._apply_highlight_to_block(block_number, scroll_into_view=True)

    def _apply_highlight_to_block(
        self, block_number: int, *, scroll_into_view: bool,
    ) -> None:
        """Paint the highlight ExtraSelection on the given block.

        Shared between the position-driven path and the user-click
        path. The ExtraSelection format paints behind the text without
        disrupting cursor / read-only state.
        """
        self._current_highlight_block = block_number
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
        if scroll_into_view:
            view_cursor = self._transcript_view.textCursor()
            view_cursor.setPosition(cursor.position())
            self._transcript_view.setTextCursor(view_cursor)
            self._transcript_view.ensureCursorVisible()

    def _clear_transcript_highlight(self) -> None:
        self._current_highlight_block = None
        self._pinned_highlight_block = None
        self._pinned_until_ms = 0
        self._transcript_view.setExtraSelections([])

    # ---- screenshots <-> transcript wiring -------------------------------

    def set_screenshot_offsets(self, offsets: list[tuple[Path, int]]) -> None:
        """Pin the (path, offset_ms) list MainApp computed at session-load.

        Pushes the list to the Slides tab so its position-driven
        advance + click-to-seek share one source of truth, and
        refreshes the playback top image against the current player
        position so a fresh capture surfaces immediately.
        """
        self._screenshot_offsets = list(offsets)
        self._slides_view.set_screenshot_offsets(self._screenshot_offsets)
        if self._is_in_playback_layout():
            self._refresh_playback_image(self._player_bar._slider.value())  # noqa: SLF001

    # ---- layout swap (idle <-> playback) ---------------------------------

    def _is_in_playback_layout(self) -> bool:
        return self._transcript_layout_stack.currentWidget() is self._transcript_playback_page

    def _enter_playback_layout(self) -> None:
        """Swap the transcript pane into the screenshare-style layout.

        No-op when there are no screenshots for this session -- the
        layout swap would just produce an empty top pane. Audio still
        plays in idle layout in that case.
        """
        if not self._screenshot_offsets:
            return  # Stay in idle layout; audio still plays beneath.
        if self._is_in_playback_layout():
            return
        # Move the editor from the idle layout into the playback
        # splitter. setParent + insertWidget keeps the QPlainTextEdit's
        # contents + scroll + selection state intact.
        self._transcript_playback_splitter.insertWidget(1, self._transcript_view)
        self._playback_editor_placeholder.hide()
        self._idle_editor_placeholder.show()
        self._transcript_layout_stack.setCurrentWidget(self._transcript_playback_page)
        # Apply the configured split AFTER the page is shown, so the
        # splitter has its final height to compute pixel sizes against.
        QTimer.singleShot(0, self._apply_playback_split_pct)

    def _leave_playback_layout(self) -> None:
        if not self._is_in_playback_layout():
            return
        # Move the editor back into the idle layout.
        idle_layout = self._transcript_idle_page.layout()
        if idle_layout is not None:
            idle_layout.insertWidget(0, self._transcript_view)
        self._idle_editor_placeholder.hide()
        self._playback_editor_placeholder.show()
        self._transcript_layout_stack.setCurrentWidget(self._transcript_idle_page)
        self._playback_image.clear_image()
        self._current_playback_screenshot = None

    def _apply_playback_split_pct(self) -> None:
        """Resize the playback splitter to match the saved top-pct.

        Reads the splitter's current height and assigns pixel sizes
        proportionally to the two visible panes (image at index 0,
        editor at index 1). The placeholder at index 2 is hidden
        while in playback layout; it stays at 0 so the visible split
        owns the full height. No-op if the splitter has zero height
        (parent not yet laid out); the caller defers via
        QTimer.singleShot(0).
        """
        h = self._transcript_playback_splitter.height()
        if h <= 0:
            return
        pct = self._playback_split_top_pct
        pct = max(10, min(90, pct))
        top = int(h * pct / 100)
        bottom = h - top
        count = self._transcript_playback_splitter.count()
        sizes = [top, bottom] + [0] * max(0, count - 2)
        self._transcript_playback_splitter.setSizes(sizes)

    def _on_playback_splitter_moved(self, pos: int, index: int) -> None:
        """Recompute top-pct from the splitter's first two sizes + emit.

        Only sizes[0] (image) and sizes[1] (editor) count; the
        placeholder at index 2 stays at 0 in playback layout.
        """
        sizes = self._transcript_playback_splitter.sizes()
        if len(sizes) < 2:
            return
        visible_total = sizes[0] + sizes[1]
        if visible_total <= 0:
            return
        pct = int(round(sizes[0] * 100 / visible_total))
        pct = max(10, min(90, pct))
        if pct == self._playback_split_top_pct:
            return
        self._playback_split_top_pct = pct
        self.transcript_playback_split_changed.emit(pct)

    def set_transcript_playback_split_top_pct(self, pct: int) -> None:
        """MainApp pushes the persisted split pct in at startup.

        Applied to the splitter immediately if it's currently in
        playback layout, otherwise stashed for the next _enter call.
        """
        pct = max(10, min(90, pct))
        self._playback_split_top_pct = pct
        if self._is_in_playback_layout():
            self._apply_playback_split_pct()

    def _refresh_playback_image(self, position_ms: int) -> None:
        """Sticky-image lookup against the screenshot offsets list.

        If the position precedes every screenshot, clear the top
        pane (Aaron's "if no image is relevant at the start, don't
        show yet"). Otherwise show the latest screenshot whose
        offset <= position; the pane stays on that image until the
        next capture's offset is reached.
        """
        if not self._is_in_playback_layout():
            return
        from ..screencap.timestamps import current_screenshot_for_position  # noqa: PLC0415
        match = current_screenshot_for_position(
            self._screenshot_offsets, position_ms,
        )
        if match is None:
            if self._playback_image.has_image():
                self._playback_image.clear_image()
            self._current_playback_screenshot = None
            return
        if match == self._current_playback_screenshot:
            return
        self._playback_image.set_image_path(match)
        self._current_playback_screenshot = match

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

    def apply_fonts(self, editor_font, preview_font) -> None:
        """Push the resolved editor + preview fonts to every surface
        that respects them: the My Notes editor + preview, the
        Synthesis editor + preview, and the Previous Notes preview.

        The Transcript view stays on its own dedicated monospace
        ("Consolas" with Monospace style hint, hard-coded at
        construction) because timestamps need column alignment that
        depends on Qt picking a non-substituted monospace face even
        if the user prefers a different one for editing prose.
        Called by MainApp at startup + on Settings Save."""
        self._live_notes_editor.apply_fonts(editor_font, preview_font)
        self._notes_view.apply_fonts(editor_font, preview_font)
        # Previous notes is a read-only preview; push the preview
        # font but skip the editor side (it has none).
        try:
            self._previous_view.apply_fonts(preview_font)
        except AttributeError:
            # Older builds without the per-widget setter; harmless to
            # skip -- preview reverts to the QApplication default.
            pass

    def set_rich_source_view(self, enabled: bool) -> None:
        """Toggle the styled markdown source view (#91) on every editor
        that hosts one. Applies to both My Notes and Synthesis -- the
        Synthesis tab is just as edit-heavy as My Notes once the user
        starts cleaning up the LLM's draft (#102 bug 1)."""
        for editor in (self._live_notes_editor, self._notes_view):
            try:
                editor.set_rich_source_view(enabled)
            except AttributeError:
                pass

    def set_export_outline_options(
        self,
        *,
        number_headings: bool,
        include_toc: bool,
        toc_max_depth: int = 3,
        use_word_for_pdf: bool = False,
    ) -> None:
        """Push the export outline preferences (#92, #94) into the
        session view. Used by the per-tab Export PDF / Print path --
        the body is transformed before render so PDFs match the
        configured preferences. Idempotent.

        ``use_word_for_pdf`` -- #94 follow-up. When True AND Word COM
        is available, "Save as PDF..." renders via Word's native TOC +
        PDF export pipeline rather than Qt's PDF backend. When True
        on a non-Windows host (or Windows without Word), the flag is
        silently ignored; the Qt path stays the fallback.
        """
        self._export_heading_numbering = bool(number_headings)
        self._export_toc = bool(include_toc)
        self._export_toc_max_depth = max(1, min(6, int(toc_max_depth)))
        self._use_word_for_pdf = bool(use_word_for_pdf)

    def set_heading_numbering(self, enabled: bool) -> None:
        """Toggle preview heading numbering (#92) on every preview-bearing
        widget. SessionView routes to My Notes, Synthesis, Previous
        Notes -- so the user sees consistent numbering across tabs.
        The widgets re-render themselves so the change shows
        immediately without a session switch."""
        for widget in (self._live_notes_editor, self._notes_view):
            try:
                widget.set_heading_numbering(enabled)
            except AttributeError:
                continue
        # Previous Notes preview is a separate widget; forward through
        # if it implements the toggle.
        try:
            self._previous_view.set_heading_numbering(enabled)
        except AttributeError:
            pass

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
        # New synthesis content can introduce JSON appendices + new
        # links; refresh the tray + preview transform (#64).
        self._refresh_appendix_trays()

    def refresh_button_state(self) -> None:
        """Re-evaluate every per-tab / per-state button (Print, Save to
        ..., Send, Copy, etc.) against the current session + buffer
        content. Public so paths that update has_notes / has_transcript
        out-of-band (e.g. the synthesis-result handler in MainApp) can
        force a recompute (#116).

        #102 bug 10 addressed the same symptom by adding an OR-with-
        buffer fallback to ``set_synthesis_in_progress(False)`` and
        mirroring the DB has_notes flip into the in-memory session
        object. Both fixes are correct but ran in the wrong order vs
        ``_apply_synthesis_result``: the in-progress clear fired
        before the session object + notes buffer were updated, so the
        button recompute it triggered evaluated the still-empty
        fallback and grayed the Save to dropdown. This entrypoint lets
        the synthesis-result handler re-run the recompute AFTER both
        updates land.

        Uses the same OR-with-buffer fallback ``set_session`` uses so
        a freshly-written buffer counts as has_notes even before the
        DB flip propagates.
        """
        if self._session is None:
            return
        self._set_buttons_for_state(
            self._session.state,
            has_transcript=(
                self._session.has_transcript
                or bool(self._raw_transcript_text)
            ),
            has_notes=(
                self._session.has_notes
                or bool(self._notes_view.toPlainText().strip())
            ),
        )

    def set_live_notes_text(self, text: str) -> None:
        """Replace the My Notes body + reapply the preview-mode default.

        The session-select path is two-phase: set_session() binds an
        empty buffer synchronously for snappy UI swap, then a worker
        in MainApp reads live_notes.md off disk and pushes the real
        content back via this setter. The preview-mode default lands
        here (not in _set_live_notes_text) so the *real* body is what
        decides Edit vs Preview -- previously this lived in
        set_session only, which always ran against an empty body and
        therefore always landed in Edit even for sessions with notes.

        Matches the Synthesis tab's set_notes_text pattern: public
        setter applies preview-mode based on content; private
        _set_live_notes_text just touches text.
        """
        self._set_live_notes_text(text)
        from ..utils.live_notes import has_user_content  # noqa: PLC0415
        self._live_notes_editor.set_preview_mode(has_user_content(text))

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
        # Synthesis edits may add / remove appendix JSON blocks;
        # re-parse + persist to the sidecar so the tray + preview
        # transform pick up the change without round-tripping
        # through the save loop (#64 + sidecar followup).
        try:
            from ..utils.appendix_store import AppendixStore  # noqa: PLC0415
            AppendixStore(self._session.id).save_from_notes(body)
        except Exception:
            # Sidecar persistence is best-effort; the tray will fall
            # back to parsing the in-memory text on the next refresh.
            pass
        self._refresh_appendix_trays()

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
        # Live-notes edits may add or remove links the appendix
        # tray's Links section surfaces; refresh (#64).
        self._refresh_appendix_trays()

    # ---- internal handlers -------------------------------------------------

    def _on_start(self) -> None:
        if self._session:
            self.start_clicked.emit(self._session.id)

    def _on_stop(self) -> None:
        if self._session:
            self.stop_clicked.emit(self._session.id)

    def _on_record_toggle(self) -> None:
        """Single button that toggles Start <-> Stop based on session
        state. _set_buttons_for_state keeps the label + enabled
        state in sync; this handler just dispatches the right
        signal."""
        if self._session is None:
            return
        if self._session.state in (STATE_RECORDING, STATE_PAUSED):
            self.stop_clicked.emit(self._session.id)
        else:
            self.start_clicked.emit(self._session.id)

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

    def _on_edit_and_send(self) -> None:
        """Dropdown menu item next to Send / Generate (#90): MainApp
        renders the prompt, opens the SessionPromptEditDialog, and
        dispatches the edited body through whichever downstream path
        (automation bridge or clipboard) the current mode requires.
        We just emit -- the work happens in app.py."""
        if self._session is not None:
            self.edit_and_send_clicked.emit(self._session.id)

    def _on_import_transcript_clicked(self) -> None:
        """Fire the per-session import signal so MainApp opens the
        ImportTranscriptDialog scoped to this session."""
        if self._session is not None:
            self.import_transcript_clicked.emit(self._session.id)

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
                # Use the same OR-with-buffer fallback set_session uses,
                # so a synthesis that JUST landed in the editor buffer
                # but hasn't yet flipped the in-memory has_notes flag
                # still flows through as has_notes=True. Aaron's
                # 2026-06-13 trace: Send button stuck disabled after a
                # successful first synthesis until the user swapped
                # sessions and back -- because set_session uses the OR
                # fallback while this path didn't (#102 bug 10).
                self._set_buttons_for_state(
                    self._session.state,
                    has_transcript=(
                        self._session.has_transcript
                        or bool(self._raw_transcript_text)
                    ),
                    has_notes=(
                        self._session.has_notes
                        or bool(self._notes_view.toPlainText().strip())
                    ),
                )

    def set_prompt_templates(
        self,
        template_names: list[str],
        selected: str = "",
        settings_default: str = "",
    ) -> None:
        """Populate the prompt template picker.

        ``template_names`` is the list of available templates (from the
        prompts module). ``selected`` is the currently-saved
        per-session override ("" = no override; let the resolution
        chain pick). ``settings_default`` is the global Settings
        default; surfaced in the "(default)" entry's label so the user
        sees which template will actually be used when no override is
        set (#55).

        Block signals during population so the currentIndexChanged
        emit doesn't fire spurious save events at app-startup.
        """
        self._prompt_template_picker.blockSignals(True)
        self._prompt_template_picker.clear()
        # First entry is always the "(default)" placeholder -- empty
        # string in data role -- so leaving the picker untouched on a
        # new session uses whatever the resolution chain decides. The
        # label includes the Settings default name when one is set so
        # the user can tell what's about to run; if Settings is empty
        # we fall back to "default" (the bundled template), matching
        # the resolution chain in _on_send_to_llm.
        resolved_default = (settings_default or "").strip() or "default"
        if resolved_default == "default":
            # No Settings override: the placeholder reads as the
            # bundled "default" template directly, matching the prior
            # UX.
            placeholder_label = "(default)"
        else:
            placeholder_label = f"(default: {resolved_default})"
        self._prompt_template_picker.addItem(placeholder_label, "")
        for name in template_names:
            if not name or name == "default":
                # We surface the bundled default via the placeholder
                # entry above; skip the literal "default" template
                # name to avoid the user seeing two entries that
                # both render the same template.
                continue
            self._prompt_template_picker.addItem(name, name)
        # Restore selection. Priority:
        #   1. Per-session override (`selected`) -> match its row.
        #   2. No override but a Settings default exists -> point at
        #      the Settings default's row so the dropdown displays
        #      the template name the user actually chose. Without
        #      this, new sessions always landed on the "(default: X)"
        #      placeholder which truncates inside the 200px-wide
        #      combo to e.g. "(default..stom)" and reads as if the
        #      Settings default is being ignored (#76).
        #   3. Otherwise fall back to the placeholder row.
        # The Settings default is dynamic: if Settings later changes
        # to a different template, sessions with `selected=""` will
        # display the new default on their next load. Per-session
        # overrides win over Settings default.
        target_idx = 0  # placeholder
        effective = selected
        if not effective:
            resolved = (settings_default or "").strip()
            if resolved and resolved != "default":
                effective = resolved
        if effective:
            for i in range(self._prompt_template_picker.count()):
                if self._prompt_template_picker.itemData(i) == effective:
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
        if current is self._live_notes_page:
            return "live_notes"
        if current is self._notes_page:
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

    def attachments_tab(self) -> AttachmentsTab:
        return self._attachments_tab

    def _on_appendix_edit_requested(self) -> None:
        """Bubble the tray's Edit click up to MainApp for handling."""
        if self._session is None:
            return
        self.appendix_edit_requested.emit(self._session.id)

    def _on_attachments_changed(self, session_id: str) -> None:
        # Forward upward; MainApp may want to update an indicator
        # on the session list (e.g. paperclip glyph).
        self.attachments_changed.emit(session_id)
        # Pull the latest attachment names + refresh the appendix
        # tray so the Session Attachments section stays current
        # (#64).
        try:
            from ..models.attachments import AttachmentsStore  # noqa: PLC0415
            store = AttachmentsStore(session_id)
            names = [rec.display_name for rec in store.list()]
            self.set_session_attachment_names(names)
        except Exception:
            # Refresh failures shouldn't disrupt the user-visible
            # add/remove flow; the tray just won't update until the
            # next session-select.
            pass

    def set_session_highlights(
        self,
        total_ms: int,
        highlights: HighlightSet,
    ) -> None:
        """Load the given highlight set into the bar.

        Called from MainApp on session selection. total_ms is the
        loaded audio's duration (0 disables the bar's toggle
        button cleanly).
        """
        session_id = self._session.id if self._session else ""
        self._highlight_bar.set_session_state(session_id, total_ms, highlights)

    def _on_highlights_changed(self, hs: HighlightSet) -> None:
        """Bar mutation -> bubble up so MainApp persists."""
        if self._session is None:
            return
        self.highlights_changed.emit(self._session.id, hs)

    def set_classification_known_lists(
        self,
        *,
        series: Optional[list[str]] = None,
        people: Optional[list[str]] = None,
        topics: Optional[list[str]] = None,
    ) -> None:
        """Forward the alphabetical known-name lists to the chips bar.

        Drives the dropdown half of the Add/Change pickers so the
        user picks-or-types instead of free-form-only.
        """
        self._classification_bar.set_known_lists(
            series=series, people=people, topics=topics,
        )

    def set_classification(self, classification) -> None:
        """Push fresh classification data into the chips bar.

        Called from MainApp whenever the active session's series /
        people / topics change. None-session is handled by passing
        an empty SessionClassification (the bar disables its
        mutator buttons).
        """
        session_id = self._session.id if self._session else None
        self._classification_bar.set_session(session_id, classification)

    def set_active_tab(
        self, tab_id: str, archive_name: Optional[str] = None,
    ) -> bool:
        """Switch to the named tab. Returns True if the tab exists.

        Used by the cross-session search dialog: after the user
        double-clicks a result, MainApp selects the session, then
        calls this to drill into the matching tab. For previous-
        notes hits, `archive_name` (the notes-YYYYMMDD-HHMM.md
        filename) is passed through to the widget so it selects the
        matching archive.
        """
        target_widget = None
        if tab_id == "transcript":
            # Transcript page is held under a stack; the tab widget
            # is the page that contains either the idle or playback
            # variant. Find it by searching the tab widget's pages
            # for the parent of self._transcript_view.
            for i in range(self._tabs.count()):
                if self._tabs.tabText(i) == "Transcript":
                    target_widget = self._tabs.widget(i)
                    break
        elif tab_id == "live_notes":
            target_widget = self._live_notes_editor
        elif tab_id == "notes":
            target_widget = self._notes_view
        elif tab_id == "previous":
            target_widget = self._previous_view
            if archive_name:
                try:
                    self._previous_view.select_archive_by_name(archive_name)
                except AttributeError:
                    # Older builds without select_archive_by_name will
                    # still scroll to the tab; the user picks from
                    # the list manually.
                    pass
        if target_widget is None:
            return False
        idx = self._tabs.indexOf(target_widget)
        if idx < 0:
            return False
        self._tabs.setCurrentIndex(idx)
        return True

    def _find_target_for_active_tab(self) -> Optional[QWidget]:
        """Return the text widget Ctrl+F should bind to in the active tab.

        Slides tab has no searchable text and returns None; the find
        bar treats that as a "nothing to search here" no-op so the
        shortcut is silently inert rather than focus-shifting into
        another tab.
        """
        tab_id = self._active_tab_id()
        if tab_id == "transcript":
            return self._transcript_view
        if tab_id == "live_notes":
            return self._live_notes_editor.find_target()
        if tab_id == "notes":
            return self._notes_view.find_target()
        if tab_id == "previous":
            return self._previous_view.find_target()
        return None

    def _open_find_bar(self) -> None:
        """Ctrl+F handler. Bind the bar to the active tab's text widget
        + reveal it. If the active tab has no searchable content
        (Slides), the find bar stays hidden so the shortcut doesn't
        appear broken via an empty-binding state."""
        target = self._find_target_for_active_tab()
        if target is None:
            return
        self._find_bar.show_for(target)

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
        # Single Record button that toggles Start <-> Stop based on
        # state. Disabled when the session already has retained audio
        # on disk so the user can't accidentally overwrite a finished
        # recording. The session_view doesn't know the audio path
        # directly so we re-check via has_retained_audio.
        if is_recording or is_paused:
            self._record_btn.setText("Stop Recording")
            self._record_btn.setEnabled(has_session)
        else:
            self._record_btn.setText("Start Recording")
            existing_recording = False
            if has_session:
                from ..utils.paths import has_retained_audio  # noqa: PLC0415
                try:
                    existing_recording = has_retained_audio(self._session.id)
                except Exception:
                    existing_recording = False
            self._record_btn.setEnabled(
                has_session and (is_new or is_complete) and not existing_recording
            )
            if existing_recording:
                self._record_btn.setToolTip(
                    "This session already has an audio recording on "
                    "disk. Delete the recording (right-click in the "
                    "session list -> Delete recording) before starting "
                    "a new one."
                )
            else:
                self._record_btn.setToolTip(
                    "Start capturing mic + system audio for this session."
                )
        self._refresh_screencap_button_enabled()
        # Empty-state affordance on the Transcript tab (#80). Visible
        # whenever a session is loaded but has no transcript yet AND
        # the session isn't actively recording (the live captions take
        # the visual real estate during a recording).
        self._transcript_empty_row.setVisible(
            has_session and not has_transcript and not is_recording
        )
        # Generate/paste are available as soon as the user has SOMETHING for
        # the LLM to chew on. That's a transcript OR notes -- a mic-only
        # voice-note / walkthrough session whose audio came out silent
        # still wants to synthesize from notes alone. The prompt
        # template already handles a missing transcript section
        # gracefully.
        # The batch-refinement pass after Stop runs in the background
        # and is explicitly NOT a gate on synthesis -- the live
        # transcript is good enough to act on, and any later regenerate
        # picks up the refined version automatically.
        can_synthesize = (
            has_session and (has_transcript or has_notes) and not is_recording
        )
        self._generate_btn.setEnabled(can_synthesize)
        self._paste_btn.setEnabled(can_synthesize)
        # Dropdown menu actions (#90) share the same gate. The button-
        # level enabled state already grays the dropdown arrow, but
        # disabling the QAction is explicit insurance against the
        # arrow being clickable when nothing's available.
        self._generate_edit_action.setEnabled(can_synthesize)
        self._send_edit_action.setEnabled(can_synthesize)
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
        if current is self._live_notes_page:
            self._print_btn.setEnabled(True)
            self._export_pdf_btn.setEnabled(True)
        elif current is self._notes_page:
            self._print_btn.setEnabled(has_notes)
            self._export_pdf_btn.setEnabled(has_notes)
        else:
            self._print_btn.setEnabled(False)
            self._export_pdf_btn.setEnabled(False)

    def _build_print_document(
        self,
        *,
        ask_appendix_inclusion: bool = True,
        appendix_defaults=None,
    ):
        """Render the active tab into a QTextDocument bound to the session dir.

        Returns (doc, tab_label) or (None, "") if the active tab can't be
        printed. Uses PrintTextDocument so that relative image refs like
        `images/foo.png` resolve to real files on every QPrinter
        loadResource() call -- QTextDocument's own setBaseUrl is only
        honored on the first call, which produced broken-image icons in
        printed PDFs.

        When ``ask_appendix_inclusion`` is True (default for the user-
        facing Export PDF + Print buttons), pops the inclusion dialog
        so the user picks which Appendix sub-sections land in the
        output. Cancelling the dialog returns (None, "") so the
        caller treats it like a normal abort.
        """
        if self._session is None:
            return None, ""
        from .print_document import PrintTextDocument

        current = self._tabs.currentWidget()
        if current is self._live_notes_page:
            markdown_source = self._live_notes_editor.toPlainText()
            tab_label = "My Notes"
        elif current is self._notes_page:
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

        # v0.7.2 #51 Phase 5: when the session has at least one
        # contact with rich-field data, replace the bullet attendee
        # list with a Markdown table so the PDF shows title /
        # company / email / phone columns. Auto-detect rule --
        # bullet list stays when nothing rich is on file.
        from ..utils.live_notes import (  # noqa: PLC0415
            replace_attendees_section_with_table,
            should_render_attendees_as_table,
        )
        body_for_print = markdown_source
        if should_render_attendees_as_table(self._session_contacts):
            body_for_print = replace_attendees_section_with_table(
                markdown_source, self._session_contacts,
            )

        # #64: replace the raw LLM JSON appendix blocks with the
        # rendered "## Appendix (auto-extracted)" table view so the
        # per-tab PDF / Print output matches what the in-app preview
        # shows. Without this the raw JSON code blocks land verbatim
        # in the PDF + the auto-extracted heading is missing.
        from ..utils.appendix_store import collect_for_session  # noqa: PLC0415
        from ..utils.appendix_transform import inject_appendix  # noqa: PLC0415
        from .appendix_inclusion_dialog import (  # noqa: PLC0415
            AppendixInclusion,
            AppendixInclusionDialog,
            apply_inclusion,
        )
        # Pull the same sidecar-backed payload the tray uses so the
        # injection picks up sections that have been stripped from
        # notes.md.
        notes_md = self._notes_view.toPlainText()
        live_md = self._live_notes_editor.toPlainText()
        appendix_data = collect_for_session(
            session_id=self._session.id,
            notes_text=notes_md,
            live_notes_text=live_md,
            session_attachments=getattr(
                self, "_session_attachment_names", [],
            ),
        )
        if ask_appendix_inclusion:
            dlg = AppendixInclusionDialog(
                appendix_data,
                export_label=f"{tab_label} export",
                defaults=appendix_defaults,
                parent=self,
            )
            if dlg.exec() != AppendixInclusionDialog.DialogCode.Accepted:
                return None, ""
            inclusion = dlg.inclusion()
        else:
            inclusion = AppendixInclusion.all_on()
        body_for_print = inject_appendix(
            body_for_print, apply_inclusion(appendix_data, inclusion),
        )

        # #92: heading numbering + TOC. Applied AFTER the appendix
        # injection so the auto-TOC catches the appendix section
        # headings and the numbering covers the whole document.
        if self._export_heading_numbering or self._export_toc:
            from ..utils.markdown_outline import apply_outline  # noqa: PLC0415
            body_for_print = apply_outline(
                body_for_print,
                number=self._export_heading_numbering,
                toc=self._export_toc,
                max_depth=self._export_toc_max_depth,
            )
            # #94: PDF anchor injection happens inside
            # markdown_to_print_html (the setHtml path); no separate
            # inject_pdf_anchors call needed here.

        printable = build_print_markdown(
            session_title=self._session.title,
            tab_label=tab_label,
            session_date=session_when,
            body=body_for_print,
        )

        sdir = session_dir(self._session.id)
        doc = PrintTextDocument(sdir, parent=self)
        # #94: render via setHtml + mistune so the TOC's internal
        # links carry into the PDF as clickable named-destination
        # anchors. The setMarkdown path drops internal links
        # silently.
        from ..utils.print_html import markdown_to_print_html  # noqa: PLC0415
        doc.setHtml(markdown_to_print_html(printable))
        # Force every anchor to render black + underline. Qt's
        # parser writes cyan into the character format at parse
        # time, which the defaultStyleSheet alone can't override;
        # the walk has to happen after setHtml.
        doc.force_anchor_styling()
        # Stash the body markdown on the doc so the Export PDF
        # handler can pass it to the pypdf post-processor for
        # bookmarks + link annotations (#94). Read-only attribute;
        # no mutation after _build_print_document returns.
        doc._mn_body_markdown = body_for_print  # noqa: SLF001
        return doc, tab_label

    def _on_print(self) -> None:
        """Print the active tab via QPrinter."""
        doc, tab_label = self._build_print_document(
            appendix_defaults=self._appendix_export_defaults,
        )
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

    def set_integration_targets(
        self, *, notion_enabled: bool, confluence_enabled: bool,
        obsidian_enabled: bool = False,
    ) -> None:
        """MainApp calls this whenever Settings is saved or on startup
        so the Export menu surfaces Notion / Confluence / Obsidian only
        when the relevant integration's verify stamp is present (#79
        / #96)."""
        self._export_notion_action.setVisible(notion_enabled)
        self._export_confluence_action.setVisible(confluence_enabled)
        self._export_obsidian_action.setVisible(obsidian_enabled)

    def _active_tab_body_and_label(self) -> tuple[str, str]:
        """Return (markdown_body, tab_label) for the currently viewed
        My Notes / Synthesis tab, with the same attendees-table +
        rendered-appendix transforms the PDF path applies (#79
        followup): the export target should receive the formatted
        appendix (tables etc.), not the raw LLM JSON blocks the
        editor source carries.

        The appendix-inclusion dialog the PDF flow shows is skipped
        here -- the integration export flow already opens a picker
        dialog, and stacking a second modal would be annoying. Saved
        Settings defaults drive the inclusion instead; the user can
        change them under Settings -> Export.
        """
        # _tabs holds wrapper pages, not the editors themselves; resolve
        # by the page widget so localization doesn't break the mapping.
        current_page = self._tabs.currentWidget()
        if current_page is self._notes_page:
            source = self._notes_view.toPlainText() or ""
            label = "Synthesis"
        elif current_page is self._live_notes_page:
            source = self._live_notes_editor.toPlainText() or ""
            label = "My Notes"
        else:
            return "", "Notes"

        if self._session is None:
            return source, label

        # Attendees-table substitution (#51 Phase 5): bullet list
        # becomes a Markdown table when at least one contact has
        # rich-field data.
        from ..utils.live_notes import (  # noqa: PLC0415
            replace_attendees_section_with_table,
            should_render_attendees_as_table,
        )
        body = source
        if should_render_attendees_as_table(self._session_contacts):
            body = replace_attendees_section_with_table(
                body, self._session_contacts,
            )

        # Appendix transform (#64) -- swap raw JSON blocks for the
        # rendered "## Appendix (auto-extracted)" tables, mirroring
        # the PDF flow.
        from ..utils.appendix_store import collect_for_session  # noqa: PLC0415
        from ..utils.appendix_transform import inject_appendix  # noqa: PLC0415
        from .appendix_inclusion_dialog import (  # noqa: PLC0415
            AppendixInclusion,
            apply_inclusion,
        )
        notes_md = self._notes_view.toPlainText()
        live_md = self._live_notes_editor.toPlainText()
        appendix_data = collect_for_session(
            session_id=self._session.id,
            notes_text=notes_md,
            live_notes_text=live_md,
            session_attachments=getattr(
                self, "_session_attachment_names", [],
            ),
        )
        inclusion = self._appendix_export_defaults or AppendixInclusion.all_on()
        body = inject_appendix(
            body, apply_inclusion(appendix_data, inclusion),
        )
        return body, label

    def _on_export_notion(self) -> None:
        if self._session is None:
            return
        body, label = self._active_tab_body_and_label()
        self.export_to_notion_requested.emit(self._session.id, label, body)

    def _on_export_confluence(self) -> None:
        if self._session is None:
            return
        body, label = self._active_tab_body_and_label()
        self.export_to_confluence_requested.emit(self._session.id, label, body)

    def _on_export_obsidian(self) -> None:
        if self._session is None:
            return
        body, label = self._active_tab_body_and_label()
        self.export_to_obsidian_requested.emit(self._session.id, label, body)

    def _on_export_pdf(self) -> None:
        """Save the active tab as a PDF via Qt's native PDF backend.

        Qt's PDF writer preserves images (via direct embedding) and link
        annotations (Markdown ``[text](url)`` becomes a clickable PDF
        annotation), where the Windows Print-to-PDF driver typically
        rasterizes both away.

        #109: the actual render + post-process moved off the UI thread
        into ``_PdfExportWorker``. The UI thread builds the document
        (so the appendix-inclusion modal still runs in the right
        context), prompts for the destination, then hands the
        markdown body + render params to the worker and stays
        interactive while the worker writes the PDF. Status-bar
        messages report progress + completion; the Export button is
        disabled for the duration so a second click can't pile a
        second export on top.
        """
        if self._pdf_export_in_flight:
            # Defensive: the Export button is disabled while a worker
            # is running, but a menu / shortcut could in theory still
            # fire. No-op with a status-bar hint instead of queuing.
            self.window().statusBar().showMessage(
                "PDF export already in progress -- please wait.", 4000,
            )
            return
        doc, tab_label = self._build_print_document(
            appendix_defaults=self._appendix_export_defaults,
        )
        if doc is None or self._session is None:
            return
        from PyQt6.QtWidgets import QFileDialog

        suggested_name = default_export_filename(
            self._session.title, tab_label, ".pdf"
        )
        suggested_path = export_initial_save_path(
            self._export_default_folder,
            session_dir(self._session.id),
            suggested_name,
        )
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

        # The doc has the rendered body markdown stashed on it from
        # _build_print_document. Forward to the worker; the worker
        # constructs a fresh PrintTextDocument on its own thread
        # (QTextDocument is reentrant for new instances).
        body_md = getattr(doc, "_mn_body_markdown", "") or ""
        # Drop our reference to the UI-thread doc -- the worker
        # rebuilds.
        del doc

        session_when = None
        if self._session.created_at:
            from datetime import datetime as _dt  # noqa: PLC0415
            try:
                session_when = _dt.fromisoformat(
                    self._session.created_at.replace("Z", "+00:00")
                ).astimezone()
            except ValueError:
                session_when = None

        # The printable markdown the worker re-renders. _build_print_
        # document's stashed body is the post-transform markdown
        # (attendee table, appendix injection, outline numbering, TOC);
        # build_print_markdown wraps it with the header on top.
        printable = build_print_markdown(
            session_title=self._session.title,
            tab_label=tab_label,
            session_date=session_when,
            body=body_md,
        )

        worker = _PdfExportWorker(
            target_path=target,
            session_dir=session_dir(self._session.id),
            session_title=self._session.title,
            tab_label=tab_label,
            printable_markdown=printable,
            body_markdown_for_anchors=body_md,
            use_word=self._use_word_for_pdf,
            export_toc=self._export_toc,
            export_heading_numbering=self._export_heading_numbering,
            export_toc_max_depth=self._export_toc_max_depth,
        )
        self._pdf_export_in_flight = True
        self._pdf_export_worker = worker
        self._refresh_export_buttons_for_in_flight()
        self.window().statusBar().showMessage(
            f"Exporting {tab_label} to PDF... (this can take a moment)",
        )

        worker.progress_message.connect(self._on_pdf_export_progress)
        worker.finished_with_result.connect(self._on_pdf_export_finished)
        worker.start()

    def _on_pdf_export_progress(self, message: str) -> None:
        """Worker -> status bar text. No timeout so the message persists
        until the next progress update or completion."""
        self.window().statusBar().showMessage(message)

    def _on_pdf_export_finished(
        self, success: bool, target_path: str, detail: str,
    ) -> None:
        """Worker done. Re-enable buttons + post the result toast.

        ``detail`` carries either '' or 'via Word' on success, and the
        exception message on failure. Errors stay in the status bar
        for 8s so the user has time to read them; successes for 5s
        (matching the prior synchronous flow's behavior)."""
        target = Path(target_path)
        if success:
            via = f" ({detail})" if detail else ""
            self.window().statusBar().showMessage(
                f"Exported PDF to {target.name}{via}", 5000,
            )
        else:
            self.window().statusBar().showMessage(
                f"PDF export failed: {detail or 'unknown error'}", 8000,
            )
            log.error("pdf export failed: %s -> %s", detail, target)
        if self._pdf_export_worker is not None:
            # Mirror the AttachmentImportWorker shape: wait + deleteLater
            # only after exec/event loop has handed control back. Here
            # the signal is delivered queued on the UI thread, so the
            # worker's run() has already returned; wait() joins the OS
            # thread before deletion.
            self._pdf_export_worker.wait()
            self._pdf_export_worker.deleteLater()
            self._pdf_export_worker = None
        self._pdf_export_in_flight = False
        self._refresh_export_buttons_for_in_flight()

    def _refresh_export_buttons_for_in_flight(self) -> None:
        """Toggle the 'Save as PDF...' / 'Save as Word...' menu actions
        while their respective workers are running -- other Save-to
        targets (Notion, Confluence, Obsidian) stay live so the user
        can start a different export while a long-running one
        finishes in the background. Each action returns to its
        natural enabled state when its worker completes (the menu's
        parent button is what enforces tab-appropriate enable/
        disable; an action's own enabled state only matters when the
        menu is open)."""
        self._export_pdf_action.setEnabled(not self._pdf_export_in_flight)
        self._export_word_action.setEnabled(not self._word_export_in_flight)

    def _on_export_word(self) -> None:
        """Save the active tab as a Word (.docx) document.

        Renders the markdown body via python-docx with a Word native
        TOC field (when the TOC export setting is on) -- Word
        populates the TOC the first time the file is opened. On
        Windows with Word installed, we additionally invoke Word COM
        to populate the TOC server-side so the file opens fully
        rendered.

        #109: render + COM populate moved off the UI thread into
        ``_WordExportWorker``. The COM populate step is the
        user-noticeable slow phase (Word launch + open + field
        update + save), so this is where the 'app frozen' symptom
        shows up most. Same pattern as the PDF worker -- the UI
        thread does the document build + file picker, hands the
        worker the body markdown + params, and stays interactive
        while the worker writes the file.
        """
        if self._word_export_in_flight:
            self.window().statusBar().showMessage(
                "Word export already in progress -- please wait.", 4000,
            )
            return
        doc, tab_label = self._build_print_document(
            appendix_defaults=self._appendix_export_defaults,
        )
        if doc is None or self._session is None:
            return
        from PyQt6.QtWidgets import QFileDialog

        suggested_name = default_export_filename(
            self._session.title, tab_label, ".docx"
        )
        suggested_path = export_initial_save_path(
            self._export_default_folder,
            session_dir(self._session.id),
            suggested_name,
        )
        path_str, _filter = QFileDialog.getSaveFileName(
            self,
            f"Export {tab_label} as Word",
            suggested_path,
            "Word documents (*.docx)",
        )
        if not path_str:
            return
        target = Path(path_str)
        if target.suffix.lower() != ".docx":
            target = target.with_suffix(".docx")

        body_md = getattr(doc, "_mn_body_markdown", "") or ""
        # Drop the UI-thread doc reference -- the worker rebuilds
        # the docx from the markdown body directly (no QTextDocument
        # needed for the Word path).
        del doc

        doc_title = default_export_document_title(
            self._session.title, tab_label,
        )
        worker = _WordExportWorker(
            target_path=target,
            session_dir=session_dir(self._session.id),
            doc_title=doc_title,
            tab_label=tab_label,
            body_markdown=body_md,
            export_toc=self._export_toc,
            export_toc_max_depth=self._export_toc_max_depth,
        )
        self._word_export_in_flight = True
        self._word_export_worker = worker
        self._refresh_export_buttons_for_in_flight()
        self.window().statusBar().showMessage(
            f"Exporting {tab_label} to Word... (this can take a moment)",
        )
        worker.progress_message.connect(self._on_word_export_progress)
        worker.finished_with_result.connect(self._on_word_export_finished)
        worker.start()

    def _on_word_export_progress(self, message: str) -> None:
        self.window().statusBar().showMessage(message)

    def _on_word_export_finished(
        self, success: bool, target_path: str, detail: str,
    ) -> None:
        target = Path(target_path)
        if success:
            via = f" ({detail})" if detail else ""
            self.window().statusBar().showMessage(
                f"Exported Word document to {target.name}{via}", 5000,
            )
        else:
            self.window().statusBar().showMessage(
                f"Word export failed: {detail or 'unknown error'}", 8000,
            )
            log.error("word export failed: %s -> %s", detail, target)
        if self._word_export_worker is not None:
            self._word_export_worker.wait()
            self._word_export_worker.deleteLater()
            self._word_export_worker = None
        self._word_export_in_flight = False
        self._refresh_export_buttons_for_in_flight()

    def _render_pdf_via_word(
        self, *, body_md: str, dst: Path, tab_label: str,
    ) -> bool:
        """Word-COM PDF render path (#94 follow-up).

        Renders markdown -> .docx with a native TOC field, then drives
        Word to populate the TOC and ExportAsFixedFormat -> PDF. Word
        emits a PDF with native sidebar bookmarks and clickable TOC
        hyperlinks via the CreateBookmarks=1 flag.

        Returns True on success, False on any failure. The .docx
        intermediate is written to a temp path next to the PDF and
        cleaned up at the end so the user only sees the PDF.
        """
        if self._session is None or not body_md:
            return False
        from ..utils.word_export import (  # noqa: PLC0415
            export_to_docx,
            export_to_pdf_via_word,
        )
        import tempfile  # noqa: PLC0415

        doc_title = default_export_document_title(
            self._session.title, tab_label,
        )
        with tempfile.TemporaryDirectory() as td:
            tmp_docx = Path(td) / f"{dst.stem}.docx"
            stats = export_to_docx(
                body_md,
                tmp_docx,
                base_dir=session_dir(self._session.id),
                title=doc_title,
                include_toc=self._export_toc,
                toc_max_depth=self._export_toc_max_depth,
            )
            if stats.error:
                return False
            return export_to_pdf_via_word(tmp_docx, dst)


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


def _pretty_state(state: str, *, has_live_transcript: bool = False) -> str:
    # The state-label is the small text to the right of the session
    # title. "Complete" was visual noise -- the session-list status
    # column already shows the green dot for completed sessions, so
    # the label string only carries weight DURING active work. Empty
    # string for STATE_COMPLETE / STATE_NEW lets the label collapse
    # to no horizontal footprint when nothing's happening.
    #
    # PROCESSING gets two phrasings. The "you can synthesize now"
    # variant was correct under the v0.6.4-and-earlier default where
    # live captions populated the transcript pane during recording
    # and the batch pass refined an already-usable live transcript.
    # Under the v0.6.5+ default (live transcription off) nothing is
    # written to the transcript file until batch completes, so the
    # session genuinely cannot be synthesized yet -- the claim that
    # it can is misleading. `has_live_transcript` reflects whether
    # the live engine wrote any segments at Stop (controller flips
    # session.has_transcript=True in that path); when False, the
    # quieter message says only that work is happening.
    if state == STATE_PROCESSING:
        if has_live_transcript:
            return "Refining transcript -- you can synthesize now"
        return "Refining transcript..."
    pretty = {
        STATE_NEW: "",
        STATE_RECORDING: "Recording",
        STATE_PAUSED: "Paused",
        STATE_COMPLETE: "",
        STATE_ERROR: "Error",
    }
    return pretty.get(state, state.title())


class _PdfExportWorker(QThread):
    """Off-UI-thread PDF render + post-process (#109).

    The synchronous ``_on_export_pdf`` path was the user-perceived
    'frozen app' for several seconds during a long export. The
    slow phases are:

      1. ``doc.print(printer)`` -- Qt's PDF writer paginates +
         rasterizes images + lays out paragraphs. Scales with doc
         length.
      2. ``add_pdf_navigation`` -- pypdf post-process that copies
         /Names/Dests entries inline onto each Link annotation so
         PDFium-based viewers (Chrome / Edge) navigate to the
         heading anchor. Scales with #headings + #links.
      3. Word COM path: ``export_to_docx`` + ``export_to_pdf_via_word``
         spawn Word, populate fields, and print. Slowest.

    All three are off-UI-thread-safe: QTextDocument is reentrant
    for new instances, QPrinter to PDF is a paint device (not a
    widget), and pypdf is pure Python. Word COM needs apartment-
    threaded ``pythoncom.CoInitialize`` on the worker, mirroring
    ``_MeetingResolveWorker`` (#106).

    Signals:
      * ``progress_message(str)`` -- status-bar text updates during
        the run (Rendering... / Adding navigation... / etc.).
      * ``finished_with_result(bool, str, str)`` -- (success,
        target_path, detail). ``detail`` is 'via Word' on the Word
        path, '' on the Qt path, or the exception message on
        failure.
    """

    progress_message = pyqtSignal(str)
    finished_with_result = pyqtSignal(bool, str, str)

    def __init__(
        self,
        *,
        target_path: Path,
        session_dir: Path,
        session_title: str,
        tab_label: str,
        printable_markdown: str,
        body_markdown_for_anchors: str,
        use_word: bool,
        export_toc: bool,
        export_heading_numbering: bool,
        export_toc_max_depth: int,
    ) -> None:
        super().__init__()
        self.setObjectName("PdfExportWorker")
        self._target = target_path
        self._session_dir = session_dir
        self._session_title = session_title
        self._tab_label = tab_label
        self._printable_markdown = printable_markdown
        self._body_for_anchors = body_markdown_for_anchors
        self._use_word = use_word
        self._export_toc = export_toc
        self._export_heading_numbering = export_heading_numbering
        self._export_toc_max_depth = export_toc_max_depth

    def run(self) -> None:  # type: ignore[override]
        co_initialized = False
        if self._use_word:
            try:
                import pythoncom  # noqa: PLC0415 -- Windows-only optional
                pythoncom.CoInitialize()
                co_initialized = True
            except Exception:
                # pythoncom absent or already initialized -- the Word
                # branch will check is_word_com_available below
                # anyway. Don't fail the whole worker here.
                pass
        try:
            if self._use_word:
                from ..utils.word_export import is_word_com_available  # noqa: PLC0415
                if is_word_com_available():
                    self.progress_message.emit(
                        f"Rendering {self._tab_label} to PDF (via Word)...",
                    )
                    if self._render_via_word():
                        self.finished_with_result.emit(
                            True, str(self._target), "via Word",
                        )
                        return
                    # Word path failed -- fall through to the Qt
                    # backend so the user still gets a PDF rather
                    # than a silent no-op. Status-bar update tells
                    # them why the path changed.
                    self.progress_message.emit(
                        "Word PDF export failed -- falling back to Qt...",
                    )
            # Qt PDF backend.
            self.progress_message.emit(
                f"Rendering {self._tab_label} to PDF...",
            )
            self._render_via_qt()
            if self._export_toc or self._export_heading_numbering:
                self.progress_message.emit("Adding PDF navigation...")
                self._post_process_navigation()
            self.finished_with_result.emit(True, str(self._target), "")
        except Exception as exc:
            log.exception("pdf export worker failed for %s", self._target)
            self.finished_with_result.emit(
                False, str(self._target), str(exc),
            )
        finally:
            if co_initialized:
                try:
                    import pythoncom  # noqa: PLC0415
                    pythoncom.CoUninitialize()
                except Exception:
                    pass

    def _render_via_qt(self) -> None:
        """Construct a fresh PrintTextDocument on the worker thread +
        print to PDF. QTextDocument is reentrant for new instances;
        we deliberately pass parent=None so the doc isn't tied to a
        UI-thread QObject."""
        from PyQt6.QtPrintSupport import QPrinter  # noqa: PLC0415

        from .print_document import PrintTextDocument  # noqa: PLC0415
        from ..utils.print_html import markdown_to_print_html  # noqa: PLC0415

        doc = PrintTextDocument(self._session_dir, parent=None)
        doc.setHtml(markdown_to_print_html(self._printable_markdown))
        doc.force_anchor_styling()

        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer.setOutputFileName(str(self._target))
        # PDF metadata title is the bare session title (#78).
        printer.setDocName(self._session_title)
        doc.clamp_images_to_printer(printer)
        doc.print(printer)

    def _post_process_navigation(self) -> None:
        if not self._body_for_anchors:
            return
        from ..utils.pdf_post_process import add_pdf_navigation  # noqa: PLC0415

        add_pdf_navigation(
            self._target,
            self._body_for_anchors,
            toc_max_depth=self._export_toc_max_depth,
        )

    def _render_via_word(self) -> bool:
        """Mirror of ``SessionView._render_pdf_via_word`` but worker-
        thread-safe (no ``self._session`` access; all data passed via
        ctor). Returns True on success."""
        if not self._body_for_anchors:
            return False
        import tempfile  # noqa: PLC0415

        from ..utils.word_export import (  # noqa: PLC0415
            export_to_docx,
            export_to_pdf_via_word,
        )

        doc_title = default_export_document_title(
            self._session_title, self._tab_label,
        )
        with tempfile.TemporaryDirectory() as td:
            tmp_docx = Path(td) / f"{self._target.stem}.docx"
            stats = export_to_docx(
                self._body_for_anchors,
                tmp_docx,
                base_dir=self._session_dir,
                title=doc_title,
                include_toc=self._export_toc,
                toc_max_depth=self._export_toc_max_depth,
            )
            if stats.error:
                return False
            return export_to_pdf_via_word(tmp_docx, self._target)


class _WordExportWorker(QThread):
    """Off-UI-thread Word (.docx) render + optional Word COM TOC
    populate (#109).

    The synchronous ``_on_export_word`` flow was the user-
    perceivable freeze: ``export_to_docx`` (python-docx) is several
    seconds on a long doc with images, and the optional
    ``populate_toc_via_word`` step (Windows + Word installed) shells
    out to Word, opens the doc, updates fields, and saves -- often
    the slowest single thing the app does.

    Both phases are off-UI-thread-safe: python-docx is pure Python,
    and Word COM needs the same ``pythoncom.CoInitialize`` /
    ``CoUninitialize`` bookend the calendar resolve worker (#106)
    and PDF export worker established.

    Signals match ``_PdfExportWorker``:
      * ``progress_message(str)`` -- status-bar text updates during
        the run.
      * ``finished_with_result(bool, str, str)`` -- (success,
        target_path, detail). ``detail`` is 'TOC populated' when
        the COM populate step ran successfully, '' when the COM
        path was skipped (no TOC, or no Word COM available), and
        the exception message on failure.
    """

    progress_message = pyqtSignal(str)
    finished_with_result = pyqtSignal(bool, str, str)

    def __init__(
        self,
        *,
        target_path: Path,
        session_dir: Path,
        doc_title: str,
        tab_label: str,
        body_markdown: str,
        export_toc: bool,
        export_toc_max_depth: int,
    ) -> None:
        super().__init__()
        self.setObjectName("WordExportWorker")
        self._target = target_path
        self._session_dir = session_dir
        self._doc_title = doc_title
        self._tab_label = tab_label
        self._body_markdown = body_markdown
        self._export_toc = export_toc
        self._export_toc_max_depth = export_toc_max_depth

    def run(self) -> None:  # type: ignore[override]
        co_initialized = False
        if self._export_toc:
            try:
                import pythoncom  # noqa: PLC0415 -- Windows-only optional
                pythoncom.CoInitialize()
                co_initialized = True
            except Exception:
                # pythoncom absent (non-Windows dev runtime) -- the
                # TOC populate path checks is_word_com_available
                # before running, so this isn't fatal.
                pass
        try:
            from ..utils.word_export import (  # noqa: PLC0415
                export_to_docx,
                is_word_com_available,
                populate_toc_via_word,
            )

            self.progress_message.emit(
                f"Rendering {self._tab_label} to Word...",
            )
            stats = export_to_docx(
                self._body_markdown,
                self._target,
                base_dir=self._session_dir,
                title=self._doc_title,
                include_toc=self._export_toc,
                toc_max_depth=self._export_toc_max_depth,
            )
            if stats.error:
                self.finished_with_result.emit(
                    False, str(self._target), stats.error,
                )
                return
            detail = ""
            if self._export_toc and is_word_com_available():
                self.progress_message.emit(
                    "Populating TOC via Word (this can take a moment)...",
                )
                # Best-effort: even if the populate step fails we
                # still wrote a valid docx via python-docx, and the
                # user can update fields manually in Word. So a COM
                # failure here doesn't fail the whole export -- we
                # just don't tag the success message with 'TOC
                # populated'.
                try:
                    populate_toc_via_word(self._target, save_in_place=True)
                    detail = "TOC populated"
                except Exception as exc:  # noqa: BLE001 -- defensive
                    log.warning(
                        "Word COM TOC populate failed for %s: %s",
                        self._target, exc,
                    )
            self.finished_with_result.emit(True, str(self._target), detail)
        except Exception as exc:
            log.exception("word export worker failed for %s", self._target)
            self.finished_with_result.emit(
                False, str(self._target), str(exc),
            )
        finally:
            if co_initialized:
                try:
                    import pythoncom  # noqa: PLC0415
                    pythoncom.CoUninitialize()
                except Exception:
                    pass
