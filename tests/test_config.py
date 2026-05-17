"""Config round-trip + validation."""
from __future__ import annotations

from meeting_notetaker.utils.config import (
    Config,
    VALID_MODEL_SIZES,
    VALID_THEMES,
)
from meeting_notetaker.utils.paths import config_path


def test_defaults_are_valid(isolated_data_dir):
    cfg = Config()
    assert cfg.validate() == []
    assert cfg.transcription.model_size in VALID_MODEL_SIZES
    assert cfg.ui.theme in VALID_THEMES
    assert 50 <= cfg.audio.vad_min_silence_ms <= 5000


def test_save_load_round_trip(isolated_data_dir):
    cfg = Config()
    cfg.audio.retain_audio_default = True
    cfg.audio.vad_min_silence_ms = 750
    cfg.transcription.model_size = "medium.en"
    cfg.transcription.capture_only_mode = True
    cfg.ui.theme = "dark"
    cfg.ui.first_run_complete = True
    cfg.save()

    loaded = Config.load()
    assert loaded.audio.retain_audio_default is True
    assert loaded.audio.vad_min_silence_ms == 750
    assert loaded.transcription.model_size == "medium.en"
    assert loaded.transcription.capture_only_mode is True
    assert loaded.ui.theme == "dark"
    assert loaded.ui.first_run_complete is True


def test_load_with_missing_file_returns_defaults(isolated_data_dir):
    cfg = Config.load()
    assert cfg.transcription.model_size == "small.en"
    assert cfg.audio.retain_audio_default is False


def test_validate_rejects_bad_values(isolated_data_dir):
    cfg = Config()
    cfg.transcription.model_size = "huge.fr"
    cfg.ui.theme = "neon"
    cfg.audio.vad_min_silence_ms = 99999
    errors = cfg.validate()
    assert len(errors) == 3
    assert any("model_size" in e for e in errors)
    assert any("theme" in e for e in errors)
    assert any("vad_min_silence_ms" in e for e in errors)


def test_device_name_round_trip(isolated_data_dir):
    cfg = Config()
    cfg.audio.mic_device_name = "Headset Microphone (Plantronics Voyager)"
    cfg.audio.loopback_device_name = "Speakers (Realtek(R) Audio) [Loopback]"
    cfg.save()

    loaded = Config.load()
    assert loaded.audio.mic_device_name == "Headset Microphone (Plantronics Voyager)"
    assert loaded.audio.loopback_device_name == "Speakers (Realtek(R) Audio) [Loopback]"


def test_device_name_defaults_to_empty(isolated_data_dir):
    cfg = Config()
    assert cfg.audio.mic_device_name == ""
    assert cfg.audio.loopback_device_name == ""
