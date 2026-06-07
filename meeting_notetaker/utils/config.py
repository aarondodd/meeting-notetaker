"""Config persistence (TOML).

Schema (config.toml):

    [audio]
    retain_audio_default = false
    retain_format = "opus"             # opus / flac / wav; format used when retain_audio is True
    vad_enabled = true
    vad_min_silence_ms = 500
    mic_device_name = ""               # empty -> system default; substring match
    loopback_device_name = ""          # empty -> system default; substring match (Windows-only)
    multi_endpoint_capture = true      # capture every WASAPI output endpoint and mix at finalize (Windows-only)

    [transcription]
    model_size = "small.en"
    capture_only_mode = true          # default off for live transcription as of v0.6.5
    skip_batch_refinement = false   # if true, no post-Stop full-recording pass -- live transcript is final
    fast_batch = true               # batch pass uses beam_size=1 (~3x faster). flip off for legal-grade verbatim.
    cpu_threads = 0                 # 0 = auto (cpu_count // num_workers); else fixed value passed to CT2
    num_workers = 2                 # CT2 inference workers per model; >1 lets parallel transcribe() calls run truly in parallel

    [ui]
    user_name = ""               # how the user's mic is labeled (defaults to "Me" when empty)
    first_run_complete = false

    [calendar]
    watch_calendar = false       # poll Outlook for imminent meetings (Windows + Outlook only)
    window_minutes = 5           # notify if a meeting starts within +- N min

    [speakers]
    enabled = true               # run post-meeting speaker identification on the loopback channel
    match_threshold = 0.75       # cosine-similarity floor to auto-label a cluster from the store
    merge_threshold = 0.75       # cosine-similarity floor for two turns to land in one cluster

    [detection]
    enabled = false              # ad-hoc meeting auto-detect (Windows-only; pycaw + psutil required)
    min_duration_sec = 25        # how long audio must sustain before prompting
    cooldown_minutes = 10        # per-app cooldown after a prompt is dismissed
    app_allowlist = ["Teams.exe", "ms-teams.exe", "Zoom.exe", ...]

    [synthesis]
    automation_enabled = false   # if true, swap Generate/Paste buttons for a single Send-to-LLM button (Windows + Chrome extension required)
    llm_target = "claude"        # one of: "claude", "copilot". Copilot is plumbed but not wired in 0.6.3.
    claude_project_id = ""       # optional Claude project UUID; when set, syntheses land in that project instead of the default chat list

Reads use tomllib (3.11+) or the tomli fallback. Writes are emitted by hand
since our schema is flat and tomli-w is not a stdlib component.
"""
from __future__ import annotations

import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover -- 3.10 fallback
    import tomli as tomllib  # type: ignore[import-not-found]

from .paths import config_path


VALID_MODEL_SIZES = ("tiny.en", "base.en", "small.en", "medium.en")
VALID_LLM_TARGETS = ("claude", "copilot")
VALID_RETAIN_FORMATS = ("opus", "flac", "wav")
VALID_SESSION_LIST_SORTS = ("date_desc", "date_asc", "title_asc", "title_desc")
VALID_BACKUP_SCHEDULES = ("manual", "on_close", "when_idle")


@dataclass
class AudioConfig:
    retain_audio_default: bool = False
    # Saved-recording format for sessions where retain_audio is True.
    # opus = ~96% size reduction vs WAV, perceptually transparent for
    # speech. flac = lossless, ~50% reduction. wav = no re-encode, keep
    # the source file as-is (matches v0.6.4 behavior, kept as an
    # escape hatch).
    retain_format: str = "opus"
    vad_enabled: bool = True
    vad_min_silence_ms: int = 500
    mic_device_name: str = ""
    loopback_device_name: str = ""
    # Multi-endpoint loopback capture (#85). When True, the recorder
    # opens one loopback stream per WASAPI output endpoint and mixes
    # at finalize. Defaults ON because Aaron's #84 case is the
    # canonical Windows multi-monitor failure mode (Teams routes to a
    # different endpoint than the one the user expected) and the
    # disk cost is held to near-single-endpoint by per-endpoint RMS
    # gating. Users who hit WASAPI stale-handle quirks at start can
    # toggle this off to fall back to single-endpoint mode.
    multi_endpoint_capture: bool = True


