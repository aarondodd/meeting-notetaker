"""Turn segmentation for the loopback audio channel.

Splits a recorded WAV into voiced spans separated by silences. Each span
is a candidate "turn" -- the same speaker is likely talking throughout.
The embedder downstream gets one embedding per turn; the clusterer
groups them into anonymous speakers.

Three-tier VAD fallback for the per-frame voiced/silent decision:

1. silero-vad (preferred). Small torch model bundled with the
   `silero-vad` PyPI package -- noticeably more accurate than
   webrtcvad on real meeting audio (HVAC noise, keyboard clatter,
   overlapping speech). Reuses the torch install pulled in by
   SpeechBrain, so the marginal cost is just ~2MB of bundled model
   weights. Loaded lazily so the import side of this module stays
   cheap.
2. webrtcvad (fallback). Still present so the segmenter works in
   environments where silero-vad or torch can't load (the bundled
   torch wheel can fail on very old CPUs; the webrtcvad path keeps
   the pipeline alive).
3. Energy-threshold mask (last resort). Fixed RMS threshold; used
   only when neither library is importable. Robust enough to keep
   `tests/test_diarization_segmenter.py` running in stripped CI envs
   without torch.

We previously tried an energy-percentile floor as the primary path;
it worked on artificial test signals but failed on real meeting audio
where voice dominates (the floor calibrates into the voiced range and
the mask collapses to all-silent). The newer VAD paths don't have
that calibration brittleness.

Input WAV can be any sample rate; we resample to 16kHz mono int16 in
memory before passing to the VAD. The returned `Turn.pcm` is also at
16kHz so the downstream embedder (which expects 16kHz) sees the same
data the VAD did.

The segmenter runs on the system-loopback channel, not the mic. The mic
is always the user, so it doesn't need diarization; clustering against
a known-self signal would only introduce noise.
"""
from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from ..audio.resample import resample_linear_int16, to_mono_int16


VAD_SAMPLE_RATE = 16000
VAD_FRAME_MS = 30
VAD_FRAME_SAMPLES = VAD_SAMPLE_RATE * VAD_FRAME_MS // 1000


@dataclass(frozen=True)
class Turn:
    """A contiguous span where someone (anyone) was talking.

    Times are seconds since the start of the audio file. `pcm` is the
    16kHz mono int16 samples for the turn. `sample_rate` is always
    `VAD_SAMPLE_RATE` -- callers don't need to inspect it, but we keep
    the field so the data is self-describing.
    """

    t_start: float
    t_end: float
    sample_rate: int
    pcm: np.ndarray

    @property
    def duration(self) -> float:
        return self.t_end - self.t_start


def read_wav_mono(wav_path: Path) -> tuple[np.ndarray, int]:
    """Load a WAV file, downmix to mono int16. Returns (pcm, sample_rate)."""
    with wave.open(str(wav_path), "rb") as wf:
        sr = wf.getframerate()
        nchan = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        nframes = wf.getnframes()
        raw = wf.readframes(nframes)
    if sampwidth != 2:
        raise ValueError(f"only 16-bit PCM supported, got {sampwidth * 8}-bit")
    pcm = np.frombuffer(raw, dtype=np.int16)
    if nchan > 1:
        pcm = to_mono_int16(pcm, channels=nchan)
    return pcm, sr


_silero_model = None
_silero_load_attempted = False


def _get_silero_model():
    """Lazy-init the silero VAD model. Returns None if unimportable.

    The model is small (~2MB) but holds torch state; caching it
    module-level keeps successive segmenter calls fast. A failed load
    is sticky so we don't pay the import cost on every retry; the
    failure path is the webrtcvad fallback, which is fine.
    """
    global _silero_model, _silero_load_attempted
    if _silero_load_attempted:
        return _silero_model
    _silero_load_attempted = True
    try:
        from silero_vad import load_silero_vad
        _silero_model = load_silero_vad()
    except Exception:
        _silero_model = None
    return _silero_model


