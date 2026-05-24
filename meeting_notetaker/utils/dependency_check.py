"""Runtime self-test for every dependency the app touches.

The frozen .exe build will silently ship a missing dependency if
PyInstaller's static analysis misses an import (most commonly when a
library resolves classes by string at runtime, like SpeechBrain's
yaml hparams). The user only finds out when the feature fails mid-meeting.

This module enumerates every external dependency by feature group and
attempts to import it. The result is structured data that the UI
dialog renders, and that tests assert against.
"""
from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Status(str, Enum):
    OK = "OK"
    MISSING = "MISSING"
    SKIP = "SKIP"  # not applicable on this platform


@dataclass(frozen=True)
class DependencyResult:
    name: str
    feature: str
    status: Status
    detail: str = ""  # version when OK, error text when MISSING, reason when SKIP


@dataclass(frozen=True)
class _Check:
    """A single import check.

    `name` is the pip distribution name shown to the user; `module` is
    what we actually try to import (sometimes they differ, e.g. the
    `webrtcvad-wheels` distribution provides `import webrtcvad`).
    """
    name: str
    feature: str
    module: str
    windows_only: bool = False
    # If set, skip the check when this expression evaluates to False
    # (used for ad-hoc rules beyond windows_only).
    skip_reason: Optional[str] = None


_CORE: tuple[_Check, ...] = (
    _Check("PyQt6", "Core UI", "PyQt6"),
    _Check("numpy", "Core", "numpy"),
    _Check("pyperclip", "Clipboard", "pyperclip"),
)


_TRANSCRIPTION: tuple[_Check, ...] = (
    _Check("faster-whisper", "Transcription", "faster_whisper"),
    _Check("ctranslate2", "Transcription (backend)", "ctranslate2"),
)


_AUDIO_CAPTURE: tuple[_Check, ...] = (
    _Check("PyAudio", "Mic capture", "pyaudio"),
    _Check("PyAudioWPatch", "System-audio capture (WASAPI loopback)", "pyaudiowpatch", windows_only=True),
    _Check("sounddevice", "Device enumeration", "sounddevice"),
)


_VAD: tuple[_Check, ...] = (
    _Check("silero-vad", "Voice-activity detection (primary)", "silero_vad"),
    _Check("webrtcvad-wheels", "Voice-activity detection (fallback)", "webrtcvad"),
)


# SpeechBrain's hparam yaml resolves classes from string paths at load
# time. PyInstaller's static analysis cannot see those, so we explicitly
# probe each leaf the ECAPA-TDNN pipeline pulls. If any of these fail in
# the frozen build, add the missing name to meeting_notetaker.spec's
# hiddenimports list and rebuild.
_SPEAKER_ID: tuple[_Check, ...] = (
    _Check("speechbrain", "Speaker ID (top-level)", "speechbrain"),
    _Check("speechbrain.inference.speaker", "Speaker ID (EncoderClassifier entry point)", "speechbrain.inference.speaker"),
    _Check("speechbrain.lobes.models.ECAPA_TDNN", "Speaker ID (ECAPA-TDNN model)", "speechbrain.lobes.models.ECAPA_TDNN"),
    _Check("speechbrain.processing.features", "Speaker ID (mel-spectrogram frontend)", "speechbrain.processing.features"),
    _Check("speechbrain.utils.fetching", "Speaker ID (model fetching helpers)", "speechbrain.utils.fetching"),
    _Check("torch", "Speaker ID (tensor backend)", "torch"),
    _Check("torchaudio", "Speaker ID (audio I/O)", "torchaudio"),
    _Check("scipy", "Speaker ID (DSP helpers)", "scipy"),
    _Check("huggingface_hub", "Speaker ID (model download)", "huggingface_hub"),
    _Check("sentencepiece", "Speaker ID (transitive tokenizer)", "sentencepiece"),
)


_NETWORKING: tuple[_Check, ...] = (
    _Check("truststore", "Corporate TLS (OS certificate store)", "truststore", windows_only=True),
    _Check("certifi", "TLS root certificates", "certifi"),
)


_OUTLOOK: tuple[_Check, ...] = (
    _Check("pywin32 (win32com.client)", "Outlook calendar", "win32com.client", windows_only=True),
    # win32timezone is loaded lazily by win32com.client.dynamic.__getattr__
    # the first time a COM property returns a typed datetime (e.g.
    # outlook_calendar reading item.Start / item.End). PyInstaller's static
    # analysis cannot see that path, so we list it explicitly here AND in
    # the spec's hiddenimports. The check catches a repeat of the 2026-05-21
    # frozen-build regression: calendar fetch silently returned "no entries
    # found" because every parse raised ModuleNotFoundError mid-iteration.
    _Check("pywin32 (win32timezone)", "Outlook calendar (COM date handling)", "win32timezone", windows_only=True),
)


