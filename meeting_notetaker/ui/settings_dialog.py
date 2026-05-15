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
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices

from ..utils.config import Config, VALID_MODEL_SIZES, VALID_THEMES
from ..utils.paths import prompts_dir


class SettingsDialog(QDialog):
    def __init__(self, config: Config, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.resize(520, 460)
        self._config = config

        layout = QVBoxLayout(self)

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
        layout.addWidget(tx_group)

        # Audio group ------------------------------------------------------
        audio_group = QGroupBox("Audio", self)
        audio_form = QFormLayout(audio_group)

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
        prompts_layout.addWidget(QLabel(
            "Prompt templates are Markdown files in the folder below. Edit any file "
            "to change wording, or drop in a new .md file to add a template -- it "
            "appears in the Generate Synthesis Prompt picker on next open. Placeholders: "
            "{{session_title}}, {{date}}, {{transcript}}, {{live_notes}}, {{attendees}}.",
            self,
        ))
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
        layout.addWidget(buttons)

    def _on_accept(self) -> None:
        self._config.transcription.model_size = self._model_picker.currentText()
        self._config.transcription.capture_only_mode = self._capture_only.isChecked()
        self._config.transcription.skip_batch_refinement = self._skip_batch.isChecked()
        self._config.transcription.fast_batch = self._fast_batch.isChecked()
        self._config.audio.retain_audio_default = self._retain_default.isChecked()
        self._config.audio.vad_enabled = self._vad_enabled.isChecked()
        self._config.audio.vad_min_silence_ms = self._vad_slider.value()
        self._config.ui.theme = self._theme_picker.currentText()
        self._config.ui.user_name = self._user_name_edit.text().strip()
        self.accept()

    def _open_prompts_folder(self) -> None:
        path = prompts_dir()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