@dataclass
class TranscriptionConfig:
    model_size: str = "small.en"
    # v0.6.5+: default is capture-only (no live transcription pass).
    # The post-meeting batch transcription on Stop still runs and is
    # what populates the Transcript tab. Users who want lines arriving
    # mid-meeting can flip this off in Settings. Existing configs keep
    # the explicit value they had saved.
    capture_only_mode: bool = True
    skip_batch_refinement: bool = False
    fast_batch: bool = True
    cpu_threads: int = 0
    num_workers: int = 2

    def resolved_cpu_threads(self, cpu_count: int | None = None) -> int:
        """0 = auto: split available cores across num_workers, minimum 2.

        Non-zero values pass through. The auto formula keeps total OS
        threads (cpu_threads * num_workers) bounded by the physical
        core count so independent transcribe() calls don't oversubscribe
        the CPU and start thrashing L3 cache.
        """
        if self.cpu_threads > 0:
            return self.cpu_threads
        import os as _os
        cores = cpu_count if cpu_count is not None else (_os.cpu_count() or 4)
        workers = max(1, self.num_workers)
        return max(2, cores // workers)


@dataclass
class UiConfig:
    user_name: str = ""
    first_run_complete: bool = False
    # Flipped to True the first time the user clicks Start Screen
    # Capture and confirms the privacy-notice popup. Suppresses the
    # popup on subsequent captures so the workflow doesn't get
    # interrupted every meeting.
    screen_capture_first_time_seen: bool = False
    # Auto-capture: when armed, snapshot the screen-capture region
    # every N seconds. Captures are deduplicated against the most-
    # recently-kept image via dHash + Hamming distance; only
    # captures whose hash differs by more than dedup_threshold bits
    # are kept. Manual Capture / Insert clicks always keep their
    # image (no dedup check) and reset the baseline.
    screen_capture_auto_enabled_default: bool = False
    screen_capture_auto_interval_sec: int = 30
    screen_capture_auto_dedup_threshold: int = 10
    # Transcript pane's playback split: top pane (screenshot) as a
    # percentage of the total splitter height. Default 70 means the
    # screenshot gets 70% and the transcript editor gets 30%. The
    # user can resize the splitter at runtime; SessionView pushes
    # the new pct back to MainApp which saves it here (debounced).
    transcript_playback_split_top_pct: int = 70
    # Session-list sort spec. One of: date_desc (default, newest first),
    # date_asc (oldest first), title_asc (A-Z), title_desc (Z-A). MainWindow
    # parses + applies; clicking the Date/Title header updates the value.
    session_list_sort: str = "date_desc"
    # Persisted window layout: QMainWindow.saveGeometry() output
    # (size + position + maximized state) and the main horizontal
    # splitter's saveState() output (left/right pane ratio), each
    # base64-encoded for TOML safety. Saved on app aboutToQuit;
    # restored at startup. Empty strings mean "use Qt defaults"
    # (first launch or after a stale-state recovery).
    main_window_geometry: str = ""
    main_splitter_state: str = ""
    # Last-active section in the Settings dialog (v0.7.5 nav redesign).
    # Empty string = first launch, alphabetical default ("Audio").
    # Persisted on accept so reopening Settings lands on the same page.
    settings_active_section: str = ""
    # Font preferences for the editor + preview surfaces (#80
    # followup, v0.7.7). Editor face must be monospace -- Markdown
    # editing benefits from column alignment for tables, code
    # blocks, and bulleted lists. Preview face is whatever the user
    # wants. Empty family strings mean "auto-pick a platform-
    # appropriate default" -- Consolas / Cascadia Mono on Windows
    # for the editor; system sans for the preview. Sizes 0 mean
    # "use the platform default" so existing installs that don't
    # carry the field don't suddenly downsize all their text.
    editor_font_family: str = ""
    editor_font_size: int = 0
    preview_font_family: str = ""
    preview_font_size: int = 0
    # Pop-out notes preview window state (#80 followup, v0.7.7).
    # Geometry mirrors the main_window_geometry round-trip pattern
    # so the popout reopens where the user left it. Empty string =
    # never opened or geometry was lost (e.g. external monitor
    # removed). Always-on-top toggle is sticky so reopening
    # preserves the screenshare-friendly setup.
    notes_popout_geometry: str = ""
    notes_popout_always_on_top: bool = False
    # Styled markdown source highlighting in the My Notes editor (#91).
    # When True, the editor decorates the source with heading sizes,
    # bold/italic/code styling, dimmed markers, etc. When False the
    # editor reverts to plain monospace. Defaults True because the
    # styling is conservative + based on palette colors so it adapts
    # to light + dark themes without forcing one look.
    markdown_rich_editor: bool = True


@dataclass
class CalendarConfig:
    watch_calendar: bool = False
    window_minutes: int = 5


@dataclass
class SpeakersConfig:
    enabled: bool = True
    match_threshold: float = 0.75
    merge_threshold: float = 0.75


@dataclass
class DetectionConfig:
    enabled: bool = False
    min_duration_sec: int = 25
    cooldown_minutes: int = 10
    app_allowlist: list[str] = field(
        default_factory=lambda: list(_DEFAULT_DETECTION_ALLOWLIST)
    )


_DEFAULT_DETECTION_ALLOWLIST: tuple[str, ...] = (
    "Teams.exe",
    "ms-teams.exe",
    "Zoom.exe",
    "ZoomPhone.exe",
    "slack.exe",
    "WebexMta.exe",
    "atmgr.exe",
    "GoToMeetingWinStore.exe",
    "Discord.exe",
)


@dataclass
class SynthesisConfig:
    automation_enabled: bool = False
    llm_target: str = "claude"
    # Optional Claude.ai project UUID. When set, the Send-to-Claude
    # flow opens https://claude.ai/project/<id> instead of /new, so
    # synthesized notes accumulate inside the named project rather
    # than flooding the user's default chat list. Empty == no project.
    claude_project_id: str = ""
    # v0.7.2 (issue #51 Phase 4): append an attendee-details
    # extraction request to every synthesis prompt. Off by default
    # (2026-05-29) -- the request adds noticeable bulk to the
    # prompt and the value is small for the common case where
    # Outlook calendar enrichment already populated the attendees'
    # rich fields. User opts in via Settings > Synthesis Prompts
    # when they want LLM backfill from in-meeting mentions.
    auto_extract_attendee_details: bool = False
    # When True, the "## Attendee Details (auto-extracted)" appendix
    # is removed from the saved notes.md after parsing. Default OFF
    # so the user can see what the LLM extracted (transparency); flip
    # ON if the appendix is cluttering shared synthesis exports.
    strip_attendee_appendix: bool = False
    # Default prompt-template filename used by sessions that don't
    # have a per-session override. Empty string falls back to
    # "default.md" (the bundled generic template). Settings >
    # Synthesis Prompts exposes a dropdown listing the templates
    # actually present in the user's prompts folder.
    default_template_name: str = ""
    # Default destination folder for the Export Recording / Video /
    # Full Session / PDF dialogs (v0.7.5). When set + the folder
    # still exists, every Save As / Choose Folder dialog opens here
    # instead of falling back to the per-call default (session dir
    # for audio + video, Documents for full-session, session dir for
    # PDF). Empty == use the per-call default (legacy behavior).
    export_default_folder: str = ""
    # MP4 quality preset for the export paths -- low / medium / high
    # (issue #54). "medium" defaults to ~1.5 Mbps video + 96 kbps
    # audio, which is appropriate for slideshow-style screenshot
    # content while keeping file sizes manageable. "high" preserves
    # the pre-#54 behavior (2.5 Mbps + 128 kbps) for users who want
    # the larger files. Settings dialog exposes a dropdown.
    video_quality: str = "medium"
    # Full-session export packaging (issue #62). When False, the
    # export writes contents to a subfolder under the user-chosen
    # parent directory (no zip step). When True, the original
    # behavior -- single .zip file. Off by default since OneDrive
    # / shared-drive use cases dominate over emailed zips.
    compress_full_session_export: bool = False
    # Default checkbox state for the AppendixInclusionDialog
    # (#65/#66 followup). The user-facing rationale Aaron picked:
    # surface the curated context surfaces (attendee context,
    # session + LLM-mentioned documents, links) by default but
    # keep the noisier per-person field dumps + topic suggestions
    # off so a fresh PDF / ZIP doesn't bury the synthesis under
    # appendix bulk.
    appendix_export_include: bool = True
    appendix_export_attendee_context: bool = True
    appendix_export_attendee_details: bool = False
    appendix_export_topics: bool = False
    appendix_export_referenced_attachments: bool = True
    appendix_export_session_attachments: bool = True
    appendix_export_links: bool = True

    def claude_chat_url(self) -> str:
        """Build the Claude.ai URL the extension should land on for
        a fresh synthesis. If a project id is configured, return the
        project URL; otherwise the canonical /new path. Stripping
        whitespace so a user pasting with trailing newlines doesn't
        produce a busted URL."""
        pid = self.claude_project_id.strip()
        if pid:
            return f"https://claude.ai/project/{pid}"
        return "https://claude.ai/new"


@dataclass
class BackupConfig:
    """Local-machine SPOF mitigation (#67).

    The user picks a destination folder (often a OneDrive / external
    drive mirror) and a schedule, the app writes timestamped zips of
    the data dir + the three sqlite stores, and a retention policy
    prunes old zips silently.

    ``schedule`` controls when an automatic snapshot fires:
      - ``manual`` -- the Tools menu's Backup Now button is the only
        trigger. No daily-on-start: Aaron flagged that as a UX hit
        when he opens the app to start a meeting quickly.
      - ``on_close`` -- snapshot on app close. Good when the user
        regularly closes the app at end of day. If a backup is mid-
        flight when the user clicks close, the app shows a modal
        "backup in progress" dialog and waits for the snapshot.
      - ``when_idle`` -- the most common pattern for an always-open
        app: snapshot when the app has been idle for
        ``idle_after_minutes`` AND the local clock is past
        ``idle_after_hour:00``. A 24h dedup cap means the same idle
        window can't trigger twice in one day.

    ``retention_count`` and ``retention_days`` both apply -- the
    intersection wins. Set either to 0 to disable that gate. Default
    is 7 snapshots OR 30 days, whichever is stricter.
    """
    folder: str = ""
    schedule: str = "manual"
    idle_after_minutes: int = 30
    idle_after_hour: int = 19
    retention_count: int = 7
    retention_days: int = 30
    # Last-successful-snapshot timestamp (ISO 8601 local). Empty until
    # the first backup completes. The 24h dedup gate compares against
    # this; restoring or upgrading does not bump it.
    last_snapshot_at: str = ""


@dataclass
class NotionConfig:
    """Experimental Notion export (issue #79).

    The user creates an internal integration at notion.so/my-integrations,
    pastes the secret_XXX token here, and shares specific pages with that
    integration. The picker dialog lists those shared pages.

    ``favorites`` and ``recents`` carry pre-rendered titles so the
    picker can render its rows without round-tripping to the API on
    every dialog open. Both are stored as TOML arrays of inline tables.
    """
    api_token: str = ""
    # Set by the Settings "Verify connection" path on a successful
    # /v1/users/me call. Empty until verified.
    last_verified_at: str = ""
    # Each entry: {"id": "<page_id>", "title": "<display path>"}.
    favorites: list[dict] = field(default_factory=list)
    # Each entry: {"id": "<page_id>", "title": "...", "used_at": "<iso>"}.
    recents: list[dict] = field(default_factory=list)


@dataclass
class ConfluenceConfig:
    """Experimental Confluence export (issue #79).

    Cloud or server -- the user provides their base URL (Cloud:
    ``https://your-org.atlassian.net/wiki``; server: same shape).
    Email + API token authenticate every call.
    """
    base_url: str = ""
    email: str = ""
    api_token: str = ""
    last_verified_at: str = ""
    favorites: list[dict] = field(default_factory=list)
    recents: list[dict] = field(default_factory=list)


@dataclass
class Config:
    audio: AudioConfig = field(default_factory=AudioConfig)
    transcription: TranscriptionConfig = field(default_factory=TranscriptionConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    calendar: CalendarConfig = field(default_factory=CalendarConfig)
    speakers: SpeakersConfig = field(default_factory=SpeakersConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    synthesis: SynthesisConfig = field(default_factory=SynthesisConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)
    notion: NotionConfig = field(default_factory=NotionConfig)
    confluence: ConfluenceConfig = field(default_factory=ConfluenceConfig)

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or config_path()
        if not path.exists():
            return cls()
        with path.open("rb") as f:
            data = tomllib.load(f)
        # Drop unknown keys from older configs (e.g. the removed `ui.theme`
        # field) so a stale config.toml from a prior version still loads.
        return cls(
            audio=AudioConfig(**_filter_fields(AudioConfig, data.get("audio", {}))),
            transcription=TranscriptionConfig(
                **_filter_fields(TranscriptionConfig, data.get("transcription", {}))
            ),
            ui=UiConfig(**_filter_fields(UiConfig, data.get("ui", {}))),
            calendar=CalendarConfig(
                **_filter_fields(CalendarConfig, data.get("calendar", {}))
            ),
            speakers=SpeakersConfig(
                **_filter_fields(SpeakersConfig, data.get("speakers", {}))
            ),
            detection=DetectionConfig(
                **_filter_fields(DetectionConfig, data.get("detection", {}))
            ),
            synthesis=SynthesisConfig(
                **_filter_fields(SynthesisConfig, data.get("synthesis", {}))
            ),
            backup=BackupConfig(
                **_filter_fields(BackupConfig, data.get("backup", {}))
            ),
            notion=NotionConfig(
                **_filter_fields(NotionConfig, data.get("notion", {}))
            ),
            confluence=ConfluenceConfig(
                **_filter_fields(ConfluenceConfig, data.get("confluence", {}))
            ),
        )

    def save(self, path: Path | None = None) -> None:
        path = path or config_path()
        path.write_text(self._dump_toml(), encoding="utf-8")

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.transcription.model_size not in VALID_MODEL_SIZES:
            errors.append(
                f"transcription.model_size {self.transcription.model_size!r} "
                f"must be one of {VALID_MODEL_SIZES}"
            )
        if not (50 <= self.audio.vad_min_silence_ms <= 5000):
            errors.append(
                f"audio.vad_min_silence_ms must be between 50 and 5000, "
                f"got {self.audio.vad_min_silence_ms}"
            )
        if self.audio.retain_format not in VALID_RETAIN_FORMATS:
            errors.append(
                f"audio.retain_format must be one of {VALID_RETAIN_FORMATS}, "
                f"got {self.audio.retain_format!r}"
            )
        if not (0 <= self.transcription.cpu_threads <= 128):
            errors.append(
                f"transcription.cpu_threads must be between 0 and 128 (0 = auto), "
                f"got {self.transcription.cpu_threads}"
            )
        if not (1 <= self.transcription.num_workers <= 8):
            errors.append(
                f"transcription.num_workers must be between 1 and 8, "
                f"got {self.transcription.num_workers}"
            )
        if not (1 <= self.calendar.window_minutes <= 60):
            errors.append(
                f"calendar.window_minutes must be between 1 and 60, "
                f"got {self.calendar.window_minutes}"
            )
        if not (0.0 < self.speakers.match_threshold <= 1.0):
            errors.append(
                f"speakers.match_threshold must be in (0, 1], "
                f"got {self.speakers.match_threshold}"
            )
        if not (0.0 < self.speakers.merge_threshold <= 1.0):
            errors.append(
                f"speakers.merge_threshold must be in (0, 1], "
                f"got {self.speakers.merge_threshold}"
            )
        if not (5 <= self.detection.min_duration_sec <= 300):
            errors.append(
                f"detection.min_duration_sec must be between 5 and 300, "
                f"got {self.detection.min_duration_sec}"
            )
        if not (1 <= self.detection.cooldown_minutes <= 120):
            errors.append(
                f"detection.cooldown_minutes must be between 1 and 120, "
                f"got {self.detection.cooldown_minutes}"
            )
        if not (5 <= self.ui.screen_capture_auto_interval_sec <= 300):
            errors.append(
                "ui.screen_capture_auto_interval_sec must be between 5 "
                f"and 300, got {self.ui.screen_capture_auto_interval_sec}"
            )
        if not (0 <= self.ui.screen_capture_auto_dedup_threshold <= 64):
            errors.append(
                "ui.screen_capture_auto_dedup_threshold must be between 0 "
                f"and 64, got {self.ui.screen_capture_auto_dedup_threshold}"
            )
        if not (10 <= self.ui.transcript_playback_split_top_pct <= 90):
            errors.append(
                "ui.transcript_playback_split_top_pct must be between 10 "
                f"and 90, got {self.ui.transcript_playback_split_top_pct}"
            )
        if self.ui.session_list_sort not in VALID_SESSION_LIST_SORTS:
            errors.append(
                f"ui.session_list_sort {self.ui.session_list_sort!r} "
                f"must be one of {VALID_SESSION_LIST_SORTS}"
            )
        if self.synthesis.llm_target not in VALID_LLM_TARGETS:
            errors.append(
                f"synthesis.llm_target {self.synthesis.llm_target!r} "
                f"must be one of {VALID_LLM_TARGETS}"
            )
        if self.backup.schedule not in VALID_BACKUP_SCHEDULES:
            errors.append(
                f"backup.schedule {self.backup.schedule!r} must be one "
                f"of {VALID_BACKUP_SCHEDULES}"
            )
        if not (1 <= self.backup.idle_after_minutes <= 720):
            errors.append(
                "backup.idle_after_minutes must be between 1 and 720, "
                f"got {self.backup.idle_after_minutes}"
            )
        if not (0 <= self.backup.idle_after_hour <= 23):
            errors.append(
                "backup.idle_after_hour must be between 0 and 23, "
                f"got {self.backup.idle_after_hour}"
            )
        if not (0 <= self.backup.retention_count <= 365):
            errors.append(
                "backup.retention_count must be between 0 and 365 "
                f"(0 disables), got {self.backup.retention_count}"
            )
        if not (0 <= self.backup.retention_days <= 3650):
            errors.append(
                "backup.retention_days must be between 0 and 3650 "
                f"(0 disables), got {self.backup.retention_days}"
            )
        # Issue #79: light validation on the experimental integration
        # fields. The token + email shapes vary across tenants so
        # we don't try to enforce them; just guard the URL shape so a
        # typo doesn't strand the verify path with a confusing error.
        if self.confluence.base_url:
            url = self.confluence.base_url.strip()
            if not (url.startswith("https://") or url.startswith("http://")):
                errors.append(
                    "confluence.base_url must start with https:// or http:// "
                    f"(got {self.confluence.base_url!r})"
                )
        if self.synthesis.claude_project_id:
            # Optional field; if set, must look like a UUID
            # (8-4-4-4-12 hex). Loose match -- Claude uses UUID-v7
            # variants whose internal structure differs from v4 but
            # the outer shape is the same.
            import re  # noqa: PLC0415

            if not re.fullmatch(
                r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                self.synthesis.claude_project_id.strip(),
            ):
                errors.append(
                    "synthesis.claude_project_id must be a UUID-shaped "
                    "string (e.g. 019e5077-c745-7541-b2c8-08caeb0f3051) "
                    "or empty to disable. Got: "
                    f"{self.synthesis.claude_project_id!r}"
                )
        return errors

    def _dump_toml(self) -> str:
        lines: list[str] = []
        for section, obj in (
            ("audio", self.audio),
            ("transcription", self.transcription),
            ("ui", self.ui),
            ("calendar", self.calendar),
            ("speakers", self.speakers),
            ("detection", self.detection),
            ("synthesis", self.synthesis),
            ("backup", self.backup),
            ("notion", self.notion),
            ("confluence", self.confluence),
        ):
            lines.append(f"[{section}]")
            for key, value in asdict(obj).items():
                lines.append(f"{key} = {_toml_repr(value)}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def _filter_fields(dataclass_type: type, data: dict[str, Any]) -> dict[str, Any]:
    """Return only the keys from `data` that match fields on `dataclass_type`."""
    valid = {f.name for f in dataclass_type.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    return {k: v for k, v in data.items() if k in valid}


def _toml_repr(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, list):
        return "[" + ", ".join(_toml_repr(v) for v in value) + "]"
    if isinstance(value, dict):
        # TOML inline table: { k = v, ... }. Used for favorites /
        # recents entries (issue #79) where each list item is a small
        # {id, title, ...} record. tomllib loads inline tables back as
        # dicts so the round-trip is clean.
        parts = [f"{k} = {_toml_repr(v)}" for k, v in value.items()]
        return "{ " + ", ".join(parts) + " }" if parts else "{}"
    raise TypeError(f"unsupported TOML value: {value!r}")
