"""Long-audio chunking for batch_transcribe.

Regression coverage for issue #137: a 2.5h recording ran
faster-whisper's feature extractor over the whole file, which tried
to allocate 1.22 GiB for a complex64 FFT buffer and failed, killing
the batch pass before any segments came back. The fix slices the
input into overlapping 15 min windows and transcribes each as a
numpy array so the per-call FFT stays under 150 MiB.

The tests stub `model.transcribe(audio_np, ...)` with a fake that
returns per-chunk fixture segments keyed on `audio_np.shape`, and
exercise:

* duration reader handles real WAVs and refuses garbage silently
* chunk iterator yields overlapping slices with the right shapes
* segment timestamps come out absolute (chunk offset + seg.start)
* boundary overlap is deduped so a segment is not emitted twice
* progress callbacks reach 1.0 at end of the last chunk
* short WAVs still route through the legacy single-call path
"""
from __future__ import annotations

import math
import struct
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

from meeting_notetaker.transcription import worker as worker_mod
from meeting_notetaker.transcription.worker import (
    LONG_AUDIO_CHUNK_S,
    LONG_AUDIO_OVERLAP_S,
    LONG_AUDIO_THRESHOLD_S,
    _iter_decoded_chunks,
    _wav_duration_seconds,
    batch_transcribe,
)


pytest.importorskip("av")


# ---------------------------------------------------------------------------
# helpers


@dataclass
class _Seg:
    start: float
    end: float
    text: str


class _FakeInfo:
    def __init__(self, duration: float, vad: float | None = None) -> None:
        self.duration = duration
        if vad is not None:
            self.duration_after_vad = vad


def _write_tone_wav(
    path: Path,
    *,
    duration_s: float,
    sample_rate: int = 16000,
    freq_hz: float = 220.0,
) -> None:
    """Write a short mono 16-bit tone. Small enough for an in-test file;
    the duration is a hint to `_wav_duration_seconds`, not to the
    model (the fake model never actually decodes)."""
    n = int(duration_s * sample_rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        for i in range(n):
            sample = int(32767 * 0.3 * math.sin(2 * math.pi * freq_hz * i / sample_rate))
            w.writeframes(struct.pack("<h", sample))


# ---------------------------------------------------------------------------
# _wav_duration_seconds


def test_wav_duration_reads_valid_file(tmp_path: Path):
    wav = tmp_path / "x.wav"
    _write_tone_wav(wav, duration_s=0.5)
    duration = _wav_duration_seconds(wav)
    assert duration is not None
    assert 0.49 <= duration <= 0.51


def test_wav_duration_returns_none_for_nonwav(tmp_path: Path):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"\x00" * 100)
    assert _wav_duration_seconds(bad) is None


def test_wav_duration_returns_none_for_missing(tmp_path: Path):
    assert _wav_duration_seconds(tmp_path / "nope.wav") is None


# ---------------------------------------------------------------------------
# _iter_decoded_chunks


def test_iter_decoded_chunks_single_chunk_for_short_audio(tmp_path: Path):
    """A WAV shorter than chunk_s produces exactly one chunk whose
    length matches the file."""
    wav = tmp_path / "short.wav"
    _write_tone_wav(wav, duration_s=0.5)
    chunks = list(_iter_decoded_chunks(wav, chunk_s=10.0, overlap_s=1.0))
    assert len(chunks) == 1
    chunk_start, audio = chunks[0]
    assert chunk_start == 0.0
    # ~0.5s * 16000 = 8000 samples, allow a few frames of resampler slop.
    assert 7900 <= audio.shape[0] <= 8100
    assert audio.dtype == np.float32


def test_iter_decoded_chunks_overlaps_consecutive_chunks(tmp_path: Path):
    """A WAV longer than chunk_s produces multiple overlapping
    chunks whose starts are spaced by chunk_s - overlap_s."""
    wav = tmp_path / "long.wav"
    _write_tone_wav(wav, duration_s=2.5)
    chunks = list(_iter_decoded_chunks(wav, chunk_s=1.0, overlap_s=0.25))
    assert len(chunks) >= 3
    starts = [s for s, _ in chunks]
    step = 1.0 - 0.25
    for i, s in enumerate(starts[:-1]):
        # Successive non-final chunks step by (chunk_s - overlap_s).
        assert s == pytest.approx(i * step, abs=1e-6)
    # First chunk must be a full chunk_s worth of samples.
    _, first_audio = chunks[0]
    assert first_audio.shape[0] == int(1.0 * 16000)


