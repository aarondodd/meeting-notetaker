"""batch_transcribe progress callback + per-segment percentages."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from meeting_notetaker.transcription.worker import batch_transcribe


@dataclass
class _Seg:
    start: float
    end: float
    text: str


class _FakeInfo:
    def __init__(self, duration: float) -> None:
        self.duration = duration


class _FakeModel:
    def __init__(self, segments: list[_Seg], duration: float) -> None:
        self.segments = segments
        self.duration = duration
        self.transcribe_calls: list[dict] = []

    def transcribe(self, _path, **kwargs):
        self.transcribe_calls.append(kwargs)
        return iter(self.segments), _FakeInfo(self.duration)


def test_batch_transcribe_emits_progress_pct_per_segment(tmp_path: Path):
    model = _FakeModel(
        segments=[
            _Seg(start=0.0, end=10.0, text="hello"),
            _Seg(start=10.0, end=25.0, text="world"),
            _Seg(start=25.0, end=40.0, text="goodbye"),
        ],
        duration=40.0,
    )
    wav = tmp_path / "x.wav"
    wav.write_bytes(b"\x00" * 100)  # not actually read by the fake
    observed: list[tuple[str, float]] = []
    segs = batch_transcribe(
        wav,
        model,
        source="mic",
        on_progress_pct=lambda source, pct: observed.append((source, pct)),
    )
    # 3 from segments + 1 explicit final = 4 callbacks; final must be (mic, 1.0).
    assert len(observed) >= 3
    assert observed[-1] == ("mic", 1.0)
    # Intermediate values monotonic increasing within [0, 1].
    pcts = [pct for _, pct in observed]
    for prev, curr in zip(pcts, pcts[1:]):
        assert curr >= prev
        assert 0.0 <= curr <= 1.0
    assert len(segs) == 3
    assert segs[0].text == "hello"


def test_batch_transcribe_progress_pct_caps_at_one_when_segment_overshoots(tmp_path: Path):
    # faster-whisper occasionally reports seg.end slightly past info.duration
    # for the last segment; we must clamp the percentage.
    model = _FakeModel(
        segments=[_Seg(start=0.0, end=10.5, text="hi")],
        duration=10.0,
    )
    wav = tmp_path / "x.wav"
    wav.write_bytes(b"\x00" * 100)
    observed: list[float] = []
    batch_transcribe(
        wav, model, source="mic",
        on_progress_pct=lambda _src, pct: observed.append(pct),
    )
    assert all(0.0 <= p <= 1.0 for p in observed)


def test_batch_transcribe_passes_beam_size(tmp_path: Path):
    model = _FakeModel(segments=[], duration=1.0)
    wav = tmp_path / "x.wav"
    wav.write_bytes(b"\x00" * 100)
    batch_transcribe(wav, model, source="mic", beam_size=1)
    assert model.transcribe_calls[0]["beam_size"] == 1


def test_batch_transcribe_returns_empty_list_for_no_segments(tmp_path: Path):
    model = _FakeModel(segments=[], duration=10.0)
    wav = tmp_path / "x.wav"
    wav.write_bytes(b"\x00" * 100)
    assert batch_transcribe(wav, model, source="mic") == []
