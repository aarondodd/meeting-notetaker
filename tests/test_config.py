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
    # v0.6.5: fresh installs default to capture-only (no live
    # transcription pass). The post-Stop batch pass is what populates
    # the Transcript tab. Pin so a future refactor doesn't silently
    # flip live transcription back on.
    assert cfg.transcription.capture_only_mode is True


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


def test_fast_batch_defaults_on(isolated_data_dir):
    """Default beam_size=1 path: fast_batch must be True out of the box.

    Justification lives in PR #12: meeting transcripts feed an LLM
    synthesis pass that absorbs the kinds of errors greedy decoding
    makes. Users wanting verbatim transcripts flip 'High accuracy
    mode' on in Settings."""
    cfg = Config()
    assert cfg.transcription.fast_batch is True


def test_ct2_defaults_and_round_trip(isolated_data_dir):
    cfg = Config()
    assert cfg.transcription.cpu_threads == 0       # 0 = auto
    assert cfg.transcription.num_workers == 2
    cfg.transcription.cpu_threads = 6
    cfg.transcription.num_workers = 4
    cfg.save()
    loaded = Config.load()
    assert loaded.transcription.cpu_threads == 6
    assert loaded.transcription.num_workers == 4


def test_ct2_validation_rejects_extremes(isolated_data_dir):
    cfg = Config()
    cfg.transcription.cpu_threads = -1
    errors = cfg.validate()
    assert any("cpu_threads" in e for e in errors)
    cfg.transcription.cpu_threads = 0
    cfg.transcription.num_workers = 99
    errors = cfg.validate()
    assert any("num_workers" in e for e in errors)


def test_resolved_cpu_threads_auto_splits_cores():
    """Auto formula: cpu_count // num_workers, floor 2."""
    cfg = Config()
    cfg.transcription.cpu_threads = 0
    cfg.transcription.num_workers = 2
    # 12-core target: 12 / 2 = 6
    assert cfg.transcription.resolved_cpu_threads(cpu_count=12) == 6
    # 8-core: 8 / 2 = 4
    assert cfg.transcription.resolved_cpu_threads(cpu_count=8) == 4
    # Tiny CI runner: 2 / 2 = 1, clamped up to 2
    assert cfg.transcription.resolved_cpu_threads(cpu_count=2) == 2


def test_resolved_cpu_threads_explicit_passes_through():
    cfg = Config()
    cfg.transcription.cpu_threads = 4
    cfg.transcription.num_workers = 2
    # Explicit non-zero overrides the auto formula entirely.
    assert cfg.transcription.resolved_cpu_threads(cpu_count=64) == 4


def test_retain_format_defaults_to_opus(isolated_data_dir):
    """Opus is the default retained-recording format -- best size, near-
    transparent for speech. Pin so a refactor doesn't accidentally
    change the default user experience."""
    cfg = Config()
    assert cfg.audio.retain_format == "opus"
    assert cfg.validate() == []


def test_retain_format_round_trip(isolated_data_dir):
    cfg = Config()
    cfg.audio.retain_format = "flac"
    cfg.save()
    loaded = Config.load()
    assert loaded.audio.retain_format == "flac"


def test_retain_format_wav_is_valid(isolated_data_dir):
    """The 'wav' value is the escape hatch (no re-encode); the validator
    must accept it."""
    cfg = Config()
    cfg.audio.retain_format = "wav"
    assert cfg.validate() == []


def test_retain_format_rejects_unknown(isolated_data_dir):
    cfg = Config()
    cfg.audio.retain_format = "mp3"
    errors = cfg.validate()
    assert any("retain_format" in e for e in errors)


def test_synthesis_defaults(isolated_data_dir):
    """Automation feature must default OFF; target must default to claude."""
    cfg = Config()
    assert cfg.synthesis.automation_enabled is False
    assert cfg.synthesis.llm_target == "claude"


def test_synthesis_round_trip(isolated_data_dir):
    cfg = Config()
    cfg.synthesis.automation_enabled = True
    cfg.synthesis.llm_target = "copilot"
    cfg.save()
    loaded = Config.load()
    assert loaded.synthesis.automation_enabled is True
    assert loaded.synthesis.llm_target == "copilot"


def test_synthesis_validation_rejects_unknown_target(isolated_data_dir):
    cfg = Config()
    cfg.synthesis.llm_target = "gemini"
    errors = cfg.validate()
    assert any("synthesis.llm_target" in e for e in errors)


def test_claude_project_id_defaults_empty(isolated_data_dir):
    cfg = Config()
    assert cfg.synthesis.claude_project_id == ""
    # claude_chat_url with empty project id returns /new.
    assert cfg.synthesis.claude_chat_url() == "https://claude.ai/new"


def test_claude_project_id_round_trip(isolated_data_dir):
    cfg = Config()
    cfg.synthesis.claude_project_id = "019e5077-c745-7541-b2c8-08caeb0f3051"
    cfg.save()
    loaded = Config.load()
    assert loaded.synthesis.claude_project_id == "019e5077-c745-7541-b2c8-08caeb0f3051"
    assert (
        loaded.synthesis.claude_chat_url()
        == "https://claude.ai/project/019e5077-c745-7541-b2c8-08caeb0f3051"
    )


def test_claude_project_id_validation_uuid_shape(isolated_data_dir):
    cfg = Config()
    cfg.synthesis.claude_project_id = "not-a-uuid"
    errors = cfg.validate()
    assert any("claude_project_id" in e for e in errors)


def test_claude_project_id_validation_accepts_uuid_v7(isolated_data_dir):
    """The Claude project ID Aaron provided is UUID v7 (time-based) --
    different from v4 internally but same 8-4-4-4-12 outer shape.
    The validator should accept both."""
    cfg = Config()
    cfg.synthesis.claude_project_id = "019e5077-c745-7541-b2c8-08caeb0f3051"
    assert cfg.validate() == []


def test_claude_project_id_accepts_empty(isolated_data_dir):
    """Empty string is the disabled-feature value; must validate."""
    cfg = Config()
    cfg.synthesis.claude_project_id = ""
    assert cfg.validate() == []


def test_claude_chat_url_strips_whitespace(isolated_data_dir):
    """Users pasting from the address bar can accidentally include
    trailing whitespace or newlines. The URL builder must strip them."""
    cfg = Config()
    cfg.synthesis.claude_project_id = " 019e5077-c745-7541-b2c8-08caeb0f3051  \n"
    assert (
        cfg.synthesis.claude_chat_url()
        == "https://claude.ai/project/019e5077-c745-7541-b2c8-08caeb0f3051"
    )


def test_unknown_synthesis_keys_in_toml_drop_cleanly(isolated_data_dir):
    """Forward-compat: a newer config with an unknown key in [synthesis]
    must not crash older releases when loaded."""
    config_path().write_text(
        "[synthesis]\n"
        "automation_enabled = true\n"
        'llm_target = "claude"\n'
        'future_field = "ignore_me"\n',
        encoding="utf-8",
    )
    loaded = Config.load()
    assert loaded.synthesis.automation_enabled is True
    assert loaded.synthesis.llm_target == "claude"