def test_iter_decoded_chunks_covers_entire_audio(tmp_path: Path):
    """Last chunk's end time reaches the file's end (within one
    resampler frame)."""
    wav = tmp_path / "long.wav"
    _write_tone_wav(wav, duration_s=3.0)
    chunks = list(_iter_decoded_chunks(wav, chunk_s=1.0, overlap_s=0.25))
    last_start, last_audio = chunks[-1]
    last_end = last_start + last_audio.shape[0] / 16000.0
    assert last_end == pytest.approx(3.0, abs=0.02)


# ---------------------------------------------------------------------------
# batch_transcribe with chunking (fake model)


class _ChunkTrackingModel:
    """Fake faster-whisper model: records each transcribe() call and
    returns fixture segments shifted into chunk-local coordinates.

    Each call sees a numpy audio array (not a path), so the test
    harness can assert the chunk size that reached the model and
    inject segment lists per call index.
    """

    def __init__(
        self,
        chunk_segments: list[list[_Seg]],
        chunk_duration_hint: float,
    ) -> None:
        self.chunk_segments = chunk_segments
        self.chunk_duration_hint = chunk_duration_hint
        self.calls: list[dict] = []

    def transcribe(self, audio, **kwargs):
        index = len(self.calls)
        if index >= len(self.chunk_segments):
            raise AssertionError(
                f"model got {index + 1} transcribe calls, "
                f"test only provided {len(self.chunk_segments)} fixtures",
            )
        self.calls.append({
            "kwargs": kwargs,
            "audio_type": type(audio).__name__,
            "audio_shape": getattr(audio, "shape", None),
        })
        segs = self.chunk_segments[index]
        return iter(segs), _FakeInfo(self.chunk_duration_hint, vad=0.5)


def test_batch_transcribe_long_audio_shifts_segment_times(
    monkeypatch, tmp_path: Path,
):
    """Segments from the Nth chunk come out with t_start = chunk_start +
    seg.start (absolute wav-local time), not 0 + seg.start."""
    monkeypatch.setattr(worker_mod, "LONG_AUDIO_THRESHOLD_S", 1.0)
    monkeypatch.setattr(worker_mod, "LONG_AUDIO_CHUNK_S", 1.0)
    monkeypatch.setattr(worker_mod, "LONG_AUDIO_OVERLAP_S", 0.2)
    wav = tmp_path / "long.wav"
    _write_tone_wav(wav, duration_s=3.0)
    # 3s / (1.0 - 0.2) = ~4 chunks starting at 0, 0.8, 1.6, 2.4.
    # Give each chunk one segment at seg.start=0.1s (well inside the
    # chunk's keep window so the dedup doesn't drop them).
    per_chunk = [
        [_Seg(start=0.1, end=0.2, text="alpha")],
        [_Seg(start=0.1, end=0.2, text="bravo")],
        [_Seg(start=0.1, end=0.2, text="charlie")],
        [_Seg(start=0.1, end=0.2, text="delta")],
    ]
    model = _ChunkTrackingModel(per_chunk, chunk_duration_hint=1.0)
    segs = batch_transcribe(wav, model, source="mic")
    assert len(segs) == 4
    # Absolute t_start = chunk_start + 0.1. Chunk starts: 0, 0.8, 1.6, 2.4.
    expected_starts = [0.1, 0.9, 1.7, 2.5]
    actual_starts = sorted(s.t_start for s in segs)
    for exp, got in zip(expected_starts, actual_starts):
        assert got == pytest.approx(exp, abs=1e-3)


