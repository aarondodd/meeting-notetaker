"""WebRTC VAD wrapper.

webrtcvad operates on 10/20/30 ms PCM frames at 8/16/32/48 kHz mono int16.
We standardize on 30 ms frames at 16 kHz to match the ChunkBuffer.

In v0.1 this is exposed but optional -- faster-whisper has its own silero
VAD via `vad_filter=True`. An aggressive pre-trim is useful when the
loopback stream is long stretches of silence.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


FRAME_MS = 30
SAMPLE_RATE = 16000
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000


@dataclass
class VadResult:
    frames_total: int
    frames_voiced: int

    @property
    def voiced_fraction(self) -> float:
        return self.frames_voiced / self.frames_total if self.frames_total else 0.0


def is_voiced_enough(pcm: np.ndarray, *, aggressiveness: int = 2, min_voiced_fraction: float = 0.02) -> bool:
    """Returns True if at least min_voiced_fraction of frames look voiced.

    Falls back to True (assume voiced) if webrtcvad is unavailable, so the
    pipeline never silently drops audio because of a missing optional dep.
    """
    result = analyse(pcm, aggressiveness=aggressiveness)
    if result.frames_total == 0:
        return True
    return result.voiced_fraction >= min_voiced_fraction


def analyse(pcm: np.ndarray, *, aggressiveness: int = 2) -> VadResult:
    try:
        import webrtcvad  # type: ignore[import-not-found]
    except ImportError:
        return VadResult(frames_total=0, frames_voiced=0)
    vad = webrtcvad.Vad(aggressiveness)
    pcm = pcm.astype(np.int16)
    n_frames = len(pcm) // FRAME_SAMPLES
    if n_frames == 0:
        return VadResult(frames_total=0, frames_voiced=0)
    voiced = 0
    for i in range(n_frames):
        frame = pcm[i * FRAME_SAMPLES:(i + 1) * FRAME_SAMPLES].tobytes()
        if vad.is_speech(frame, SAMPLE_RATE):
            voiced += 1
    return VadResult(frames_total=n_frames, frames_voiced=voiced)
