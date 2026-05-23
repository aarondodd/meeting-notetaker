"""WAV-alignment helper.

After Stop, rewrite a WAV file with leading + trailing silence so its
content spans the recorder's full wall-clock window. This is the
load-bearing piece for keeping mic.wav and sys.wav aligned -- WASAPI
loopback often doesn't deliver its first sample until something
actually plays through the speakers (the Windows audio engine
doesn't wake up until a renderer is active), and can drop trailing
samples if the engine goes idle before stop.

Mic capture is generally continuous, so the leading + trailing pads
are typically zero on the mic side; for sys they can be several
seconds. Applying the same helper to both keeps the recorders
symmetric and avoids surprise drift if PyAudio also stutters.
"""
from __future__ import annotations

import logging
import wave
from pathlib import Path

log = logging.getLogger(__name__)


# Streaming I/O block size for the in-place rewrite. 16 K frames is
# 64 KB at 16-bit mono, 256 KB at 16-bit 8-channel; small enough to
# keep memory bounded but large enough that the file I/O dominates
# the per-block overhead.
_COPY_BLOCK_FRAMES = 16_384
_SILENCE_BLOCK_FRAMES = 8_192


def pad_wav(
    wav_path: Path,
    *,
    leading_frames: int,
    trailing_frames: int,
) -> None:
    """Rewrite wav_path with N leading + M trailing all-zero frames.

    No-op if both counts are <= 0. Streams in chunks so a 1-hour
    stereo 48k WAV (~700 MB) doesn't materialize in RAM. Writes to
    a sibling .tmp file then atomically replaces the original;
    falls back to deleting the .tmp on any failure so we don't
    leave a partial file alongside the real one.
    """
    if leading_frames <= 0 and trailing_frames <= 0:
        return
    if not wav_path.exists():
        return
    tmp_path = wav_path.with_suffix(wav_path.suffix + ".tmp")
    try:
        with wave.open(str(wav_path), "rb") as rf:
            channels = rf.getnchannels()
            sampwidth = rf.getsampwidth()
            framerate = rf.getframerate()
            n_frames = rf.getnframes()
            bytes_per_frame = channels * sampwidth
            with wave.open(str(tmp_path), "wb") as wf:
                wf.setnchannels(channels)
                wf.setsampwidth(sampwidth)
                wf.setframerate(framerate)
                if leading_frames > 0:
                    _write_silence(wf, leading_frames, bytes_per_frame)
                remaining = n_frames
                while remaining > 0:
                    to_read = min(_COPY_BLOCK_FRAMES, remaining)
                    data = rf.readframes(to_read)
                    if not data:
                        break
                    wf.writeframes(data)
                    remaining -= to_read
                if trailing_frames > 0:
                    _write_silence(wf, trailing_frames, bytes_per_frame)
        tmp_path.replace(wav_path)
    except Exception:
        log.exception("pad_wav: failed for %s", wav_path)
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def _write_silence(
    wf: wave.Wave_write, frames: int, bytes_per_frame: int,
) -> None:
    """Append `frames` of all-zero PCM to wf in chunks."""
    if frames <= 0:
        return
    block = b"\x00" * (_SILENCE_BLOCK_FRAMES * bytes_per_frame)
    remaining = frames
    while remaining >= _SILENCE_BLOCK_FRAMES:
        wf.writeframes(block)
        remaining -= _SILENCE_BLOCK_FRAMES
    if remaining > 0:
        wf.writeframes(b"\x00" * (remaining * bytes_per_frame))


def compute_pad_frames(
    *,
    start_wallclock: float,
    first_sample_wallclock: float | None,
    stop_wallclock: float,
    actual_frames: int,
    sample_rate: int,
    min_pad_frames: int = 50,
) -> tuple[int, int]:
    """Return (leading_frames, trailing_frames) for a recording.

    ``first_sample_wallclock`` is the monotonic timestamp of the
    recorder's first callback; None when no samples were ever
    delivered. ``actual_frames`` is the WAV's current frame count
    (callback writes only, no pads). ``sample_rate`` is the WAV's
    rate.

    Returns leading=0,trailing=0 when both deltas are below
    min_pad_frames (default 50 frames, about 1 ms at 48 kHz) so we
    don't churn the file for sub-perceptible jitter. Caller can pass
    min_pad_frames=0 to force always-pad.
    """
    if first_sample_wallclock is None:
        return 0, 0
    elapsed_seconds = max(0.0, stop_wallclock - start_wallclock)
    expected_total_frames = int(elapsed_seconds * sample_rate)
    leading_seconds = max(0.0, first_sample_wallclock - start_wallclock)
    leading_frames = int(leading_seconds * sample_rate)
    trailing_frames = max(
        0, expected_total_frames - leading_frames - actual_frames,
    )
    if leading_frames < min_pad_frames:
        leading_frames = 0
    if trailing_frames < min_pad_frames:
        trailing_frames = 0
    return leading_frames, trailing_frames
