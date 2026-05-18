"""Config round-trip + validation."""
from __future__ import annotations

from meeting_notetaker.utils.config import (
    Config,
    VALID_MODEL_SIZES,
)
from meeting_notetaker.utils.paths import config_path


def test_defaults_are_valid(isolated_data_dir):
    cfg = Config()
    assert cfg.validate() == []
    assert cfg.transcription.model_size in VALID_MODEL_SIZES
    assert 50 <= cfg.audio.vad_min_silence_ms <= 5000


def test_save_load_round_trip(isolated_data_dir):
    cfg = Config()
    cfg.audio.retain_audio_default = True
    cfg.audio.vad_min_silence_ms = 750
    cfg.transcription.model_size = "medium.en"
    cfg.transcription.capture_only_mode = True
    cfg.ui.first_run_complete = True
    cfg.save()

    loaded = Config.load()
    assert loaded.audio.retain_audio_default is True
    assert loaded.audio.vad_min_silence_ms == 750
    assert loaded.transcription.model_size == "medium.en"
    assert loaded.transcription.capture_only_mode is True
    assert loaded.ui.first_run_complete is True


def test_load_with_missing_file_returns_defaults(isolated_data_dir):
    cfg = Config.load()
    assert cfg.transcription.model_size == "small.en"
    assert cfg.audio.retain_audio_default is False


def test_validate_rejects_bad_values(isolated_data_dir):
    cfg = Config()
    cfg.transcription.model_size = "huge.fr"
    cfg.audio.vad_min_silence_ms = 99999
    errors = cfg.validate()
    assert len(errors) == 2
    assert any("model_size" in e for e in errors)
    assert any("vad_min_silence_ms" in e for e in errors)


def test_load_ignores_legacy_theme_key(isolated_data_dir, tmp_path):
    """An older config.toml with `ui.theme = "dark"` should still load."""
    p = tmp_path / "legacy.toml"
    p.write_text(
        "[audio]\n"
        "[transcription]\n"
        '[ui]\ntheme = "dark"\nuser_name = "Aaron"\n'
        "[calendar]\n",
        encoding="utf-8",
    )
    loaded = Config.load(p)
    assert loaded.ui.user_name == "Aaron"
    assert not hasattr(loaded.ui, "theme")


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


def test_calendar_defaults_and_round_trip(isolated_data_dir):
    cfg = Config()
    assert cfg.calendar.watch_calendar is False
    assert cfg.calendar.window_minutes == 5
    cfg.calendar.watch_calendar = True
    cfg.calendar.window_minutes = 10
    cfg.save()
    loaded = Config.load()
    assert loaded.calendar.watch_calendar is True
    assert loaded.calendar.window_minutes == 10


def test_calendar_window_validation(isolated_data_dir):
    cfg = Config()
    cfg.calendar.window_minutes = 90
    errors = cfg.validate()
    assert any("calendar.window_minutes" in e for e in errors)
    cfg.calendar.window_minutes = 0
    errors = cfg.validate()
    assert any("calendar.window_minutes" in e for e in errors)


def test_speakers_defaults_and_round_trip(isolated_data_dir):
    cfg = Config()
    assert cfg.speakers.enabled is True
    assert cfg.speakers.match_threshold == 0.75
    assert cfg.speakers.merge_threshold == 0.75
    cfg.speakers.enabled = False
    cfg.speakers.match_threshold = 0.80
    cfg.speakers.merge_threshold = 0.65
    cfg.save()
    loaded = Config.load()
    assert loaded.speakers.enabled is False
    assert loaded.speakers.match_threshold == 0.80
    assert loaded.speakers.merge_threshold == 0.65


def test_speakers_threshold_validation(isolated_data_dir):
    cfg = Config()
    cfg.speakers.match_threshold = 1.5
    errors = cfg.validate()
    assert any("speakers.match_threshold" in e for e in errors)
    cfg.speakers.match_threshold = 0.75
    cfg.speakers.merge_threshold = 0.0
    errors = cfg.validate()
    assert any("speakers.merge_threshold" in e for e in errors)


def test_detection_defaults_and_round_trip(isolated_data_dir):
    cfg = Config()
    assert cfg.detection.enabled is False
    assert cfg.detection.min_duration_sec == 25
    assert cfg.detection.cooldown_minutes == 10
    assert "Teams.exe" in cfg.detection.app_allowlist
    cfg.detection.enabled = True
    cfg.detection.min_duration_sec = 45
    cfg.detection.cooldown_minutes = 30
    cfg.detection.app_allowlist = ["Custom.exe", "Other.exe"]
    cfg.save()
    loaded = Config.load()
    assert loaded.detection.enabled is True
    assert loaded.detection.min_duration_sec == 45
    assert loaded.detection.cooldown_minutes == 30
    assert loaded.detection.app_allowlist == ["Custom.exe", "Other.exe"]


def test_detection_validation_rejects_extremes(isolated_data_dir):
    cfg = Config()
    cfg.detection.min_duration_sec = 1
    errors = cfg.validate()
    assert any("detection.min_duration_sec" in e for e in errors)
    cfg.detection.min_duration_sec = 25
    cfg.detection.cooldown_minutes = 999
    errors = cfg.validate()
    assert any("detection.cooldown_minutes" in e for e in errors)