_AD_HOC_DETECT: tuple[_Check, ...] = (
    _Check("pycaw", "Ad-hoc meeting detect (audio mixer)", "pycaw", windows_only=True),
    _Check("psutil", "Ad-hoc meeting detect (process names)", "psutil", windows_only=True),
)


# Synthesis automation (v0.6.3+). The bridge module is pure stdlib so
# it never goes MISSING in a healthy bundle -- this entry mostly
# documents that the package was packaged in. winreg is the registry
# helper used by installer.register_native_host on Windows; checking
# it surfaces a frozen-build packaging bug if it were ever dropped.
# Screen capture (v0.6.5+) + video export. mss does conditional
# platform imports, Pillow dynamically loads codec plugins on Image.open
# / Image.save, and PyAV ships bundled FFmpeg shared libs. All three
# are listed in the spec's collect_all and probed here so a missing-
# from-bundle case surfaces at build-gate time rather than at runtime
# when a user tries to take a screenshot or export a video.
_SCREEN_CAPTURE: tuple[_Check, ...] = (
    _Check("mss", "Screen capture (region grabber)", "mss"),
    _Check("Pillow", "Screen capture + video export (image I/O)", "PIL"),
    _Check("PyAV", "Video export (MP4 mux + libx264 + AAC)", "av"),
)


_AUTOMATION: tuple[_Check, ...] = (
    _Check(
        "meeting_notetaker.automation",
        "Synthesis automation bridge",
        "meeting_notetaker.automation.bridge",
    ),
    _Check(
        "winreg",
        "Windows registry (native-host registration)",
        "winreg",
        windows_only=True,
    ),
)


_GROUPS: tuple[tuple[str, tuple[_Check, ...]], ...] = (
    ("Core", _CORE),
    ("Transcription", _TRANSCRIPTION),
    ("Audio capture", _AUDIO_CAPTURE),
    ("Voice-activity detection", _VAD),
    ("Speaker identification", _SPEAKER_ID),
    ("Networking / TLS", _NETWORKING),
    ("Outlook calendar (Windows)", _OUTLOOK),
    ("Ad-hoc meeting detection (Windows)", _AD_HOC_DETECT),
    ("Screen capture + video export", _SCREEN_CAPTURE),
    ("Synthesis automation", _AUTOMATION),
)


def _platform_is_windows() -> bool:
    return sys.platform == "win32"


def _probe(check: _Check) -> DependencyResult:
    if check.windows_only and not _platform_is_windows():
        return DependencyResult(
            name=check.name,
            feature=check.feature,
            status=Status.SKIP,
            detail="Windows-only -- not applicable on this platform",
        )
    try:
        mod = importlib.import_module(check.module)
    except ImportError as exc:
        return DependencyResult(
            name=check.name,
            feature=check.feature,
            status=Status.MISSING,
            detail=str(exc),
        )
    except Exception as exc:
        # Some modules raise non-ImportError exceptions on a broken
        # install (e.g. torch with a CUDA mismatch). Treat anything that
        # blocks import as MISSING but flag the unusual exception type
        # so the user can investigate.
        return DependencyResult(
            name=check.name,
            feature=check.feature,
            status=Status.MISSING,
            detail=f"{type(exc).__name__}: {exc}",
        )
    version = getattr(mod, "__version__", "")
    detail = f"version {version}" if version else "imported"
    return DependencyResult(
        name=check.name,
        feature=check.feature,
        status=Status.OK,
        detail=detail,
    )


def run_checks() -> list[tuple[str, list[DependencyResult]]]:
    """Run every dependency check and return results grouped by feature.

    Returns a list of (group_name, [results]) tuples in the same order
    as `_GROUPS` so the UI can render predictable sections.
    """
    return [(group_name, [_probe(c) for c in checks]) for group_name, checks in _GROUPS]


def format_report(grouped: list[tuple[str, list[DependencyResult]]]) -> str:
    """Render the results as plain text for the clipboard."""
    lines = ["Meeting Notetaker -- Dependency check", ""]
    counts = {Status.OK: 0, Status.MISSING: 0, Status.SKIP: 0}
    for group, results in grouped:
        lines.append(f"## {group}")
        for r in results:
            lines.append(f"  [{r.status.value:>7}]  {r.name}  --  {r.feature}")
            if r.detail:
                lines.append(f"             {r.detail}")
            counts[r.status] += 1
        lines.append("")
    lines.append(
        f"Summary: {counts[Status.OK]} OK, "
        f"{counts[Status.MISSING]} MISSING, "
        f"{counts[Status.SKIP]} SKIP"
    )
    return "\n".join(lines)


def summary(grouped: list[tuple[str, list[DependencyResult]]]) -> dict[Status, int]:
    """Counts by status -- handy for the dialog header."""
    counts = {Status.OK: 0, Status.MISSING: 0, Status.SKIP: 0}
    for _, results in grouped:
        for r in results:
            counts[r.status] += 1
    return counts
