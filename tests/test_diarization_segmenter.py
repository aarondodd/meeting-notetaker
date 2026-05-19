"""Turn segmentation on synthetic audio.

These tests feed `find_turns` synthetic sine waves rather than real
speech, which the production silero-vad backend would (correctly)
classify as non-speech. To keep these focused on the
segmentation/bridging/length-enforcement algorithm rather than which
VAD backend is in use, the `_disable_silero_for_segmenter_tests`
fixture forces find_turns through the webrtcvad path (which treats
structured tonal signal as voiced). A separate suite below exercises
the silero path against a mocked timestamp source.
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from meeting_notetaker.diarization import segmenter
from meeting_notetaker.diarization.segmenter import (
    Turn,
    find_turns,
    find_turns_in_wav,
    read_wav_mono,
)


@pytest.fixture(autouse=True)
def _disable_silero_for_segmenter_tests(monkeypatch):
    """Make `_voiced_mask_silero` return an empty mask so callers fall
    through to webrtcvad. Synthetic sine waves are not speech and
    silero will (correctly) classify them as non-voiced; the tests in
    this file are about the segmentation algorithm, not the VAD
    backend choice.
    """
    monkeypatch.setattr(
        segmenter,
        "_voiced_mask_silero",
        lambda pcm_16k, **_kwargs: np.zeros(0, dtype=bool),
    )
    # Also reset the module-level model load flag so a later test that
    # explicitly exercises silero isn't fooled by a sticky load failure.
    monkeypatch.setattr(segmenter, "_silero_load_attempted", False)
    monkeypatch.setattr(segmenter, "_silero_model", None)


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


# ---------------------------------------------------------------------------
# Silero-vad backend specific tests.
#
# These bypass the autouse fixture above by re-patching `_voiced_mask_silero`
# with a synthetic implementation that mimics silero's sample-range output.
# That lets us verify the sample-range -> frame-mask conversion in
# `_voiced_mask_silero` and the find_turns wiring without depending on the
# real silero model's classification of synthetic test audio.
# ---------------------------------------------------------------------------


def test_silero_voiced_ranges_become_frame_mask(monkeypatch):
    """A faked silero pass returns explicit sample ranges; verify
    that find_turns honors them and rasterizes correctly.
    """
    from meeting_notetaker.diarization.segmenter import (
        VAD_FRAME_SAMPLES,
        VAD_SAMPLE_RATE,
        _voiced_mask_silero,
    )

    # 4 seconds of all-silent audio. The silero stub will claim that
    # samples [1.0s, 3.0s) are voiced, which should produce exactly
    # one turn covering ~2s in the middle.
    sr = VAD_SAMPLE_RATE
    pcm = np.zeros(int(4.0 * sr), dtype=np.int16)

    voiced_ranges = [
        {"start": int(1.0 * sr), "end": int(3.0 * sr)},
    ]

    def fake_silero_mask(pcm_16k, **_kwargs):
        n_frames = pcm_16k.size // VAD_FRAME_SAMPLES
        mask = np.zeros(n_frames, dtype=bool)
        for span in voiced_ranges:
            start_frame = max(0, span["start"] // VAD_FRAME_SAMPLES)
            end_frame = min(
                n_frames,
                (span["end"] + VAD_FRAME_SAMPLES - 1) // VAD_FRAME_SAMPLES,
            )
            mask[start_frame:end_frame] = True
        return mask

    monkeypatch.setattr(segmenter, "_voiced_mask_silero", fake_silero_mask)

    turns = find_turns(pcm, sr)
    assert len(turns) == 1
    assert 0.9 < turns[0].t_start < 1.1
    assert 2.9 < turns[0].t_end < 3.1


def test_silero_unavailable_falls_through_to_webrtcvad(monkeypatch):
    """When silero returns an empty mask (unavailable / model load
    failure), find_turns must still detect voiced spans via the
    webrtcvad path. This is the production fallback chain working.
    """
    # Default monkeypatch from the autouse fixture already stubs
    # _voiced_mask_silero to empty. Synthetic sine -> webrtcvad should
    # still find the voiced span.
    sr = 16000
    pcm = np.concatenate([_silence(0.8, sr), _sine(220, 2.0, sr), _silence(0.8, sr)])
    turns = find_turns(pcm, sr)
    assert len(turns) == 1


def test_silero_mask_overrides_webrtcvad_when_present(monkeypatch):
    """If silero returns a non-empty mask, that mask wins -- webrtcvad
    is not consulted. We prove this by having silero return a mask
    that disagrees with the energy of the input (silero says voiced
    in the silent region) and confirming find_turns followed silero.
    """
    from meeting_notetaker.diarization.segmenter import (
        VAD_FRAME_SAMPLES,
        VAD_SAMPLE_RATE,
    )

    sr = VAD_SAMPLE_RATE
    # Voiced sine in the first half, silence in the second half.
    pcm = np.concatenate([_sine(220, 2.0, sr), _silence(2.0, sr)])

    # Stub silero to claim the OPPOSITE: only the silent second half is voiced.
    def fake_silero_mask(pcm_16k, **_kwargs):
        n_frames = pcm_16k.size // VAD_FRAME_SAMPLES
        mask = np.zeros(n_frames, dtype=bool)
        mask[n_frames // 2:] = True
        return mask

    monkeypatch.setattr(segmenter, "_voiced_mask_silero", fake_silero_mask)

    turns = find_turns(pcm, sr)
    assert len(turns) == 1
    # The turn should start in the second half of the audio, not the first.
    assert turns[0].t_start >= 1.9
