"""Voice-activity detection wrapper.

Thin public API over the diarization segmenter's three-tier VAD chain
(silero -> webrtcvad -> energy). Exposed for any future caller that
needs a simple "is this audio mostly voiced?" check without going
through the full turn-segmentation pipeline.

`silero-vad` is the preferred backend on production installs (more
accurate than webrtcvad on real meeting audio, especially noisy
calls). The fallback chain keeps the API usable in environments
where silero or torch can't load.

For per-chunk VAD inside the *transcription* path, faster-whisper has
its own embedded silero pass via `vad_filter=True`; that is a
separate code path with its own `vad_min_silence_ms` setting in the
audio config.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..diarization.segmenter import (
    VAD_FRAME_MS,
    VAD_FRAME_SAMPLES,
    VAD_SAMPLE_RATE,
    _voiced_mask_energy,
    _voiced_mask_silero,
    _voiced_mask_webrtc,
)


FRAME_MS = VAD_FRAME_MS
SAMPLE_RATE = VAD_SAMPLE_RATE
FRAME_SAMPLES = VAD_FRAME_SAMPLES


@dataclass
class VadResult:
    frames_total: int
    frames_voiced: int

    @property
    def voiced_fraction(self) -> float:
        return self.frames_voiced / self.frames_total if self.frames_total else 0.0


def is_voiced_enough(
    pcm: np.ndarray,
    *,
    aggressiveness: int = 2,
    min_voiced_fraction: float = 0.02,
) -> bool:
    """Returns True if at least min_voiced_fraction of frames look voiced.

    Falls back to True (assume voiced) if no VAD backend is available, so
    the pipeline never silently drops audio because of a missing optional
    dep.
    """
    result = analyse(pcm, aggressiveness=aggressiveness)
    if result.frames_total == 0:
        return True
    return result.voiced_fraction >= min_voiced_fraction


def analyse(pcm: np.ndarray, *, aggressiveness: int = 2) -> VadResult:
    """Per-frame voicing analysis on 30ms frames at 16kHz.

    Tries silero-vad first, then webrtcvad, then a fixed-threshold
    energy mask. Returns frames_total=0 if no backend produced a mask
    (treated as "unknown -> assume voiced" by `is_voiced_enough`).
    """
    pcm = pcm.astype(np.int16, copy=False)
    mask = _voiced_mask_silero(pcm, threshold=0.5, min_silence_ms=100)
    if mask.size == 0:
        mask = _voiced_mask_webrtc(pcm, aggressiveness=aggressiveness)
    if mask.size == 0:
        mask = _voiced_mask_energy(pcm)
    if mask.size == 0:
        return VadResult(frames_total=0, frames_voiced=0)
    return VadResult(frames_total=int(mask.size), frames_voiced=int(mask.sum()))