def _voiced_mask_silero(
    pcm_16k: np.ndarray,
    *,
    threshold: float,
    min_silence_ms: int,
) -> np.ndarray:
    """Per-frame voiced bool mask via silero-vad on 30ms frames at 16kHz.

    silero returns sample-accurate speech timestamp ranges; we
    rasterize those onto our 30ms frame grid so the rest of the
    algorithm (span detection, bridging, min/max turn enforcement) is
    unchanged. Returns an empty array if silero or torch can't load.
    """
    model = _get_silero_model()
    if model is None:
        return np.zeros(0, dtype=bool)
    try:
        import torch
        from silero_vad import get_speech_timestamps
    except ImportError:
        return np.zeros(0, dtype=bool)

    n_frames = pcm_16k.size // VAD_FRAME_SAMPLES
    if n_frames == 0:
        return np.zeros(0, dtype=bool)
    # silero expects float32 in [-1, 1]; our buffers are int16.
    audio = torch.from_numpy(pcm_16k.astype(np.float32) / 32768.0)
    try:
        timestamps = get_speech_timestamps(
            audio,
            model,
            sampling_rate=VAD_SAMPLE_RATE,
            threshold=threshold,
            min_silence_duration_ms=min_silence_ms,
            # min_speech_duration_ms is intentionally short here -- the
            # find_turns layer enforces its own min_turn_sec, and a
            # short floor at the VAD layer keeps brief utterances from
            # being dropped before find_turns gets to see them.
            min_speech_duration_ms=100,
        )
    except Exception:
        return np.zeros(0, dtype=bool)

    mask = np.zeros(n_frames, dtype=bool)
    for span in timestamps:
        frame_start = max(0, span["start"] // VAD_FRAME_SAMPLES)
        # End is exclusive in our frame grid; round up so a span that
        # ends partway through a frame still marks that frame voiced.
        frame_end = min(
            n_frames,
            (span["end"] + VAD_FRAME_SAMPLES - 1) // VAD_FRAME_SAMPLES,
        )
        if frame_end > frame_start:
            mask[frame_start:frame_end] = True
    return mask


def _voiced_mask_webrtc(pcm_16k: np.ndarray, *, aggressiveness: int) -> np.ndarray:
    """Per-frame voiced bool mask using webrtcvad on 30ms frames at 16kHz.

    Returns an empty array if webrtcvad isn't importable -- the segmenter
    will then fall back to a relative-energy mask.
    """
    try:
        import webrtcvad
    except ImportError:
        return np.zeros(0, dtype=bool)
    vad = webrtcvad.Vad(aggressiveness)
    n_frames = pcm_16k.size // VAD_FRAME_SAMPLES
    if n_frames == 0:
        return np.zeros(0, dtype=bool)
    mask = np.zeros(n_frames, dtype=bool)
    for i in range(n_frames):
        frame = pcm_16k[i * VAD_FRAME_SAMPLES:(i + 1) * VAD_FRAME_SAMPLES].tobytes()
        mask[i] = vad.is_speech(frame, VAD_SAMPLE_RATE)
    return mask


def _voiced_mask_energy(pcm_16k: np.ndarray) -> np.ndarray:
    """Fallback: per-frame mask via absolute RMS-energy threshold.

    Used only when webrtcvad isn't importable. The fixed threshold
    (300 raw int16 RMS, roughly -40dBFS) catches normal speech levels
    and rejects digital silence. Less robust than webrtcvad but it
    keeps the segmenter usable in stripped test envs.
    """
    n_frames = pcm_16k.size // VAD_FRAME_SAMPLES
    if n_frames == 0:
        return np.zeros(0, dtype=bool)
    usable = n_frames * VAD_FRAME_SAMPLES
    frames = pcm_16k[:usable].astype(np.float32).reshape(-1, VAD_FRAME_SAMPLES)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    return rms >= 300.0


def find_turns(
    pcm: np.ndarray,
    sample_rate: int,
    *,
    aggressiveness: int = 2,
    min_turn_sec: float = 1.0,
    min_silence_sec: float = 0.5,
    max_turn_sec: float = 30.0,
    silero_threshold: float = 0.5,
) -> list[Turn]:
    """VAD-based turn segmentation. Returns one Turn per voiced span.

    Spans shorter than `min_turn_sec` are dropped (too short for a
    reliable embedding). Spans longer than `max_turn_sec` are
    subdivided so the embedder still gets a usable chunk for
    monologues. Silence shorter than `min_silence_sec` doesn't split a
    turn -- this is what keeps a single utterance from being chopped
    by brief breath pauses.

    Tries silero-vad first, then webrtcvad, then a fixed-threshold
    energy mask. `aggressiveness` only applies to the webrtcvad path;
    `silero_threshold` is the silero speech-probability cutoff (the
    silero default of 0.5 is well-calibrated for meeting audio).
    """
    if pcm.size == 0:
        return []
    # All downstream VAD paths run at 16kHz mono int16. Resample once.
    pcm_16k = (
        resample_linear_int16(pcm, src_rate=sample_rate, target_rate=VAD_SAMPLE_RATE)
        if sample_rate != VAD_SAMPLE_RATE
        else pcm.astype(np.int16, copy=False)
    )

    min_silence_ms = int(round(min_silence_sec * 1000.0))
    mask = _voiced_mask_silero(
        pcm_16k,
        threshold=silero_threshold,
        min_silence_ms=min_silence_ms,
    )
    if mask.size == 0:
        mask = _voiced_mask_webrtc(pcm_16k, aggressiveness=aggressiveness)
    if mask.size == 0:
        mask = _voiced_mask_energy(pcm_16k)
    if mask.size == 0:
        return []

    min_silence_frames = max(1, int(round(min_silence_sec * 1000.0 / VAD_FRAME_MS)))
    min_turn_frames = max(1, int(round(min_turn_sec * 1000.0 / VAD_FRAME_MS)))
    max_turn_frames = max(min_turn_frames, int(round(max_turn_sec * 1000.0 / VAD_FRAME_MS)))

    # Sweep voiced mask -> (start, end) spans, bridging silences shorter
    # than `min_silence_frames`.
    spans: list[tuple[int, int]] = []
    in_span = False
    span_start = 0
    silence_run = 0
    for i, voiced in enumerate(mask):
        if voiced:
            if not in_span:
                span_start = i
                in_span = True
            silence_run = 0
        else:
            if in_span:
                silence_run += 1
                if silence_run >= min_silence_frames:
                    spans.append((span_start, i - silence_run + 1))
                    in_span = False
                    silence_run = 0
    if in_span:
        spans.append((span_start, len(mask)))

    # Drop too-short spans; subdivide too-long ones.
    turns: list[Turn] = []
    for start_f, end_f in spans:
        if end_f - start_f < min_turn_frames:
            continue
        cursor = start_f
        while cursor < end_f:
            chunk_end = min(end_f, cursor + max_turn_frames)
            turns.append(_build_turn(pcm_16k, cursor, chunk_end))
            cursor = chunk_end
    return turns


def _build_turn(pcm_16k: np.ndarray, start_frame: int, end_frame: int) -> Turn:
    s = start_frame * VAD_FRAME_SAMPLES
    e = min(len(pcm_16k), end_frame * VAD_FRAME_SAMPLES)
    return Turn(
        t_start=s / VAD_SAMPLE_RATE,
        t_end=e / VAD_SAMPLE_RATE,
        sample_rate=VAD_SAMPLE_RATE,
        pcm=pcm_16k[s:e].copy(),
    )


def find_turns_in_wav(wav_path: Path, **kwargs) -> list[Turn]:
    """Read WAV + segment in one call."""
    pcm, sr = read_wav_mono(Path(wav_path))
    return find_turns(pcm, sr, **kwargs)
