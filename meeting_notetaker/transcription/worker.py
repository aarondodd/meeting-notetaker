"""Transcription workers.

Two patterns:

1. LiveTranscriptionWorker -- QThread that polls a ChunkBuffer per source,
   pops windows on a timer, runs faster-whisper, and emits chunk_done
   signals. Two workers (one per source) run in parallel.

2. batch_transcribe(wav_path, ...) -- standalone function for the
   record-then-transcribe path (capture-only mode + final pass on stop).
   Long recordings are sliced into overlapping chunks and transcribed
   piece by piece so the feature extractor's upfront STFT does not try
   to allocate a multi-GiB FFT buffer. See _should_chunk_long_audio.
"""
from __future__ import annotations

import logging
import os
import queue
import tempfile
import threading
import wave
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

import numpy as np

from ..audio.chunk_buffer import ChunkBuffer, Window, write_window_wav
from ..models.transcript import TranscriptSegment


log = logging.getLogger(__name__)


# Long-audio chunking. faster-whisper's transcribe() runs the Mel/STFT
# feature extractor over the WHOLE input before its 30s decoder loop:
# `features = self.feature_extractor(audio, chunk_length=chunk_length)`
# at faster_whisper/transcribe.py:916. The intermediate FFT array is
# complex64 at shape (1, frames, n_fft//2 + 1); at the default hop of
# 160 samples a 2.5h recording produces ~812k frames and asks for
# 1.22 GiB in one allocation. Any Windows build with a bit of memory
# pressure fails that allocation and the batch pass dies before a
# single segment comes back.
#
# Above LONG_AUDIO_THRESHOLD_S we decode the WAV once through PyAV
# into overlapping LONG_AUDIO_CHUNK_S slices and run transcribe() on
# each slice as a numpy array. Each slice's feature-extractor cost
# scales with the slice length, not the file length, so a 15 min
# window peaks around ~140 MiB regardless of how long the meeting is.
#
# The overlap exists so a word that happens to straddle a slice
# boundary is seen in full by at least one slice. Segments whose
# absolute start time falls inside the overlap are deduped by the
# midpoint rule in _batch_transcribe_chunked.
LONG_AUDIO_THRESHOLD_S = 1800.0
LONG_AUDIO_CHUNK_S = 900.0
LONG_AUDIO_OVERLAP_S = 4.0
_WHISPER_SAMPLE_RATE = 16000


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

    Long recordings route through _batch_transcribe_chunked which decodes
    and transcribes the WAV in overlapping LONG_AUDIO_CHUNK_S slices.
    Short recordings keep the direct path; the overhead of a single
    transcribe() call on a short file is already the bottleneck.
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
    duration_hint = _wav_duration_seconds(wav_path)
    if duration_hint is not None and duration_hint > LONG_AUDIO_THRESHOLD_S:
        log.info(
            "batch_transcribe %s: input is %.1fs (> %.1fs); using chunked path",
            source, duration_hint, LONG_AUDIO_THRESHOLD_S,
        )
        return _batch_transcribe_chunked(
            wav_path, model,
            source=source,
            total_duration=duration_hint,
            session_start_offset=session_start_offset,
            kwargs=kwargs,
            progress=progress,
            on_progress_pct=on_progress_pct,
        )
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
    # #118: post-batch diagnostics. Captures the input/output shape
    # so "only the first N min transcribed" reports can be self-
    # diagnosing from the runtime log alone (no session artifacts
    # required). One INFO line per source; the combined post-merge
    # summary lives in _BatchTranscribeThread.run.
    _log_batch_summary(source, info, duration, result, session_start_offset)
    return result


