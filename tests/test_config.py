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


def test_auto_capture_defaults_and_round_trip(isolated_data_dir):
    """Auto-capture defaults: off, 30 s interval, 10-bit dedup threshold."""
    cfg = Config()
    assert cfg.ui.screen_capture_auto_enabled_default is False
    assert cfg.ui.screen_capture_auto_interval_sec == 30
    assert cfg.ui.screen_capture_auto_dedup_threshold == 10
    cfg.ui.screen_capture_auto_enabled_default = True
    cfg.ui.screen_capture_auto_interval_sec = 60
    cfg.ui.screen_capture_auto_dedup_threshold = 15
    cfg.save()
    loaded = Config.load()
    assert loaded.ui.screen_capture_auto_enabled_default is True
    assert loaded.ui.screen_capture_auto_interval_sec == 60
    assert loaded.ui.screen_capture_auto_dedup_threshold == 15


def test_auto_capture_interval_validation(isolated_data_dir):
    cfg = Config()
    cfg.ui.screen_capture_auto_interval_sec = 2  # below floor
    errors = cfg.validate()
    assert any("screen_capture_auto_interval_sec" in e for e in errors)
    cfg.ui.screen_capture_auto_interval_sec = 999  # above ceiling
    errors = cfg.validate()
    assert any("screen_capture_auto_interval_sec" in e for e in errors)


def test_auto_capture_dedup_threshold_validation(isolated_data_dir):
    cfg = Config()
    cfg.ui.screen_capture_auto_dedup_threshold = -1
    errors = cfg.validate()
    assert any("screen_capture_auto_dedup_threshold" in e for e in errors)
    cfg.ui.screen_capture_auto_dedup_threshold = 100  # > 64
    errors = cfg.validate()
    assert any("screen_capture_auto_dedup_threshold" in e for e in errors)


def test_transcript_playback_split_defaults_seventy(isolated_data_dir):
    """Default split puts 70% on the top (screenshot) pane."""
    cfg = Config()
    assert cfg.ui.transcript_playback_split_top_pct == 70


def test_transcript_playback_split_round_trip(isolated_data_dir):
    cfg = Config()
    cfg.ui.transcript_playback_split_top_pct = 55
    cfg.save()
    loaded = Config.load()
    assert loaded.ui.transcript_playback_split_top_pct == 55


def test_transcript_playback_split_validation(isolated_data_dir):
    cfg = Config()
    cfg.ui.transcript_playback_split_top_pct = 5
    errors = cfg.validate()
    assert any("transcript_playback_split_top_pct" in e for e in errors)
    cfg.ui.transcript_playback_split_top_pct = 95
    errors = cfg.validate()
    assert any("transcript_playback_split_top_pct" in e for e in errors)
    cfg.ui.transcript_playback_split_top_pct = 50
    assert not any("transcript_playback_split_top_pct" in e for e in cfg.validate())


def test_screen_capture_first_time_seen_defaults_false(isolated_data_dir):
    """Fresh installs have NOT seen the screen-capture notice. The first
    Start Screen Capture click shows the popup, then flips this to True
    so future clicks don't interrupt."""
    cfg = Config()
    assert cfg.ui.screen_capture_first_time_seen is False
    cfg.ui.screen_capture_first_time_seen = True
    cfg.save()
    loaded = Config.load()
    assert loaded.ui.screen_capture_first_time_seen is True


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


def test_synthesis_default_template_name_defaults_to_empty():
    """The global default-template setting starts empty so the runtime
    falls back to the bundled default.md. Users set it via Settings >
    Synthesis Prompts > Default template."""
    from meeting_notetaker.utils.config import SynthesisConfig
    assert SynthesisConfig().default_template_name == ""


def test_synthesis_auto_extract_attendee_details_defaults_off():
    """Attendee-details extraction is opt-in starting 2026-05-29.
    The default was flipped from True to False after the appended
    system-prompt was causing Claude to flag the synthesis as
    embedded prompt injection."""
    from meeting_notetaker.utils.config import SynthesisConfig
    assert SynthesisConfig().auto_extract_attendee_details is False


