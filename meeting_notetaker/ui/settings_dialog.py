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
    QLabel,
    QSlider,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtCore import Qt

from ..utils.config import Config, VALID_MODEL_SIZES, VALID_THEMES


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
        self._theme_picker = QComboBox(self)
        for theme in VALID_THEMES:
            self._theme_picker.addItem(theme)
        self._theme_picker.setCurrentText(config.ui.theme)
        ui_form.addRow("Theme:", self._theme_picker)
        layout.addWidget(ui_group)

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
        self._config.audio.retain_audio_default = self._retain_default.isChecked()
        self._config.audio.vad_enabled = self._vad_enabled.isChecked()
        self._config.audio.vad_min_silence_ms = self._vad_slider.value()
        self._config.ui.theme = self._theme_picker.currentText()
        self.accept()
