"""Per-session four-pane view: transcript + my-notes + synthesis + previous-notes."""
from __future__ import annotations

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
    QTextBrowser,
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
from ..utils.paths import session_dir
from .live_notes_widget import LiveNotesWidget


class SessionView(QWidget):
    """Right-hand pane shown when a session is selected."""

    start_clicked = pyqtSignal(str)               # session_id
    pause_clicked = pyqtSignal(str)
    resume_clicked = pyqtSignal(str)
    stop_clicked = pyqtSignal(str)
    generate_prompt_clicked = pyqtSignal(str)
    paste_notes_clicked = pyqtSignal(str)
    copy_notes_clicked = pyqtSignal(str)
    retain_audio_toggled = pyqtSignal(str, bool)  # session_id, value
    live_notes_changed = pyqtSignal(str, str)     # session_id, body

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._session: Optional[Session] = None
        self._provisional_segments: dict[tuple[str, float], int] = {}
        # Maps (source, t_start) -> line index in the transcript view.
        self._user_name = ""
        self._raw_transcript_text = ""
        self._live_notes_save_timer = QTimer(self)
        self._live_notes_save_timer.setSingleShot(True)
        self._live_notes_save_timer.setInterval(800)
        self._live_notes_save_timer.timeout.connect(self._flush_live_notes)
        self._suppress_live_notes_signal = False

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
        self._copy_notes_btn = QPushButton("Copy Notes to Clipboard", self)
        self._copy_notes_btn.clicked.connect(self._on_copy_notes)
        synthesis.addWidget(self._copy_notes_btn)
        self._print_btn = QPushButton("Print...", self)
        self._print_btn.setToolTip(
            "Print the active tab (My Notes or Synthesis). Use the printer "
            "dialog's destination dropdown to pick \"Microsoft Print to PDF\" "
            "for a PDF copy."
        )
        self._print_btn.clicked.connect(self._on_print)
        synthesis.addWidget(self._print_btn)
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

        self._notes_view = QTextBrowser(self)
        self._notes_view.setOpenExternalLinks(True)
        self._tabs.addTab(self._notes_view, "Synthesis")

        self._previous_view = QPlainTextEdit(self)
        self._previous_view.setReadOnly(True)
        self._tabs.addTab(self._previous_view, "Previous Notes")

        layout.addWidget(self._tabs, 1)
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
        # Flush any pending live-notes save before swapping out the editor.
        if self._live_notes_save_timer.isActive():
            self._live_notes_save_timer.stop()
            self._flush_live_notes()
        self._session = session
        self._provisional_segments.clear()
        if session is None:
            self._title_label.setText("(no session)")
            self._state_label.setText("")
            self._raw_transcript_text = ""
            self._transcript_view.setPlainText("")
            self._notes_view.setSearchPaths([])
            self._notes_view.setMarkdown("")
            self._previous_view.setPlainText("")
            self._live_notes_editor.set_session_dir(None)
            self._set_live_notes_text("")
            self._retain_checkbox.setChecked(False)
            self._retain_checkbox.setEnabled(False)
            self._set_buttons_for_state(STATE_NEW, has_transcript=False, has_notes=False)
            return
        self._title_label.setText(session.title)
        self._state_label.setText(_pretty_state(session.state))
        self._raw_transcript_text = transcript
        self._transcript_view.setPlainText(rewrite_user_label(transcript, self._user_name))
        sdir = session_dir(session.id)
        self._notes_view.setSearchPaths([str(sdir)])
        self._notes_view.setMarkdown(notes)
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
        self._notes_view.setMarkdown(text)

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

    def _set_live_notes_text(self, text: str) -> None:
        self._suppress_live_notes_signal = True
        try:
            self._live_notes_editor.setPlainText(text)
        finally:
            self._suppress_live_notes_signal = False

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

    def _on_copy_notes(self) -> None:
        if self._session:
            self.copy_notes_clicked.emit(self._session.id)

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
        self._copy_notes_btn.setEnabled(has_session and has_notes)
        self._update_print_button(has_session=has_session, has_notes=has_notes)

    def _on_tab_changed(self, _index: int) -> None:
        has_session = self._session is not None
        has_notes = bool(
            self._session and (self._session.has_notes or self._notes_view.toPlainText().strip())
        )
        self._update_print_button(has_session=has_session, has_notes=has_notes)

    def _update_print_button(self, *, has_session: bool, has_notes: bool) -> None:
        """Print is only meaningful on My Notes / Synthesis tabs."""
        if not has_session:
            self._print_btn.setEnabled(False)
            return
        current = self._tabs.currentWidget()
        if current is self._live_notes_editor:
            # Always allow printing the user's own notes (even if empty -- the
            # printer dialog and a blank page is harmless).
            self._print_btn.setEnabled(True)
        elif current is self._notes_view:
            self._print_btn.setEnabled(has_notes)
        else:
            self._print_btn.setEnabled(False)

    def _on_print(self) -> None:
        """Print the active tab via QPrinter.

        QPrintDialog exposes every installed Windows printer, including
        "Microsoft Print to PDF" -- selecting it from the destination
        dropdown is the standard path for saving to PDF. Markdown is
        rendered into a temporary QTextDocument so the printer output
        reflects the rendered view rather than the raw source.
        """
        if self._session is None:
            return
        current = self._tabs.currentWidget()
        if current is self._live_notes_editor:
            markdown_source = self._live_notes_editor.toPlainText()
            doc_title = f"{self._session.title} -- My Notes"
        elif current is self._notes_view:
            markdown_source = self._notes_view.toMarkdown()
            doc_title = f"{self._session.title} -- Synthesis"
        else:
            return

        # Lazy-imported so headless test envs without QtPrintSupport still
        # import this module cleanly.
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QTextDocument
        from PyQt6.QtPrintSupport import QPrintDialog, QPrinter

        doc = QTextDocument(self)
        sdir = session_dir(self._session.id)
        doc.setBaseUrl(QUrl.fromLocalFile(str(sdir) + "/"))
        doc.setMarkdown(markdown_source)

        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setDocName(doc_title)
        dialog = QPrintDialog(printer, self)
        dialog.setWindowTitle(f"Print -- {doc_title}")
        if dialog.exec() != QPrintDialog.DialogCode.Accepted:
            return
        doc.print(printer)


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