def test_appendix_export_defaults_match_aarons_chosen_set():
    """Aaron's explicit defaults (2026-05-30): Appendix master + the
    user-curated context surfaces (attendee context + session and
    LLM-mentioned documents + links) default ON; the noisier
    Attendee Details + Suggested Topics default OFF."""
    from meeting_notetaker.utils.config import SynthesisConfig
    s = SynthesisConfig()
    assert s.appendix_export_include is True
    assert s.appendix_export_attendee_context is True
    assert s.appendix_export_attendee_details is False
    assert s.appendix_export_topics is False
    assert s.appendix_export_referenced_attachments is True
    assert s.appendix_export_session_attachments is True
    assert s.appendix_export_links is True


def test_appendix_export_defaults_round_trip(isolated_data_dir):
    """User toggles in Settings persist across restarts."""
    cfg = Config()
    cfg.synthesis.appendix_export_attendee_details = True
    cfg.synthesis.appendix_export_topics = True
    cfg.synthesis.appendix_export_links = False
    cfg.save()
    loaded = Config.load()
    assert loaded.synthesis.appendix_export_attendee_details is True
    assert loaded.synthesis.appendix_export_topics is True
    assert loaded.synthesis.appendix_export_links is False
    # Defaults for fields the test didn't touch survive.
    assert loaded.synthesis.appendix_export_include is True
    assert loaded.synthesis.appendix_export_attendee_context is True


# ---- BackupConfig (#67) ---------------------------------------------------


def test_backup_defaults_are_safe(isolated_data_dir):
    """Fresh install lands with manual schedule + empty folder so the
    backup feature is opt-in. The first time the user opens Settings >
    Backups they consciously pick a destination + schedule before any
    snapshot fires."""
    cfg = Config()
    assert cfg.backup.folder == ""
    assert cfg.backup.schedule == "manual"
    assert cfg.backup.idle_after_minutes == 30
    assert cfg.backup.idle_after_hour == 19
    assert cfg.backup.retention_count == 7
    assert cfg.backup.retention_days == 30
    assert cfg.backup.last_snapshot_at == ""
    assert cfg.validate() == []


def test_backup_settings_round_trip(isolated_data_dir):
    cfg = Config()
    cfg.backup.folder = "/mnt/backups"
    cfg.backup.schedule = "when_idle"
    cfg.backup.idle_after_minutes = 45
    cfg.backup.idle_after_hour = 21
    cfg.backup.retention_count = 14
    cfg.backup.retention_days = 60
    cfg.backup.last_snapshot_at = "2026-06-01T22:00:00"
    cfg.save()
    loaded = Config.load()
    assert loaded.backup.folder == "/mnt/backups"
    assert loaded.backup.schedule == "when_idle"
    assert loaded.backup.idle_after_minutes == 45
    assert loaded.backup.idle_after_hour == 21
    assert loaded.backup.retention_count == 14
    assert loaded.backup.retention_days == 60
    assert loaded.backup.last_snapshot_at == "2026-06-01T22:00:00"


def test_backup_validate_rejects_unknown_schedule(isolated_data_dir):
    cfg = Config()
    cfg.backup.schedule = "weekly"  # not in VALID_BACKUP_SCHEDULES
    errors = cfg.validate()
    assert any("backup.schedule" in e for e in errors)


def test_backup_validate_rejects_out_of_range_idle(isolated_data_dir):
    cfg = Config()
    cfg.backup.idle_after_minutes = 0
    cfg.backup.idle_after_hour = 24
    errors = cfg.validate()
    assert any("idle_after_minutes" in e for e in errors)
    assert any("idle_after_hour" in e for e in errors)


def test_backup_validate_rejects_negative_retention(isolated_data_dir):
    cfg = Config()
    cfg.backup.retention_count = -1
    cfg.backup.retention_days = -1
    errors = cfg.validate()
    assert any("retention_count" in e for e in errors)
    assert any("retention_days" in e for e in errors)
