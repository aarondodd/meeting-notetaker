"""Mono downmix + linear resample correctness checks."""
from __future__ import annotations

import numpy as np

from meeting_notetaker.audio.resample import (
    resample_linear_int16,
    to_mono_16k,
    to_mono_int16,
)


def test_to_mono_int16_passes_mono_through():
    pcm = np.array([1, 2, 3, 4], dtype=np.int16)
    out = to_mono_int16(pcm, channels=1)
    assert np.array_equal(out, pcm)


def test_to_mono_int16_averages_stereo():
    # Stereo interleaved: L,R,L,R,...
    pcm = np.array([100, 200, 300, 400, -100, 100], dtype=np.int16)
    out = to_mono_int16(pcm, channels=2)
    # Pairs: (100,200) -> 150, (300,400) -> 350, (-100,100) -> 0
    assert list(out) == [150, 350, 0]


def test_to_mono_int16_truncates_uneven_frames():
    pcm = np.array([10, 20, 30, 40, 50], dtype=np.int16)   # 5 samples; 2-channel needs 4
    out = to_mono_int16(pcm, channels=2)
    assert list(out) == [15, 35]


def test_resample_identity_when_rates_match():
    pcm = np.array([1, 2, 3], dtype=np.int16)
    assert np.array_equal(resample_linear_int16(pcm, src_rate=16000, target_rate=16000), pcm)


def test_resample_downsample_length_correct():
    # 1 second @ 48000 -> 1 second @ 16000 = length 16000
    pcm = np.zeros(48000, dtype=np.int16)
    out = resample_linear_int16(pcm, src_rate=48000, target_rate=16000)
    assert len(out) == 16000


def test_to_mono_16k_end_to_end():
    # 0.1s stereo @ 48000 -> mono @ 16000 = 1600 samples
    pcm = np.zeros(48000 * 2 // 10, dtype=np.int16)
    out = to_mono_16k(pcm, channels=2, src_rate=48000)
    assert len(out) == 1600
    assert out.dtype == np.int16
