"""Transcription workers.

Two patterns:

1. LiveTranscriptionWorker -- QThread that polls a ChunkBuffer per source,
   pops windows on a timer, runs faster-whisper, and emits chunk_done
   signals. Two workers (one per source) run in parallel.

2. batch_transcribe(wav_path, ...) -- standalone function for the
   record-then-transcribe path (capture-only mode + final pass on stop).
"""
from __future__ import annotations

import logging
import os
import queue
import tempfile
import threading
from pathlib import Path
from typing import Callable, Iterable, Optional

import numpy as np

from ..audio.chunk_buffer import ChunkBuffer, Window, write_window_wav
from ..models.transcript import TranscriptSegment


log = logging.getLogger(__name__)


try:
    from PyQt6.QtCore import QObject, QThread, pyqtSignal  # noqa: F401
    _HAS_QT = True
except ImportError:  # pragma: no cover -- pure-Python test envs
    _HAS_QT = False


# LiveTranscriptionWorker is Qt-dependent; we only define it when PyQt6 is
# importable. Pure-Python code paths (batch_transcribe, transcribe_pcm) work
# unconditionally so they can be unit-tested without Qt installed.
if _HAS_QT:  # pragma: no cover -- exercised in the Qt-enabled runtime
    class LiveTranscriptionWorker(QThread):
        """One worker per source. Pops windows, transcribes, emits segments."""

        chunk_done = pyqtSignal(object)  # TranscriptSegment
        progress = pyqtSignal(str)
        error = pyqtSignal(str)

        def __init__(
            self,
            chunk_buffer: ChunkBuffer,
            source: str,
            model,
            *,
            beam_size: int = 1,
            vad_filter: bool = True,
            vad_min_silence_ms: int = 500,
            language: str = "en",
            hotwords: str = "",
            poll_interval_sec: float = 1.0,
            parent: Optional[QObject] = None,
        ) -> None:
            super().__init__(parent)
            self.chunk_buffer = chunk_buffer
            self.source = source
            self.model = model
            self.beam_size = beam_size
            self.vad_filter = vad_filter
            self.vad_min_silence_ms = vad_min_silence_ms
            self.language = language
            self.hotwords = hotwords
            self.poll_interval_sec = poll_interval_sec
            self._stop = threading.Event()
            self._drain_on_stop = True

        def stop(self, drain: bool = True) -> None:
            self._drain_on_stop = drain
            self._stop.set()

        def run(self) -> None:
            try:
                while not self._stop.is_set():
                    window = self.chunk_buffer.pop_window(self.source)
                    if window is None:
                        self._stop.wait(self.poll_interval_sec)
                        continue
                    self._transcribe_and_emit(window)
                if self._drain_on_stop:
                    window = self.chunk_buffer.drain(self.source)
                    if window is not None:
                        self._transcribe_and_emit(window)
            except Exception as exc:  # pragma: no cover - worker safety net
                log.exception("transcription worker crashed: %s", self.source)
                self.error.emit(str(exc))

        def _transcribe_and_emit(self, window: Window) -> None:
            if len(window.pcm) == 0:
                return
            text = transcribe_pcm(
                window.pcm,
                self.model,
                beam_size=self.beam_size,
                vad_filter=self.vad_filter,
                vad_min_silence_ms=self.vad_min_silence_ms,
                language=self.language,
                hotwords=self.hotwords,
            )
            if not text:
                return
            seg = TranscriptSegment(
                source=window.source,
                text=text,
                t_start=window.t_start,
                t_end=window.t_end,
                is_provisional=False,
            )
            self.chunk_done.emit(seg)


def transcribe_pcm(
    pcm: np.ndarray,
    model,
    *,
    beam_size: int = 5,
    vad_filter: bool = True,
    vad_min_silence_ms: int = 500,
    language: str = "en",
    hotwords: str = "",
) -> str:
    """Transcribe a mono-16k int16 PCM array. Returns concatenated text (may be empty)."""
    fd, tmp_name = tempfile.mkstemp(suffix=".wav", prefix="mn_chunk_")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        write_window_wav(Window(source="_tmp", pcm=pcm, t_start=0.0, t_end=0.0), tmp_path)
        kwargs = dict(
            language=language,
            beam_size=beam_size,
            vad_filter=vad_filter,
            vad_parameters=dict(min_silence_duration_ms=vad_min_silence_ms),
        )
        if hotwords:
            kwargs["hotwords"] = hotwords
        segments, _info = model.transcribe(str(tmp_path), **kwargs)
        text = " ".join((seg.text or "").strip() for seg in segments).strip()
        return text
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def batch_transcribe(
    wav_path: Path,
    model,
    *,
    source: str,
    session_start_offset: float = 0.0,
    beam_size: int = 5,
    vad_filter: bool = True,
    vad_min_silence_ms: int = 500,
    language: str = "en",
    hotwords: str = "",
    progress: Optional[Callable[[str], None]] = None,
    on_progress_pct: Optional[Callable[[str, float], None]] = None,
) -> list[TranscriptSegment]:
    """Run faster-whisper over a complete WAV. Returns one segment per sentence-ish chunk.

    `on_progress_pct(source, pct)` is called after each yielded segment with
    `pct` in [0.0, 1.0] based on the segment's end time divided by the audio
    duration. This is meaningful only because faster-whisper's `transcribe()`
    yields segments lazily as it decodes, so the iterator advances roughly in
    proportion to wall time. Use this to drive a determinate progress UI.
    """
    if progress:
        progress(f"Transcribing {source}...")
    kwargs = dict(
        language=language,
        beam_size=beam_size,
        vad_filter=vad_filter,
        vad_parameters=dict(min_silence_duration_ms=vad_min_silence_ms),
    )
    if hotwords:
        kwargs["hotwords"] = hotwords
    segments_iter, info = model.transcribe(str(wav_path), **kwargs)
    duration = max(float(getattr(info, "duration", 0.0) or 0.0), 0.001)
    result: list[TranscriptSegment] = []
    for seg in segments_iter:
        text = (seg.text or "").strip()
        if text:
            result.append(
                TranscriptSegment(
                    source=source,
                    text=text,
                    t_start=session_start_offset + float(seg.start),
                    t_end=session_start_offset + float(seg.end),
                    is_provisional=False,
                )
            )
        if on_progress_pct is not None:
            pct = min(max(float(seg.end) / duration, 0.0), 1.0)
            on_progress_pct(source, pct)
    if on_progress_pct is not None:
        on_progress_pct(source, 1.0)
    if progress:
        progress(f"Transcribed {source}: {len(result)} segments")
    return result


def interleave(segments_a: Iterable[TranscriptSegment], segments_b: Iterable[TranscriptSegment]) -> list[TranscriptSegment]:
    """Merge two per-source segment lists into a single timeline sorted by t_start."""
    merged = list(segments_a) + list(segments_b)
    merged.sort(key=lambda s: (s.t_start, 0 if s.source == "mic" else 1))
    return merged
