"""Config persistence (TOML).

Schema (config.toml):

    [audio]
    retain_audio_default = false
    vad_enabled = true
    vad_min_silence_ms = 500
    mic_device_name = ""               # empty -> system default; substring match
    loopback_device_name = ""          # empty -> system default; substring match (Windows-only)

    [transcription]
    model_size = "small.en"
    capture_only_mode = false
    skip_batch_refinement = false   # if true, no post-Stop full-recording pass -- live transcript is final
    fast_batch = false              # if true, batch pass uses beam_size=1 (~3x faster, slight quality drop)

    [ui]
    theme = "auto"               # "auto" | "light" | "dark"
    user_name = ""               # how the user's mic is labeled (defaults to "Me" when empty)
    first_run_complete = false

    [calendar]
    watch_calendar = false       # poll Outlook for imminent meetings (Windows + Outlook only)
    window_minutes = 5           # notify if a meeting starts within +- N min

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
VALID_THEMES = ("auto", "light", "dark")


@dataclass
class AudioConfig:
    retain_audio_default: bool = False
    vad_enabled: bool = True
    vad_min_silence_ms: int = 500
    mic_device_name: str = ""
    loopback_device_name: str = ""


@dataclass
class TranscriptionConfig:
    model_size: str = "small.en"
    capture_only_mode: bool = False
    skip_batch_refinement: bool = False
    fast_batch: bool = False


@dataclass
class UiConfig:
    theme: str = "auto"
    user_name: str = ""
    first_run_complete: bool = False


@dataclass
class CalendarConfig:
    watch_calendar: bool = False
    window_minutes: int = 5


@dataclass
class Config:
    audio: AudioConfig = field(default_factory=AudioConfig)
    transcription: TranscriptionConfig = field(default_factory=TranscriptionConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    calendar: CalendarConfig = field(default_factory=CalendarConfig)

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or config_path()
        if not path.exists():
            return cls()
        with path.open("rb") as f:
            data = tomllib.load(f)
        return cls(
            audio=AudioConfig(**data.get("audio", {})),
            transcription=TranscriptionConfig(**data.get("transcription", {})),
            ui=UiConfig(**data.get("ui", {})),
            calendar=CalendarConfig(**data.get("calendar", {})),
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
        if self.ui.theme not in VALID_THEMES:
            errors.append(f"ui.theme {self.ui.theme!r} must be one of {VALID_THEMES}")
        if not (50 <= self.audio.vad_min_silence_ms <= 5000):
            errors.append(
                f"audio.vad_min_silence_ms must be between 50 and 5000, "
                f"got {self.audio.vad_min_silence_ms}"
            )
        if not (1 <= self.calendar.window_minutes <= 60):
            errors.append(
                f"calendar.window_minutes must be between 1 and 60, "
                f"got {self.calendar.window_minutes}"
            )
        return errors

    def _dump_toml(self) -> str:
        lines: list[str] = []
        for section, obj in (
            ("audio", self.audio),
            ("transcription", self.transcription),
            ("ui", self.ui),
            ("calendar", self.calendar),
        ):
            lines.append(f"[{section}]")
            for key, value in asdict(obj).items():
                lines.append(f"{key} = {_toml_repr(value)}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def _toml_repr(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    raise TypeError(f"unsupported TOML value: {value!r}")
