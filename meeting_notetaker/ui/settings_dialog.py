"""App-level settings dialog.

Exposes: model size, retain-audio default, VAD enable + min-silence threshold,
capture-only mode, theme. Mutates the supplied Config in-place on accept;
caller is responsible for persisting + applying the new values.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices

from ..audio.devices import AudioDevice, list_input_devices, list_loopback_devices
from ..utils.config import Config, VALID_MODEL_SIZES, VALID_THEMES
from ..utils.paths import prompts_dir, vocabulary_path
from ..utils.vocabulary import seed_vocabulary_file


class SettingsDialog(QDialog):
    def __init__(self, config: Config, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.resize(560, 600)
        self._config = config

        # Outer layout: scroll area on top, button bar at the bottom outside
        # the scroll region so OK/Cancel are always reachable. The content
        # widget's layout is what every group below adds to.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll, 1)

        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)

        # Transcription group ---------------------------------------------
        tx_group = QGroupBox("Transcription", self)
        tx_form = QFormLayout(tx_group)

        self._model_picker = QComboBox(self)
        for size in VALID_MODEL_SIZES:
            self._model_picker.addItem(size)
        self._model_picker.setCurrentText(config.transcription.model_size)
        self._model_picker.setToolTip(
            "tiny.en: fastest, lowest accuracy.\n"
            "base.en: small download.\n"
            "small.en: recommended default for English meetings.\n"
            "medium.en: slowest but most accurate; ~3x small.en CPU cost."
        )
        tx_form.addRow("Model size:", self._model_picker)

        self._capture_only = QCheckBox(
            "Capture-only mode (skip live transcription; full pass on stop)", self
        )
        self._capture_only.setChecked(config.transcription.capture_only_mode)
        self._capture_only.setToolTip(
            "Useful for long meetings on a slow CPU. Live view stays empty during the "
            "meeting; full transcript appears after you click Stop."
        )
        tx_form.addRow(self._capture_only)

        self._skip_batch = QCheckBox(
            "Skip post-Stop refinement (use live transcript as final)", self
        )
        self._skip_batch.setChecked(config.transcription.skip_batch_refinement)
        self._skip_batch.setToolTip(
            "After Stop, do NOT re-run Whisper over the full recording. The live "
            "transcript (already on disk) becomes the final transcript. Skips the "
            "long post-meeting wait (a 30-min meeting on small.en is ~30 min of CPU "
            "to re-transcribe). Quality is slightly lower than the batch pass -- "
            "small.en's live windows are 10s with 5s overlap, so cross-sentence "
            "context is shorter than what the batch pass gets. Recommended if you "
            "find the live view's quality acceptable."
        )
        tx_form.addRow(self._skip_batch)

        self._fast_batch = QCheckBox(
            "Fast batch mode (beam_size=1, ~3x faster, slight quality drop)", self
        )
        self._fast_batch.setChecked(config.transcription.fast_batch)
        self._fast_batch.setToolTip(
            "Only applies when the post-Stop refinement is running. Uses greedy "
            "decoding instead of beam search 5. For English-only models the "
            "quality drop is modest -- a handful of incorrect word choices in "
            "noisy audio. Wall-clock is roughly 1/3 of the default. Ignored if "
            "Skip refinement is on."
        )
        tx_form.addRow(self._fast_batch)

        vocab_blurb = QLabel(
            "Custom vocabulary biases the transcriber toward proper nouns and "
            "corporate terms it would otherwise mis-hear (\"Plantronics\", "
            "\"EDAPA-737\", \"Snowflake Cortex\"). One phrase per line; '#' is a "
            "comment. Edits take effect on the next session.",
            self,
        )
        vocab_blurb.setWordWrap(True)
        tx_form.addRow(vocab_blurb)
        self._open_vocab_btn = QPushButton("Open Vocabulary File", self)
        self._open_vocab_btn.clicked.connect(self._open_vocabulary_file)
        tx_form.addRow(self._open_vocab_btn)
        layout.addWidget(tx_group)

        # Audio group ------------------------------------------------------
        audio_group = QGroupBox("Audio", self)
        audio_form = QFormLayout(audio_group)

        self._mic_devices = list_input_devices()
        self._mic_picker = QComboBox(self)
        _populate_device_picker(
            self._mic_picker, self._mic_devices, config.audio.mic_device_name
        )
        self._mic_picker.setToolTip(
            "Microphone capture device. (System default) follows the OS-level "
            "default, which is usually correct. Persists by name so the same "
            "device is picked after replug or reboot."
        )
        audio_form.addRow("Mic device:", self._mic_picker)

        self._loopback_devices = list_loopback_devices()
        self._loopback_picker = QComboBox(self)
        _populate_device_picker(
            self._loopback_picker, self._loopback_devices, config.audio.loopback_device_name
        )
        if not self._loopback_devices:
            # Not on Windows, or pyaudiowpatch missing -- nothing meaningful to pick.
            self._loopback_picker.setEnabled(False)
            self._loopback_picker.setToolTip(
                "System-audio loopback is Windows-only (requires pyaudiowpatch). "
                "Disabled on this platform."
            )
        else:
            self._loopback_picker.setToolTip(
                "System audio capture (WASAPI loopback). (System default) picks "
                "the loopback paired with the OS default output."
            )
        audio_form.addRow("Loopback device:", self._loopback_picker)

        self._retain_default = QCheckBox(
            "Retain audio files after transcription (default for new sessions)", self
        )
        self._retain_default.setChecked(config.audio.retain_audio_default)
        self._retain_default.setToolTip(
            "Default value of the 'Keep recording' checkbox in the New Session dialog. "
            "Can be overridden per session."
        )
        audio_form.addRow(self._retain_default)

        self._vad_enabled = QCheckBox("Enable VAD trimming (recommended)", self)
        self._vad_enabled.setChecked(config.audio.vad_enabled)
        self._vad_enabled.setToolTip(
            "When on, faster-whisper trims silent stretches before decoding. "
            "Saves CPU on quiet loopback streams. Disable if it clips speech."
        )
        audio_form.addRow(self._vad_enabled)

        self._vad_slider = QSlider(Qt.Orientation.Horizontal, self)
        self._vad_slider.setMinimum(100)
        self._vad_slider.setMaximum(2000)
        self._vad_slider.setSingleStep(50)
        self._vad_slider.setPageStep(100)
        self._vad_slider.setValue(config.audio.vad_min_silence_ms)
        self._vad_value_label = QLabel(f"{config.audio.vad_min_silence_ms} ms", self)
        self._vad_slider.valueChanged.connect(
            lambda v: self._vad_value_label.setText(f"{v} ms")
        )
        audio_form.addRow("VAD min silence:", self._vad_slider)
        audio_form.addRow("", self._vad_value_label)
        layout.addWidget(audio_group)

        # Calendar group ---------------------------------------------------
        calendar_group = QGroupBox("Calendar (Outlook)", self)
        calendar_form = QFormLayout(calendar_group)
        calendar_blurb = QLabel(
            "When enabled, the app polls your local Outlook profile (via COM, "
            "no network calls) and pops a tray notification a few minutes "
            "before each meeting starts. Click the notification to open New "
            "Session pre-filled with the meeting subject, attendees, and "
            "agenda. Recording is never started automatically -- you click "
            "Start when you're ready.",
            self,
        )
        calendar_blurb.setWordWrap(True)
        calendar_form.addRow(calendar_blurb)

        self._watch_calendar = QCheckBox("Watch Outlook calendar", self)
        self._watch_calendar.setChecked(config.calendar.watch_calendar)
        self._watch_calendar.setToolTip(
            "Requires Outlook installed and running on this machine. "
            "Windows-only; safely no-ops on other platforms."
        )
        calendar_form.addRow(self._watch_calendar)

        self._window_slider = QSlider(Qt.Orientation.Horizontal, self)
        self._window_slider.setMinimum(1)
        self._window_slider.setMaximum(30)
        self._window_slider.setSingleStep(1)
        self._window_slider.setValue(config.calendar.window_minutes)
        self._window_value_label = QLabel(
            f"{config.calendar.window_minutes} min", self
        )
        self._window_slider.valueChanged.connect(
            lambda v: self._window_value_label.setText(f"{v} min")
        )
        calendar_form.addRow("Notify within:", self._window_slider)
        calendar_form.addRow("", self._window_value_label)
        layout.addWidget(calendar_group)

        # UI group ---------------------------------------------------------
        ui_group = QGroupBox("Interface", self)
        ui_form = QFormLayout(ui_group)
        self._user_name_edit = QLineEdit(self)
        self._user_name_edit.setPlaceholderText("e.g. John Smith (leave blank for \"Me\")")
        self._user_name_edit.setText(config.ui.user_name)
        self._user_name_edit.setToolTip(
            "How the user's microphone is labeled in the transcript and synthesis "
            "prompt. When set, the LLM sees your real name and can attribute action "
            "items to you instead of \"Me\" or \"TBD\". On-disk transcripts always "
            "store the neutral \"Me:\" label and get rewritten on display + at "
            "prompt-generation time."
        )
        ui_form.addRow("Your name:", self._user_name_edit)
        self._theme_picker = QComboBox(self)
        for theme in VALID_THEMES:
            self._theme_picker.addItem(theme)
        self._theme_picker.setCurrentText(config.ui.theme)
        ui_form.addRow("Theme:", self._theme_picker)
        layout.addWidget(ui_group)

        # Prompts group ----------------------------------------------------
        prompts_group = QGroupBox("Synthesis Prompts", self)
        prompts_layout = QVBoxLayout(prompts_group)
        prompts_blurb = QLabel(
            "Prompt templates are Markdown files in the folder below. Edit any file "
            "to change wording, or drop in a new .md file to add a template -- it "
            "appears in the Generate Synthesis Prompt picker on next open. Placeholders: "
            "{{session_title}}, {{date}}, {{transcript}}, {{live_notes}}, {{attendees}}.",
            self,
        )
        prompts_blurb.setWordWrap(True)
        prompts_layout.addWidget(prompts_blurb)
        prompts_row = QHBoxLayout()
        self._open_prompts_btn = QPushButton("Open Prompts Folder", self)
        self._open_prompts_btn.clicked.connect(self._open_prompts_folder)
        prompts_row.addWidget(self._open_prompts_btn)
        prompts_row.addStretch(1)
        prompts_layout.addLayout(prompts_row)
        layout.addWidget(prompts_group)

        layout.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        # Button bar lives outside the scroll area so OK/Cancel are always
        # reachable, even if the content overflows.
        outer.addWidget(buttons)

    def _on_accept(self) -> None:
        self._config.transcription.model_size = self._model_picker.currentText()
        self._config.transcription.capture_only_mode = self._capture_only.isChecked()
        self._config.transcription.skip_batch_refinement = self._skip_batch.isChecked()
        self._config.transcription.fast_batch = self._fast_batch.isChecked()
        self._config.audio.retain_audio_default = self._retain_default.isChecked()
        self._config.audio.vad_enabled = self._vad_enabled.isChecked()
        self._config.audio.vad_min_silence_ms = self._vad_slider.value()
        self._config.audio.mic_device_name = self._mic_picker.currentData() or ""
        self._config.audio.loopback_device_name = self._loopback_picker.currentData() or ""
        self._config.calendar.watch_calendar = self._watch_calendar.isChecked()
        self._config.calendar.window_minutes = int(self._window_slider.value())
        self._config.ui.theme = self._theme_picker.currentText()
        self._config.ui.user_name = self._user_name_edit.text().strip()
        self.accept()

    def _open_prompts_folder(self) -> None:
        path = prompts_dir()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _open_vocabulary_file(self) -> None:
        path = seed_vocabulary_file()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def _populate_device_picker(
    combo: QComboBox, devices: list[AudioDevice], saved_name: str
) -> None:
    """Fill the combobox with (System default) + each device. Selects saved_name.

    If saved_name doesn't match any currently-available device, prepend a
    "(saved: <name>) -- not currently connected" item so the user sees their
    saved selection wasn't lost when they reopen Settings without the device
    plugged in. Selecting another option overwrites it on accept.
    """
    combo.clear()
    combo.addItem("(System default)", "")

    saved = (saved_name or "").strip()
    matched_index: int | None = None
    saved_lower = saved.lower()
    # Decide if the saved name matches an available device (matches the
    # resolution policy in devices.resolve_device_index).
    if saved:
        for d in devices:
            if d.name == saved:
                matched_index = d.index
                break
        if matched_index is None:
            for d in devices:
                if d.name.lower() == saved_lower:
                    matched_index = d.index
                    break
        if matched_index is None:
            for d in devices:
                if saved_lower in d.name.lower():
                    matched_index = d.index
                    break

    for d in devices:
        suffix = f"  [#{d.index}]"
        combo.addItem(d.name + suffix, d.name)

    if saved and matched_index is None:
        combo.addItem(f"(saved: {saved}) -- not currently connected", saved)
        combo.setCurrentIndex(combo.count() - 1)
    elif saved:
        # Select the entry whose userData matches the saved name (preferring
        # exact device-name match if present, otherwise the substring one).
        for i in range(combo.count()):
            if combo.itemData(i) == saved:
                combo.setCurrentIndex(i)
                return
        # Fall through: substring match -- select the first device whose name
        # contains saved.
        for i in range(1, combo.count()):
            data = combo.itemData(i) or ""
            if saved_lower in data.lower():
                combo.setCurrentIndex(i)
                return
