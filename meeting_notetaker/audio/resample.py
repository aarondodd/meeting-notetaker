"""Mono downmix + linear resample helpers.

We don't ship scipy or librosa just for resampling -- linear interpolation
through numpy is good enough for Whisper's tolerance. The input is always
int16 interleaved PCM; output is int16 mono at the target rate.
"""
from __future__ import annotations

import numpy as np


def to_mono_int16(pcm: np.ndarray, *, channels: int) -> np.ndarray:
    """Downmix interleaved int16 PCM to mono."""
    if pcm.dtype != np.int16:
        pcm = pcm.astype(np.int16)
    if channels <= 1:
        return pcm.reshape(-1)
    # Reshape with allow_copy=False would be unsafe if length not a multiple;
    # truncate to a clean boundary first.
    n = (len(pcm) // channels) * channels
    if n != len(pcm):
        pcm = pcm[:n]
    matrix = pcm.reshape(-1, channels).astype(np.int32)
    mono = matrix.mean(axis=1)
    return np.clip(mono, -32768, 32767).astype(np.int16)


def resample_linear_int16(pcm: np.ndarray, *, src_rate: int, target_rate: int) -> np.ndarray:
    """Linear-interpolate mono int16 PCM from src_rate to target_rate."""
    if src_rate == target_rate or len(pcm) == 0:
        return pcm.astype(np.int16, copy=False)
    duration = len(pcm) / src_rate
    n_out = max(0, int(round(duration * target_rate)))
    if n_out == 0:
        return np.zeros(0, dtype=np.int16)
    x_old = np.linspace(0.0, duration, len(pcm), endpoint=False)
    x_new = np.linspace(0.0, duration, n_out, endpoint=False)
    resampled = np.interp(x_new, x_old, pcm.astype(np.float32))
    return np.clip(resampled, -32768, 32767).astype(np.int16)


def to_mono_16k(pcm: np.ndarray, *, channels: int, src_rate: int, target_rate: int = 16000) -> np.ndarray:
    """Downmix to mono, then resample to target_rate. Convenience wrapper."""
    mono = to_mono_int16(pcm, channels=channels)
    return resample_linear_int16(mono, src_rate=src_rate, target_rate=target_rate)
