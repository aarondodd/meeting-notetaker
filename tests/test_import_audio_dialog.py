"""Tests for the ImportAudioDialog (#88).

Drives the dialog's UI surface offscreen via QT_QPA_PLATFORM=offscreen.
The dialog's heavy lifting (PyAV decode) is tested in
`test_import_audio.py`; here we cover the picker -> metadata panel ->
treatment-dropdown flow and the speaker-treatment -> slot mapping.
"""
from __future__ import annotations

import os
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QDialogButtonBox  # noqa: E402

from meeting_notetaker.ui.import_audio_dialog import (  # noqa: E402
    ALL_SPEAKER_TREATMENTS,
    ImportAudioDialog,
    SPEAKER_TREATMENT_COMBINED,
    SPEAKER_TREATMENT_MY_VOICE,
    SPEAKER_TREATMENT_OTHERS,
    build_audio_file_filter,
)


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


def _write_sine_wav(path: Path, *, duration_sec: float = 1.0) -> Path:
    n = int(duration_sec * 48000)
    t = np.arange(n) / 48000
    sine = (np.sin(2 * np.pi * 440 * t) * 16000).astype(np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(48000)
        wf.writeframes(sine.tobytes())
    return path


# ---- file filter --------------------------------------------------------

def test_file_filter_lists_supported_extensions():
    f = build_audio_file_filter()
    assert "*.wav" in f
    assert "*.mp3" in f
    assert "*.m4a" in f
    assert "*.opus" in f
    assert "*.flac" in f
    assert "*.mp4" in f
    # Two-section filter: typed group + everything fallback.
    assert "All files" in f


# ---- speaker treatment mapping ------------------------------------------

def test_three_speaker_treatments():
    """Exactly three treatments wired -- changing this requires touching
    the dialog UI and tests, so a regression is loud."""
    assert len(ALL_SPEAKER_TREATMENTS) == 3


def test_combined_treatment_targets_sys_slot_with_diarization():
    label, slot, diarize = SPEAKER_TREATMENT_COMBINED
    assert slot == "sys"
    assert diarize is True


def test_my_voice_treatment_targets_mic_slot_without_diarization():
    label, slot, diarize = SPEAKER_TREATMENT_MY_VOICE
    assert slot == "mic"
    assert diarize is False


def test_others_treatment_targets_sys_slot_with_diarization():
    label, slot, diarize = SPEAKER_TREATMENT_OTHERS
    assert slot == "sys"
    assert diarize is True


# ---- dialog initial state -----------------------------------------------

def test_dialog_starts_with_disabled_ok_button(qt_app, tmp_path):
    dlg = ImportAudioDialog(tmp_path / "audio")
    ok = dlg._buttons.button(QDialogButtonBox.StandardButton.Ok)  # noqa: SLF001
    assert not ok.isEnabled()


def test_dialog_metadata_panel_starts_with_placeholder(qt_app, tmp_path):
    dlg = ImportAudioDialog(tmp_path / "audio")
    text = dlg._meta_label.text()  # noqa: SLF001
    assert "Choose a file" in text


def test_dialog_treatment_picker_lists_three_options(qt_app, tmp_path):
    dlg = ImportAudioDialog(tmp_path / "audio")
    assert dlg._treatment_picker.count() == 3  # noqa: SLF001


# ---- post-pick metadata refresh ----------------------------------------

def test_source_pick_refreshes_metadata_panel(qt_app, tmp_path):
    src = _write_sine_wav(tmp_path / "sine.wav", duration_sec=1.5)
    dlg = ImportAudioDialog(tmp_path / "audio")
    # Simulate the file picker's outcome: set source_path + drive the
    # refresh helper. The QFileDialog itself isn't tested here.
    dlg._source_path = src  # noqa: SLF001
    dlg._refresh_meta_panel()  # noqa: SLF001
    text = dlg._meta_label.text()  # noqa: SLF001
    assert "WAV" in text  # extension surfaces uppercase
    assert "48,000" in text or "48000" in text  # sample rate
    assert "mono" in text  # downmix label


def test_source_pick_metadata_panel_handles_missing_pyav_gracefully(qt_app, tmp_path):
    """If describe_source can't read the file (PyAV missing or file
    corrupted), the panel surfaces a fallback message instead of
    crashing."""
    dlg = ImportAudioDialog(tmp_path / "audio")
    # Point at a non-existent path; describe_source returns {} or
    # {"error": ...}; the panel must accept either.
    dlg._source_path = tmp_path / "nope.wav"  # noqa: SLF001
    dlg._refresh_meta_panel()  # noqa: SLF001
    text = dlg._meta_label.text()  # noqa: SLF001
    # Either fallback message or details message -- we just don't crash.
    assert text  # non-empty
