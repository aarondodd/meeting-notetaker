"""batch_transcribe progress callback + per-segment percentages."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from meeting_notetaker.transcription.worker import batch_transcribe


@dataclass
class _Seg:
    start: float
    end: float
    text: str


class _FakeInfo:
    def __init__(
        self,
        duration: float,
        *,
        duration_after_vad: float | None = None,
    ) -> None:
        self.duration = duration
        # #118: newer faster-whisper versions expose this; older
        # ones don't. The summary logger handles both.
        if duration_after_vad is not None:
            self.duration_after_vad = duration_after_vad


class _FakeModel:
    def __init__(
        self,
        segments: list[_Seg],
        duration: float,
        *,
        duration_after_vad: float | None = None,
    ) -> None:
        self.segments = segments
        self.duration = duration
        self.duration_after_vad = duration_after_vad
        self.transcribe_calls: list[dict] = []

    def transcribe(self, _path, **kwargs):
        self.transcribe_calls.append(kwargs)
        return iter(self.segments), _FakeInfo(
            self.duration, duration_after_vad=self.duration_after_vad,
        )


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


# ---- #118: post-batch diagnostic log line ---------------------------------


def _batch_summary_record(caplog) -> str:
    """Return the single 'batch_transcribe ... done' INFO log message."""
    msgs = [
        r.message for r in caplog.records
        if r.name == "meeting_notetaker.transcription.worker"
        and "batch_transcribe" in r.message
        and "done" in r.message
    ]
    assert len(msgs) == 1, f"expected exactly one summary log, got {msgs}"
    return msgs[0]


def test_batch_summary_log_includes_segment_count_and_span(tmp_path, caplog):
    model = _FakeModel(
        segments=[
            _Seg(start=10.0, end=20.0, text="hello there"),
            _Seg(start=25.0, end=40.0, text="general kenobi"),
        ],
        duration=120.0,
        duration_after_vad=45.0,
    )
    wav = tmp_path / "x.wav"
    wav.write_bytes(b"\x00" * 100)
    with caplog.at_level(
        logging.INFO, logger="meeting_notetaker.transcription.worker",
    ):
        batch_transcribe(wav, model, source="sys")
    msg = _batch_summary_record(caplog)
    assert "sys" in msg
    assert "input=120.0s" in msg
    assert "vad_surv=45.0s" in msg
    assert "segments=2" in msg
    # span = first segment start (10) to last segment end (40).
    assert "span=10.0-40.0s" in msg
    # speech = sum of (end-start) = 10 + 15 = 25.
    assert "speech=25.0s" in msg
    # chars = len('hello there') + len('general kenobi') = 11 + 14 = 25.
    assert "chars=25" in msg


def test_batch_summary_log_no_segments_path(tmp_path, caplog):
    model = _FakeModel(
        segments=[], duration=60.0, duration_after_vad=0.5,
    )
    wav = tmp_path / "x.wav"
    wav.write_bytes(b"\x00" * 100)
    with caplog.at_level(
        logging.INFO, logger="meeting_notetaker.transcription.worker",
    ):
        batch_transcribe(wav, model, source="mic")
    msg = _batch_summary_record(caplog)
    assert "mic" in msg
    assert "input=60.0s" in msg
    assert "vad_surv=0.5s" in msg
    assert "segments=0" in msg
    assert "no speech transcribed" in msg


def test_batch_summary_log_missing_duration_after_vad_is_na(tmp_path, caplog):
    """Older faster-whisper versions don't expose duration_after_vad;
    the summary line must degrade to 'n/a' rather than crash."""
    model = _FakeModel(
        segments=[_Seg(start=0.0, end=5.0, text="hi")],
        duration=10.0,
        # No duration_after_vad -- _FakeInfo will omit the attribute.
    )
    wav = tmp_path / "x.wav"
    wav.write_bytes(b"\x00" * 100)
    with caplog.at_level(
        logging.INFO, logger="meeting_notetaker.transcription.worker",
    ):
        batch_transcribe(wav, model, source="mic")
    msg = _batch_summary_record(caplog)
    assert "vad_surv=n/a" in msg
    assert "segments=1" in msg


def test_batch_summary_log_subtracts_session_start_offset(tmp_path, caplog):
    """Span must be wav-local, not session-wide. session_start_offset
    is what the live-transcription continuation path passes in; the
    log should report timestamps relative to the wav file, not the
    cumulative session timeline."""
    model = _FakeModel(
        segments=[
            _Seg(start=0.0, end=10.0, text="first"),
            _Seg(start=20.0, end=30.0, text="second"),
        ],
        duration=40.0,
    )
    wav = tmp_path / "x.wav"
    wav.write_bytes(b"\x00" * 100)
    with caplog.at_level(
        logging.INFO, logger="meeting_notetaker.transcription.worker",
    ):
        batch_transcribe(
            wav, model, source="mic", session_start_offset=600.0,
        )
    msg = _batch_summary_record(caplog)
    # Wav-local: 0 to 30, NOT 600 to 630.
    assert "span=0.0-30.0s" in msg