def test_batch_transcribe_long_audio_dedupes_boundary_segments(
    monkeypatch, tmp_path: Path,
):
    """A word straddling the boundary is seen in both adjacent chunks.
    The midpoint rule keeps exactly one copy."""
    monkeypatch.setattr(worker_mod, "LONG_AUDIO_THRESHOLD_S", 1.0)
    monkeypatch.setattr(worker_mod, "LONG_AUDIO_CHUNK_S", 1.0)
    monkeypatch.setattr(worker_mod, "LONG_AUDIO_OVERLAP_S", 0.4)
    wav = tmp_path / "long.wav"
    _write_tone_wav(wav, duration_s=1.6)
    # 1.6s / (1.0 - 0.4) = 3 chunks at starts 0, 0.6, 1.2. Make chunk 0
    # report a segment in its trailing overlap, and chunk 1 report the
    # SAME utterance in its leading overlap. Exactly one should survive.
    # Chunk 0 ends at 1.0; overlap region is 0.6..1.0; midpoint is 0.8.
    # Segment at wav-local 0.85 belongs to chunk 1.
    per_chunk = [
        [_Seg(start=0.85, end=0.95, text="straddle")],  # absolute 0.85
        [_Seg(start=0.25, end=0.35, text="straddle")],  # absolute 0.85 as well
        [_Seg(start=0.1, end=0.2, text="later")],       # absolute 1.3
    ]
    model = _ChunkTrackingModel(per_chunk, chunk_duration_hint=1.0)
    segs = batch_transcribe(wav, model, source="mic")
    texts = [s.text for s in segs]
    assert texts.count("straddle") == 1, (
        f"expected exactly one 'straddle' segment after boundary dedup, "
        f"got {texts}"
    )