def _wav_duration_seconds(wav_path: Path) -> Optional[float]:
    """Return WAV duration in seconds, or None when the header can't be read.

    The chunking path only kicks in on a real WAV with a valid RIFF
    header. Anything else -- a non-WAV container, a stub file, the
    100-byte fake WAVs the unit tests write -- falls through to the
    legacy single-call path, which is what existing fake-model tests
    exercise.
    """
    try:
        with wave.open(str(wav_path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
    except (wave.Error, OSError, EOFError):
        return None
    if rate <= 0 or frames <= 0:
        return None
    return frames / float(rate)


def _iter_decoded_chunks(
    wav_path: Path,
    *,
    chunk_s: float,
    overlap_s: float,
    target_rate: int = _WHISPER_SAMPLE_RATE,
) -> Iterator[tuple[float, np.ndarray]]:
    """Walk the WAV through PyAV once; emit overlapping mono float32 slices.

    Yields `(chunk_start_s, audio_np)` pairs where `chunk_start_s` is
    the slice's position in the resampled timeline (0 for the first
    chunk, step_s for the second, and so on) and `audio_np` is 1D
    float32 in [-1, 1] at `target_rate`.

    Peak memory is roughly one chunk plus one overlap window. We never
    materialize the full file in memory, which is the whole point:
    `_decode_to_mono_float32` in audio/player.py does concatenate the
    full file and that is exactly what fails on long recordings.
    """
    import av  # noqa: PLC0415

    chunk_samples = int(chunk_s * target_rate)
    overlap_samples = int(overlap_s * target_rate)
    if chunk_samples <= 0:
        raise ValueError("chunk_s must be > 0")
    step_samples = max(chunk_samples - overlap_samples, 1)

    pending: list[np.ndarray] = []
    pending_len = 0
    emitted_samples = 0

    def _ingest(samples: np.ndarray) -> Iterator[tuple[float, np.ndarray]]:
        nonlocal pending, pending_len, emitted_samples
        pending.append(samples)
        pending_len += samples.shape[0]
        while pending_len >= chunk_samples:
            arr = np.concatenate(pending) if len(pending) > 1 else pending[0]
            chunk = arr[:chunk_samples]
            yield emitted_samples / target_rate, chunk
            tail = arr[step_samples:]
            pending = [tail] if tail.size else []
            pending_len = tail.shape[0]
            emitted_samples += step_samples

    with av.open(str(wav_path)) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(
            format="fltp", layout="mono", rate=target_rate,
        )
        for frame in container.decode(stream):
            for rs in resampler.resample(frame):
                samples = rs.to_ndarray().reshape(-1).astype(
                    np.float32, copy=False,
                )
                yield from _ingest(samples)
        for rs in resampler.resample(None) or []:
            samples = rs.to_ndarray().reshape(-1).astype(
                np.float32, copy=False,
            )
            yield from _ingest(samples)
    if pending_len > 0:
        arr = np.concatenate(pending) if len(pending) > 1 else pending[0]
        yield emitted_samples / target_rate, arr


def _batch_transcribe_chunked(
    wav_path: Path,
    model,
    *,
    source: str,
    total_duration: float,
    session_start_offset: float,
    kwargs: dict,
    progress: Optional[Callable[[str], None]],
    on_progress_pct: Optional[Callable[[str, float], None]],
) -> list[TranscriptSegment]:
    """Long-audio path for batch_transcribe.

    Walks the WAV in overlapping slices via _iter_decoded_chunks and
    calls `model.transcribe(audio_np, ...)` on each slice. Slice-local
    segment times are shifted by `chunk_start_s + session_start_offset`
    on the way out. Segments inside the overlap region are deduped by
    midpoint: everything up to the midpoint of this chunk's trailing
    overlap belongs to this chunk, everything from the midpoint on
    belongs to the next.
    """
    duration = max(total_duration, 0.001)
    overlap_half = LONG_AUDIO_OVERLAP_S / 2.0
    last_boundary_rel = 0.0  # latest accepted segment start in wav-local seconds
    # Overlap means chunk N+1's first segments cover time that already
    # ran through chunk N, so per-segment pct could regress across a
    # chunk boundary. Clamp monotonically: the progress bar never
    # slides backwards, even if the model does.
    max_pct_so_far = 0.0
    result: list[TranscriptSegment] = []
    total_vad_surv = 0.0
    saw_vad = False
    chunks_seen = 0

    for chunk_start, audio in _iter_decoded_chunks(
        wav_path,
        chunk_s=LONG_AUDIO_CHUNK_S,
        overlap_s=LONG_AUDIO_OVERLAP_S,
    ):
        chunks_seen += 1
        chunk_len_s = float(audio.shape[0]) / _WHISPER_SAMPLE_RATE
        chunk_end = chunk_start + chunk_len_s
        is_last_chunk = chunk_end >= duration - 1e-3
        # Everything up to this boundary (wav-local seconds) is this
        # chunk's responsibility. On the last chunk, take everything
        # left so a trailing word is never dropped.
        next_boundary = chunk_end if is_last_chunk else chunk_end - overlap_half
        segments_iter, info = model.transcribe(audio, **kwargs)
        vad_this_chunk = getattr(info, "duration_after_vad", None)
        if isinstance(vad_this_chunk, (int, float)):
            total_vad_surv += float(vad_this_chunk)
            saw_vad = True
        last_seg_end_abs = chunk_start
        for seg in segments_iter:
            seg_start_rel = chunk_start + float(seg.start)
            seg_end_rel = chunk_start + float(seg.end)
            last_seg_end_abs = seg_end_rel
            text = (seg.text or "").strip()
            keep = last_boundary_rel <= seg_start_rel < next_boundary
            if text and keep:
                result.append(
                    TranscriptSegment(
                        source=source,
                        text=text,
                        t_start=session_start_offset + seg_start_rel,
                        t_end=session_start_offset + seg_end_rel,
                        is_provisional=False,
                    )
                )
            if on_progress_pct is not None:
                pct = min(max(seg_end_rel / duration, 0.0), 1.0)
                if pct < max_pct_so_far:
                    pct = max_pct_so_far
                else:
                    max_pct_so_far = pct
                on_progress_pct(source, pct)
        last_boundary_rel = next_boundary
        if progress:
            progress(
                f"Transcribed {source} chunk "
                f"{chunks_seen} ({chunk_end:.0f}s / {duration:.0f}s)",
            )
        del audio  # free the slice before pulling the next one

    if on_progress_pct is not None:
        on_progress_pct(source, 1.0)
    if progress:
        progress(f"Transcribed {source}: {len(result)} segments")

    class _ChunkedInfo:
        pass

    synth_info = _ChunkedInfo()
    synth_info.duration = duration
    if saw_vad:
        synth_info.duration_after_vad = total_vad_surv
    log.info(
        "batch_transcribe %s chunked: chunks=%d total_duration=%.1fs",
        source, chunks_seen, duration,
    )
    _log_batch_summary(source, synth_info, duration, result, session_start_offset)
    return result


def _log_batch_summary(
    source: str,
    info,
    duration: float,
    segments: list[TranscriptSegment],
    session_start_offset: float,
) -> None:
    """Single INFO line summarizing what came out of a per-source
    faster-whisper transcribe call (#118).

    Fields:
      * ``input``    -- input audio duration (info.duration; what the
                        worker received off disk).
      * ``vad_surv`` -- duration after VAD silence removal, when
                        faster-whisper reports it (newer versions
                        expose ``info.duration_after_vad``; older
                        ones don't, in which case we log "n/a").
      * ``segments`` -- count of non-empty segments emitted.
      * ``speech``   -- summed segment duration (sum of seg.end -
                        seg.start). Approximates the actual speech
                        audio surviving VAD + transcription.
      * ``span``     -- first-segment start through last-segment end
                        in the original audio timeline (after
                        subtracting session_start_offset so the
                        numbers are wav-local, not session-wide).
      * ``chars``    -- total characters across all segment texts.
    """
    duration_after_vad = getattr(info, "duration_after_vad", None)
    if isinstance(duration_after_vad, (int, float)):
        vad_str = f"{float(duration_after_vad):.1f}s"
    else:
        vad_str = "n/a"
    if segments:
        first_t = segments[0].t_start - session_start_offset
        last_t = segments[-1].t_end - session_start_offset
        speech_dur = sum(s.t_end - s.t_start for s in segments)
        total_chars = sum(len(s.text) for s in segments)
        log.info(
            "batch_transcribe %s done: input=%.1fs vad_surv=%s "
            "segments=%d speech=%.1fs span=%.1f-%.1fs chars=%d",
            source, duration, vad_str,
            len(segments), speech_dur,
            first_t, last_t, total_chars,
        )
    else:
        log.info(
            "batch_transcribe %s done: input=%.1fs vad_surv=%s "
            "segments=0 (no speech transcribed)",
            source, duration, vad_str,
        )


def interleave(segments_a: Iterable[TranscriptSegment], segments_b: Iterable[TranscriptSegment]) -> list[TranscriptSegment]:
    """Merge two per-source segment lists into a single timeline sorted by t_start."""
    merged = list(segments_a) + list(segments_b)
    merged.sort(key=lambda s: (s.t_start, 0 if s.source == "mic" else 1))
    return merged
