"""Time-windowed PCM ring buffer + overlap-dedupe helper.

ChunkBuffer accumulates 16-bit mono PCM samples per source (mic, sys) at
16 kHz. A scheduler calls pop_window() at regular intervals to pull a window
ready for transcription. Successive windows overlap by WINDOW_OVERLAP_SEC
seconds so the downstream dedupe can stitch them.

dedupe_overlap() collapses the seam between successive Whisper outputs by
finding the longest suffix of chunk N's text that matches a prefix of chunk
N+1's text within a configurable lookahead window. On no match it returns
the new chunk unchanged -- loss-averse, since duplication is recoverable
but missing words are not.
"""
from __future__ import annotations

import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np


SAMPLE_RATE = 16000          # what Whisper consumes
SAMPLE_WIDTH = 2             # int16
WINDOW_SEC = 10.0
WINDOW_OVERLAP_SEC = 5.0


@dataclass
class Window:
    source: str
    pcm: np.ndarray           # int16 mono
    t_start: float            # seconds since session start
    t_end: float


class ChunkBuffer:
    """Per-source rolling buffer of int16 mono PCM at SAMPLE_RATE."""

    def __init__(
        self,
        sources: list[str],
        *,
        window_sec: float = WINDOW_SEC,
        overlap_sec: float = WINDOW_OVERLAP_SEC,
        sample_rate: int = SAMPLE_RATE,
    ) -> None:
        if overlap_sec >= window_sec:
            raise ValueError("overlap_sec must be < window_sec")
        self.sources = list(sources)
        self.window_sec = window_sec
        self.overlap_sec = overlap_sec
        self.sample_rate = sample_rate
        self._lock = threading.Lock()
        self._buffers: dict[str, np.ndarray] = {s: np.empty(0, dtype=np.int16) for s in sources}
        # head_t[s] = absolute time (seconds since session start) of buffers[s][0]
        self._head_t: dict[str, float] = {s: 0.0 for s in sources}
        # total seconds written per source (monotonic)
        self._written_t: dict[str, float] = {s: 0.0 for s in sources}

    @property
    def window_samples(self) -> int:
        return int(self.window_sec * self.sample_rate)

    @property
    def step_samples(self) -> int:
        return int((self.window_sec - self.overlap_sec) * self.sample_rate)

    def write(self, source: str, pcm: np.ndarray) -> None:
        if source not in self._buffers:
            raise KeyError(f"unknown source {source!r}; known: {self.sources}")
        if pcm.dtype != np.int16:
            pcm = pcm.astype(np.int16)
        if pcm.ndim != 1:
            pcm = pcm.reshape(-1)
        with self._lock:
            self._buffers[source] = np.concatenate([self._buffers[source], pcm])
            self._written_t[source] += len(pcm) / self.sample_rate

    def pop_window(self, source: str) -> Optional[Window]:
        """Return the next overlap-eligible window if enough samples are buffered, else None."""
        with self._lock:
            buf = self._buffers[source]
            window_n = self.window_samples
            step_n = self.step_samples
            if len(buf) < window_n:
                return None
            pcm = buf[:window_n].copy()
            t_start = self._head_t[source]
            t_end = t_start + window_n / self.sample_rate
            self._buffers[source] = buf[step_n:]
            self._head_t[source] = t_start + step_n / self.sample_rate
            return Window(source=source, pcm=pcm, t_start=t_start, t_end=t_end)

    def drain(self, source: str) -> Optional[Window]:
        """Return whatever remains as a final (possibly short) window."""
        with self._lock:
            buf = self._buffers[source]
            if len(buf) == 0:
                return None
            t_start = self._head_t[source]
            t_end = t_start + len(buf) / self.sample_rate
            window = Window(source=source, pcm=buf.copy(), t_start=t_start, t_end=t_end)
            self._buffers[source] = np.empty(0, dtype=np.int16)
            self._head_t[source] = t_end
            return window

    def written_seconds(self, source: str) -> float:
        with self._lock:
            return self._written_t[source]


def write_window_wav(window: Window, path: Path, *, sample_rate: int = SAMPLE_RATE) -> None:
    """Persist a Window to a 16-bit mono WAV at sample_rate."""
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(sample_rate)
        wf.writeframes(window.pcm.tobytes())


def _tokens(text: str) -> list[str]:
    return text.strip().split()


def dedupe_overlap(
    previous: str,
    current: str,
    *,
    min_match: int = 2,
    max_lookahead_tokens: int = 30,
) -> str:
    """Strip current's prefix that duplicates previous's tail.

    Looks for the longest suffix of `previous` that appears as a prefix of
    `current` (within max_lookahead_tokens). On match, returns current
    starting after the matched prefix. On no match, returns current
    unchanged.
    """
    prev = _tokens(previous)
    curr = _tokens(current)
    if not prev or not curr:
        return current.strip()
    max_n = min(len(prev), len(curr), max_lookahead_tokens)
    prev_lower = [t.lower() for t in prev]
    curr_lower = [t.lower() for t in curr]
    for n in range(max_n, min_match - 1, -1):
        if prev_lower[-n:] == curr_lower[:n]:
            return " ".join(curr[n:])
    return " ".join(curr)
