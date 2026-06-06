"""ImportTranscriptDialog -- bring an external transcript into a session.

User flow (#80):

  1. Click "From file..." or "Paste from clipboard".
  2. The dialog reads the source, runs `normalize_text` (with the
     Strip Teams formatting toggle on by default), and shows the
     result in the preview pane.
  3. If the preview contains "Name:" speaker prefixes, a Speakers
     table appears under the preview. The user can type a remapped
     label per row, click Apply, and the preview redraws.
  4. OK returns the final body (+ the speaker mapping for diagnostics);
     MainApp writes it to raw.transcript.md and flips
     session.has_transcript=True.

The dialog never touches the session or the store directly -- it is a
pure read-from-input / write-to-result widget so the unit tests can
exercise it without an app context.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..integrations.transcript_import import (
    ALL_FORMATS,
    FORMAT_PLAIN,
    FORMAT_SRT,
    FORMAT_VTT,
    FORMAT_WHISPER_JSON,
    TranscriptImportError,
    apply_speaker_map,
    detect_format,
    detect_speakers,
    iter_speakers_with_counts,
    load_transcript_from_file,
    normalize_text,
    parse_srt,
    parse_vtt,
    parse_whisper_json,
    segments_to_transcript_md,
)


_FILE_FILTER = (
    "Transcript files (*.txt *.md *.docx *.vtt *.srt *.json);;"
    "Text (*.txt *.md);;"
    "Word (*.docx);;"
    "WebVTT (*.vtt);;"
    "SubRip (*.srt);;"
    "Whisper JSON (*.json);;"
    "All files (*)"
)


# Format dropdown labels + the FORMAT_* constants they map to. "Auto"
# is the sentinel that runs detect_format on the loaded body.
_FORMAT_AUTO = "auto"
_FORMAT_PICKER_ENTRIES: list[tuple[str, str]] = [
    ("Auto-detect", _FORMAT_AUTO),
    ("Plain text / Markdown", FORMAT_PLAIN),
    ("WebVTT (.vtt)", FORMAT_VTT),
    ("SubRip (.srt)", FORMAT_SRT),
    ("Whisper JSON", FORMAT_WHISPER_JSON),
]


class ImportTranscriptDialog(QDialog):
    """Two-source picker + preview + optional speaker remap.

    Public surface (used by MainApp):
      - `result_body` / `result_mapping` after exec()
      - `set_initial_body(body)` for tests + clipboard preseed
    """

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        session_title: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import Transcript")
        self.setModal(True)
        self.resize(820, 640)

        # The raw text we last loaded (before normalize + remap). Kept
        # so the user can toggle "Strip Teams formatting" without
        # re-reading the file.
        self._source_body: str = ""
        # Optional path the source body was loaded from. Lets the
        # auto-detect heuristic use the extension as a strong hint
        # when the user picks a file (vs the weaker content-sniff
        # only when they paste).
        self._source_path: Optional[Path] = None
        # Cached normalized body so speaker-detect + apply are cheap.
        self._normalized_body: str = ""
        # Result fields populated on accept.
        self.result_body: str = ""
        self.result_mapping: dict[str, str] = {}

        outer = QVBoxLayout(self)
        if session_title:
            header = QLabel(
                f"Import a transcript into <b>{_escape(session_title)}</b>. "
                "After import, the Send to Claude.ai and Save to... buttons "
                "evaluate the same way they do for recorded sessions.",
                self,
            )
        else:
            header = QLabel(
                "Import a transcript into the selected session. After import, "
                "the Send to Claude.ai and Save to... buttons evaluate the "
                "same way they do for recorded sessions.",
                self,
            )
        header.setWordWrap(True)
        outer.addWidget(header)

        # --- Source row: From file / Paste from clipboard --------------
        source_row = QHBoxLayout()
        self._from_file_btn = QPushButton("From file...", self)
        self._from_file_btn.clicked.connect(self._on_from_file)
        self._paste_btn = QPushButton("Paste from clipboard", self)
        self._paste_btn.clicked.connect(self._on_paste_clipboard)
        source_row.addWidget(self._from_file_btn)
        source_row.addWidget(self._paste_btn)
        source_row.addStretch(1)
        self._strip_chrome = QCheckBox("Strip Teams formatting", self)
        self._strip_chrome.setChecked(True)
        self._strip_chrome.setToolTip(
            "Drop Teams banners ('Started transcription', 'View original "
            "meeting'), fold split timestamp lines into the preceding "
            "speaker label, and collapse runs of blank lines. Only "
            "applies to plain text imports; structured formats (VTT, "
            "SRT, Whisper JSON) skip this pass."
        )
        self._strip_chrome.stateChanged.connect(self._on_strip_toggle)
        source_row.addWidget(self._strip_chrome)
        outer.addLayout(source_row)

        # --- Format row (v0.7.8): force a parser when auto-detect
        # misfires, or to recognize a .txt file that's actually
        # VTT/SRT pasted in.
        format_row = QHBoxLayout()
        format_row.addWidget(QLabel("Format:", self))
        self._format_picker = QComboBox(self)
        for label, key in _FORMAT_PICKER_ENTRIES:
            self._format_picker.addItem(label, key)
        self._format_picker.setCurrentIndex(0)  # Auto-detect default
        self._format_picker.setToolTip(
            "Plain text and Markdown get the Teams-cleanup pass. "
            "WebVTT / SubRip / Whisper JSON get parsed into "
            "timestamped lines so the player can seek to anything "
            "you click. Auto-detect uses the file extension and "
            "content sniff."
        )
        self._format_picker.currentIndexChanged.connect(
            self._on_format_changed,
        )
        format_row.addWidget(self._format_picker)
        format_row.addStretch(1)
        # Status indicator: when auto-detect chooses a non-plain
        # format, surface what it picked so the user can override.
        self._format_status = QLabel("", self)
        self._format_status.setStyleSheet(
            "color: palette(placeholder-text); font-style: italic;"
        )
        format_row.addWidget(self._format_status)
        outer.addLayout(format_row)

        self._status_label = QLabel("", self)
        self._status_label.setStyleSheet("color: palette(placeholder-text);")
        outer.addWidget(self._status_label)

        # --- Preview pane ------------------------------------------------
        self._preview = QPlainTextEdit(self)
        self._preview.setReadOnly(True)
        self._preview.setPlaceholderText(
            "Pick a file or paste from the clipboard. The normalized "
            "transcript preview will appear here."
        )
        self._preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        outer.addWidget(self._preview, 1)

        # --- Speakers table (hidden until something is detected) -------
        self._speakers_frame = QFrame(self)
        speakers_layout = QVBoxLayout(self._speakers_frame)
        speakers_layout.setContentsMargins(0, 6, 0, 0)
        speakers_layout.addWidget(QLabel(
            "Speakers detected (optional remap). Leave blank to keep "
            "the original label.",
            self,
        ))
        self._speakers_table = QTableWidget(0, 3, self)
        self._speakers_table.setHorizontalHeaderLabels([
            "Original label", "Lines", "Remap to",
        ])
        self._speakers_table.verticalHeader().setVisible(False)
        header_view = self._speakers_table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._speakers_table.setEditTriggers(
            QTableWidget.EditTrigger.AllEditTriggers
        )
        speakers_layout.addWidget(self._speakers_table)
        speakers_row = QHBoxLayout()
        speakers_row.addStretch(1)
        self._apply_remap_btn = QPushButton("Apply remap to preview", self)
        self._apply_remap_btn.clicked.connect(self._on_apply_remap)
        speakers_row.addWidget(self._apply_remap_btn)
        speakers_layout.addLayout(speakers_row)
        self._speakers_frame.setVisible(False)
        outer.addWidget(self._speakers_frame)

        # --- Buttons ----------------------------------------------------
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self._ok_btn = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_btn.setText("Import")
        self._ok_btn.setEnabled(False)
        self._buttons.accepted.connect(self._on_accept)
        self._buttons.rejected.connect(self.reject)
        outer.addWidget(self._buttons)

    # --------- public API for tests + preseed ----------------------------
    def set_initial_body(self, body: str) -> None:
        """Inject a body as if the user had just loaded it. Used by
        tests and by the clipboard-preseed path when MainApp opens the
        dialog with the clipboard already populated."""
        self._source_body = body
        self._source_path = None
        self._rerender()

    # --------- source actions -------------------------------------------
    def _on_from_file(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Select transcript file",
            "",
            _FILE_FILTER,
        )
        if not path_str:
            return
        try:
            body = load_transcript_from_file(Path(path_str))
        except TranscriptImportError as exc:
            QMessageBox.warning(self, "Import Transcript", exc.reason)
            return
        if not body.strip():
            QMessageBox.information(
                self,
                "Import Transcript",
                "That file appears to be empty.",
            )
            return
        self._source_body = body
        self._source_path = Path(path_str)
        self._status_label.setText(f"Loaded {Path(path_str).name}")
        self._rerender()

    def _on_paste_clipboard(self) -> None:
        clip = QGuiApplication.clipboard()
        if clip is None:
            QMessageBox.warning(
                self, "Import Transcript",
                "Clipboard is not available.",
            )
            return
        text = clip.text()
        if not text.strip():
            QMessageBox.information(
                self, "Import Transcript",
                "The clipboard is empty. Copy the transcript first, then click Paste from clipboard.",
            )
            return
        self._source_body = text
        self._source_path = None
        self._status_label.setText(
            f"Pasted {len(text):,} characters from clipboard"
        )
        self._rerender()

    def _on_strip_toggle(self, _state: int) -> None:
        if self._source_body:
            self._rerender()

    def _on_format_changed(self, _index: int) -> None:
        if self._source_body:
            self._rerender()

    def _on_apply_remap(self) -> None:
        mapping = self._read_speakers_table()
        if not any(v.strip() for v in mapping.values()):
            return
        self._normalized_body = apply_speaker_map(
            self._normalized_body, mapping,
        )
        self._preview.setPlainText(self._normalized_body)
        # Detect again so removed labels disappear + the new ones
        # become editable too.
        self._populate_speakers_table(self._normalized_body)

    # --------- rendering -------------------------------------------------
    def _resolve_format(self) -> str:
        """Return the effective format for the current source body.

        ``_FORMAT_AUTO`` runs detect_format with the optional path
        hint; any explicit pick wins. Public so tests can assert
        the resolved choice without driving the combobox."""
        chosen = self._format_picker.currentData()
        if chosen != _FORMAT_AUTO:
            return chosen
        return detect_format(self._source_body, filename=self._source_path)

    def _rerender(self) -> None:
        """Refresh the preview + speakers from the source body.

        Branches on format: plain text runs the existing
        normalize_text + Teams-cleanup pass; structured formats
        (VTT, SRT, Whisper JSON) parse to TranscriptCue and render
        through segments_to_transcript_md so the player's
        ``[HH:MM:SS] Label: text`` shape carries timestamps that
        drive playback sync after the audio is added.
        """
        fmt = self._resolve_format()
        # Strip-Teams toggle is only meaningful for plain bodies;
        # disable + dim it when a structured format is active so
        # the user isn't misled into thinking it does anything.
        is_plain = fmt == FORMAT_PLAIN
        self._strip_chrome.setEnabled(is_plain)
        if not is_plain:
            self._strip_chrome.setToolTip(
                "Disabled for structured formats; the parser strips "
                "format-specific noise on its own."
            )
        else:
            self._strip_chrome.setToolTip(
                "Drop Teams banners ('Started transcription', 'View "
                "original meeting'), fold split timestamp lines into "
                "the preceding speaker label, and collapse runs of "
                "blank lines."
            )
        # Status text reflects auto-detect's pick (vs an explicit
        # override which the user already sees in the combobox).
        chosen_label = (
            "auto-detected"
            if self._format_picker.currentData() == _FORMAT_AUTO
            else "selected"
        )
        self._format_status.setText(
            f"{chosen_label}: {_format_label_for_key(fmt)}"
        )

        try:
            body = self._render_body_for_format(fmt)
        except TranscriptImportError as exc:
            self._normalized_body = ""
            self._preview.setPlainText(
                f"Could not parse as {_format_label_for_key(fmt)}.\n\n"
                f"{exc.reason}\n\n"
                "Pick a different format or fall back to Plain text "
                "from the Format dropdown above."
            )
            self._ok_btn.setEnabled(False)
            self._populate_speakers_table("")
            return

        self._normalized_body = body
        self._preview.setPlainText(body)
        self._ok_btn.setEnabled(bool(body.strip()))
        self._populate_speakers_table(body)

    def _render_body_for_format(self, fmt: str) -> str:
        """Dispatch the source body through the right parser +
        renderer for ``fmt``. Raises TranscriptImportError when a
        structured format's input is malformed."""
        if fmt == FORMAT_PLAIN:
            return normalize_text(
                self._source_body,
                strip_teams_chrome=self._strip_chrome.isChecked(),
            )
        if fmt == FORMAT_VTT:
            cues = parse_vtt(self._source_body)
            return segments_to_transcript_md(cues, default_speaker="Speaker")
        if fmt == FORMAT_SRT:
            cues = parse_srt(self._source_body)
            return segments_to_transcript_md(cues, default_speaker="Speaker")
        if fmt == FORMAT_WHISPER_JSON:
            cues = parse_whisper_json(self._source_body)
            return segments_to_transcript_md(cues, default_speaker="Speaker")
        # Unknown format -- safest fall-back is the plain pass.
        return normalize_text(
            self._source_body,
            strip_teams_chrome=self._strip_chrome.isChecked(),
        )

    def _populate_speakers_table(self, body: str) -> None:
        speakers = detect_speakers(body)
        counts = dict(iter_speakers_with_counts(body))
        self._speakers_table.setRowCount(len(speakers))
        for row, sp in enumerate(speakers):
            label_item = QTableWidgetItem(sp.name)
            label_item.setFlags(label_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            count_item = QTableWidgetItem(str(counts.get(sp.name, 0)))
            count_item.setFlags(count_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            remap_item = QTableWidgetItem("")
            self._speakers_table.setItem(row, 0, label_item)
            self._speakers_table.setItem(row, 1, count_item)
            self._speakers_table.setItem(row, 2, remap_item)
        self._speakers_frame.setVisible(bool(speakers))

    def _read_speakers_table(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for row in range(self._speakers_table.rowCount()):
            label_item = self._speakers_table.item(row, 0)
            remap_item = self._speakers_table.item(row, 2)
            if label_item is None:
                continue
            mapping[label_item.text()] = (
                remap_item.text().strip() if remap_item else ""
            )
        return mapping

    # --------- accept ----------------------------------------------------
    def _on_accept(self) -> None:
        body = self._preview.toPlainText()
        if not body.strip():
            QMessageBox.information(
                self, "Import Transcript",
                "There is nothing to import.",
            )
            return
        # Capture any final-edit speaker remap.
        mapping = self._read_speakers_table()
        effective = {k: v for k, v in mapping.items() if v.strip() and v != k}
        if effective:
            body = apply_speaker_map(body, effective)
        self.result_body = body
        self.result_mapping = effective
        self.accept()


def _escape(text: str) -> str:
    """Tiny HTML-escape so a session title with '<' / '&' stays literal in the header label."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _format_label_for_key(key: str) -> str:
    """Return the user-facing label for a FORMAT_* key. Used by
    the status indicator under the Format dropdown."""
    for label, k in _FORMAT_PICKER_ENTRIES:
        if k == key:
            return label
    return key
