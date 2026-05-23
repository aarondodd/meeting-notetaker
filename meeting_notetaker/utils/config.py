"""Config persistence (TOML).

Schema (config.toml):

    [audio]
    retain_audio_default = false
    retain_format = "opus"             # opus / flac / wav; format used when retain_audio is True
    vad_enabled = true
    vad_min_silence_ms = 500
    mic_device_name = ""               # empty -> system default; substring match
    loopback_device_name = ""          # empty -> system default; substring match (Windows-only)

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
class Config:
    audio: AudioConfig = field(default_factory=AudioConfig)
    transcription: TranscriptionConfig = field(default_factory=TranscriptionConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    calendar: CalendarConfig = field(default_factory=CalendarConfig)
    speakers: SpeakersConfig = field(default_factory=SpeakersConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    synthesis: SynthesisConfig = field(default_factory=SynthesisConfig)

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
        if self.synthesis.llm_target not in VALID_LLM_TARGETS:
            errors.append(
                f"synthesis.llm_target {self.synthesis.llm_target!r} "
                f"must be one of {VALID_LLM_TARGETS}"
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
    raise TypeError(f"unsupported TOML value: {value!r}")
