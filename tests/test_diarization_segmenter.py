"""Energy-based turn segmentation on synthetic audio."""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from meeting_notetaker.diarization.segmenter import (
    Turn,
    find_turns,
    find_turns_in_wav,
    read_wav_mono,
)


def _sine(freq_hz: float, duration_sec: float, sample_rate: int, amplitude: float = 0.5) -> np.ndarray:
    t = np.linspace(0, duration_sec, int(duration_sec * sample_rate), endpoint=False)
    return (np.sin(2 * np.pi * freq_hz * t) * amplitude * 32767).astype(np.int16)


def _silence(duration_sec: float, sample_rate: int) -> np.ndarray:
    return np.zeros(int(duration_sec * sample_rate), dtype=np.int16)


def test_empty_pcm_returns_no_turns():
    assert find_turns(np.zeros(0, dtype=np.int16), 16000) == []


def test_one_voiced_span_yields_one_turn():
    sr = 16000
    pcm = np.concatenate([_silence(1.0, sr), _sine(220, 2.0, sr), _silence(1.0, sr)])
    turns = find_turns(pcm, sr)
    assert len(turns) == 1
    # Span should roughly cover the voiced middle. We allow a little slack
    # since edge frames near the silence boundary may straddle the threshold.
    assert 0.7 < turns[0].t_start < 1.3
    assert 2.7 < turns[0].t_end < 3.3


def test_silence_only_returns_nothing():
    pcm = _silence(3.0, 16000)
    assert find_turns(pcm, 16000) == []


def test_two_voiced_spans_with_long_silence_become_two_turns():
    sr = 16000
    pcm = np.concatenate([
        _sine(200, 1.5, sr),
        _silence(1.0, sr),
        _sine(400, 1.5, sr),
    ])
    turns = find_turns(pcm, sr)
    assert len(turns) == 2


def test_short_silence_between_voiced_spans_bridges_into_one_turn():
    sr = 16000
    pcm = np.concatenate([
        _sine(200, 1.5, sr),
        _silence(0.2, sr),  # below 0.5s min_silence
        _sine(200, 1.5, sr),
    ])
    turns = find_turns(pcm, sr)
    # Short silence should not split the turn.
    assert len(turns) == 1
    assert turns[0].duration > 2.5


def test_too_short_turns_filtered_out():
    sr = 16000
    pcm = np.concatenate([_silence(1.0, sr), _sine(200, 0.3, sr), _silence(1.0, sr)])
    turns = find_turns(pcm, sr, min_turn_sec=1.0)
    assert turns == []


def test_long_turn_is_subdivided_at_max_turn_sec():
    sr = 16000
    pcm = np.concatenate([_silence(0.6, sr), _sine(200, 25.0, sr), _silence(0.6, sr)])
    turns = find_turns(pcm, sr, max_turn_sec=10.0)
    # 25s of continuous voiced audio split into <=10s chunks -> >=3 turns.
    assert len(turns) >= 3
    for turn in turns:
        assert turn.duration <= 10.1


def test_turn_pcm_matches_span(tmp_path):
    sr = 16000
    pcm = np.concatenate([_silence(0.6, sr), _sine(200, 2.0, sr), _silence(0.6, sr)])
    turns = find_turns(pcm, sr)
    assert len(turns) == 1
    expected_len_lo = int((turns[0].t_end - turns[0].t_start) * sr) - 480  # one frame slack
    expected_len_hi = int((turns[0].t_end - turns[0].t_start) * sr) + 480
    assert expected_len_lo <= turns[0].pcm.size <= expected_len_hi


def test_read_wav_mono_handles_stereo(tmp_path):
    sr = 16000
    left = _sine(200, 1.0, sr).astype(np.int16)
    right = _sine(400, 1.0, sr).astype(np.int16)
    interleaved = np.empty(left.size + right.size, dtype=np.int16)
    interleaved[0::2] = left
    interleaved[1::2] = right
    wav_path = tmp_path / "stereo.wav"
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(interleaved.tobytes())
    pcm, got_sr = read_wav_mono(wav_path)
    assert got_sr == sr
    assert pcm.dtype == np.int16
    # Mono length should match per-channel length.
    assert pcm.size == left.size


def test_find_turns_in_wav_end_to_end(tmp_path):
    sr = 16000
    pcm = np.concatenate([_silence(1.0, sr), _sine(220, 2.0, sr), _silence(1.0, sr)])
    wav_path = tmp_path / "audio.wav"
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())
    turns = find_turns_in_wav(wav_path)
    assert len(turns) == 1