def test_batch_transcribe_long_audio_progress_pct_reaches_one(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setattr(worker_mod, "LONG_AUDIO_THRESHOLD_S", 1.0)
    monkeypatch.setattr(worker_mod, "LONG_AUDIO_CHUNK_S", 1.0)
    monkeypatch.setattr(worker_mod, "LONG_AUDIO_OVERLAP_S", 0.2)
    wav = tmp_path / "long.wav"
    _write_tone_wav(wav, duration_s=2.0)
    per_chunk = [
        [_Seg(start=0.2, end=0.3, text="a")],
        [_Seg(start=0.2, end=0.3, text="b")],
        [_Seg(start=0.2, end=0.3, text="c")],
    ]
    model = _ChunkTrackingModel(per_chunk, chunk_duration_hint=1.0)
    observed: list[tuple[str, float]] = []
    batch_transcribe(
        wav, model, source="sys",
        on_progress_pct=lambda src, pct: observed.append((src, pct)),
    )
    assert observed[-1] == ("sys", 1.0)
    # All pcts in [0, 1], monotonically non-decreasing.
    pcts = [p for _, p in observed]
    for prev, curr in zip(pcts, pcts[1:]):
        assert curr >= prev
        assert 0.0 <= curr <= 1.0


def test_batch_transcribe_long_audio_progress_pct_is_monotonic(
    monkeypatch, tmp_path: Path,
):
    """Overlap means chunk N+1's first few segments cover audio already
    seen by chunk N. The raw pct from `seg.end / duration` could go
    backwards there; the output stream must still be non-decreasing."""
    monkeypatch.setattr(worker_mod, "LONG_AUDIO_THRESHOLD_S", 1.0)
    monkeypatch.setattr(worker_mod, "LONG_AUDIO_CHUNK_S", 1.0)
    monkeypatch.setattr(worker_mod, "LONG_AUDIO_OVERLAP_S", 0.4)
    wav = tmp_path / "long.wav"
    _write_tone_wav(wav, duration_s=1.6)
    # Chunk 0 (start=0): segment ending at 0.95 -> pct 0.59
    # Chunk 1 (start=0.6): first segment ending at wav-local 0.70
    #                      -> raw pct 0.44 (WOULD regress). Clamp holds it.
    # Chunk 2 (start=1.2): segment ending at wav-local 1.5 -> pct 0.94
    per_chunk = [
        [_Seg(start=0.0, end=0.95, text="one")],
        [_Seg(start=0.0, end=0.10, text="overlap")],
        [_Seg(start=0.1, end=0.3, text="three")],
    ]
    model = _ChunkTrackingModel(per_chunk, chunk_duration_hint=1.0)
    observed: list[float] = []
    batch_transcribe(
        wav, model, source="mic",
        on_progress_pct=lambda _src, pct: observed.append(pct),
    )
    for prev, curr in zip(observed, observed[1:]):
        assert curr >= prev, (
            f"progress pct regressed across chunks: {observed}"
        )
    assert observed[-1] == 1.0


def test_batch_transcribe_long_audio_passes_numpy_not_path(
    monkeypatch, tmp_path: Path,
):
    """model.transcribe must see a numpy array (chunk audio), not the
    original WAV path. If it got a path, the memory bug is still live."""
    monkeypatch.setattr(worker_mod, "LONG_AUDIO_THRESHOLD_S", 1.0)
    monkeypatch.setattr(worker_mod, "LONG_AUDIO_CHUNK_S", 1.0)
    monkeypatch.setattr(worker_mod, "LONG_AUDIO_OVERLAP_S", 0.2)
    wav = tmp_path / "long.wav"
    _write_tone_wav(wav, duration_s=1.5)
    per_chunk = [[], []]
    model = _ChunkTrackingModel(per_chunk, chunk_duration_hint=1.0)
    batch_transcribe(wav, model, source="mic")
    assert model.calls, "fake model should have been called at least once"
    for call in model.calls:
        assert call["audio_type"] == "ndarray", (
            f"long-audio path must pass numpy to model.transcribe; "
            f"got {call['audio_type']}"
        )
        assert call["audio_shape"] is not None
        # Chunk is <= chunk_s + overlap_s worth of samples at 16 kHz.
        assert call["audio_shape"][0] <= int(1.2 * 16000)


def test_batch_transcribe_short_audio_bypasses_chunking(tmp_path: Path):
    """A WAV below LONG_AUDIO_THRESHOLD_S must NOT go through the
    chunker; model.transcribe is called exactly once and sees the WAV
    path string (legacy path)."""
    wav = tmp_path / "short.wav"
    _write_tone_wav(wav, duration_s=0.5)
    assert LONG_AUDIO_THRESHOLD_S > 0.5  # sanity-check the default
    seen: list[dict] = []

    class _OneShotModel:
        def transcribe(self, audio, **kwargs):
            seen.append({"type": type(audio).__name__, "value": audio})
            return iter([_Seg(start=0.0, end=0.4, text="hi")]), _FakeInfo(0.5)

    segs = batch_transcribe(wav, _OneShotModel(), source="mic")
    assert len(seen) == 1
    assert seen[0]["type"] == "str"  # legacy path passed str(wav_path)
    assert len(segs) == 1
    assert segs[0].text == "hi"


def test_batch_transcribe_long_audio_summary_log_sums_vad(
    monkeypatch, tmp_path: Path, caplog,
):
    import logging

    monkeypatch.setattr(worker_mod, "LONG_AUDIO_THRESHOLD_S", 1.0)
    monkeypatch.setattr(worker_mod, "LONG_AUDIO_CHUNK_S", 1.0)
    monkeypatch.setattr(worker_mod, "LONG_AUDIO_OVERLAP_S", 0.2)
    wav = tmp_path / "long.wav"
    _write_tone_wav(wav, duration_s=2.0)
    per_chunk = [
        [_Seg(start=0.0, end=0.5, text="a")],
        [_Seg(start=0.0, end=0.5, text="b")],
        [_Seg(start=0.0, end=0.5, text="c")],
    ]
    model = _ChunkTrackingModel(per_chunk, chunk_duration_hint=1.0)
    with caplog.at_level(
        logging.INFO, logger="meeting_notetaker.transcription.worker",
    ):
        batch_transcribe(wav, model, source="mic")
    summaries = [
        r.message for r in caplog.records
        if "batch_transcribe mic done" in r.message
    ]
    assert len(summaries) == 1
    # Each fake chunk reports vad=0.5; 3 chunks -> 1.5 total.
    assert "vad_surv=1.5s" in summaries[0]
    # Duration is the wav-local total (not chunk-local).
    assert "input=2.0s" in summaries[0]


# Expose the module-level constants so a reviewer can see what the
# defaults are without jumping into worker.py.
def test_long_audio_defaults_are_sane():
    assert LONG_AUDIO_THRESHOLD_S >= LONG_AUDIO_CHUNK_S
    assert LONG_AUDIO_OVERLAP_S > 0
    assert LONG_AUDIO_OVERLAP_S < LONG_AUDIO_CHUNK_S / 10
