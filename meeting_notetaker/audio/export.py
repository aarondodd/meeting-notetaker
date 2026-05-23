"""Mix mic + sys into a single audio file in a user-chosen format.

The retained recording lives as two files on disk (mic.* and sys.*)
because keeping the streams separate preserves fidelity for any
future re-transcription / diarization pass. For sharing with a
colleague over email or chat, the user wants one playable file --
this module is the bridge.

Implementation:

1. Decode each source to float32 mono PCM at the target rate via
   PyAV's AudioResampler. Mic is naturally mono; sys is downmixed.
2. Pad the shorter buffer with silence so duration = max(both).
3. Sum the two and divide by 2 (clip-safe average mix). Wrap as a
   single AudioFrame (or chunked, for very long meetings) and feed
   the encoder for the user-chosen format.

This avoids PyAV's filter graph (amix) entirely; the numpy mix is
exact and the code path doesn't depend on the libavfilter API
shape, which churns release-to-release.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)


# Map filename extension -> (PyAV codec name, container format hint).
# m4a needs the 'ipod' muxer or PyAV writes a generic 'mov' that
# Windows Media Player doesn't recognize. opus needs the ogg
# container explicitly. wav and flac use the suffix's default.
_EXPORT_FORMATS: dict[str, tuple[str, Optional[str]]] = {
    ".flac": ("flac", None),
    ".opus": ("libopus", "ogg"),
    ".mp3":  ("libmp3lame", None),
    ".m4a":  ("aac", "ipod"),
    ".wav":  ("pcm_s16le", "wav"),
}

# Target the mixed output at 48k mono. Mono is the right choice for
# meeting audio (no spatial information to preserve) and 48k is the
# rate Opus prefers; for FLAC / MP3 / AAC it's well within their
# native operating range.
_TARGET_RATE = 48000
_TARGET_LAYOUT = "mono"

# Encode the mixed PCM in chunks. Avoids constructing one enormous
# AudioFrame whose internal copy could spike memory on a long meeting.
# 1 second of float32 mono at 48k is 192 KB; a 1-hour mix at this
# chunk size produces ~3600 small frames, trivial overhead.
_FRAMES_PER_CHUNK = _TARGET_RATE  # 1 sec


def export_mixed(
    mic_path: Optional[Path],
    sys_path: Optional[Path],
    dst_path: Path,
) -> None:
    """Mix the two source files into dst_path; format inferred from suffix.

    Raises ValueError on an unknown / unsupported destination suffix
    or when both sources are missing. av.AVError on a codec / muxer
    failure, OSError on file-IO failure.
    """
    suffix = dst_path.suffix.lower()
    if suffix not in _EXPORT_FORMATS:
        raise ValueError(
            f"unsupported export format {suffix!r}; "
            f"choose one of {sorted(_EXPORT_FORMATS)}"
        )
    codec_name, container_format = _EXPORT_FORMATS[suffix]

    sources = [
        p for p in (mic_path, sys_path)
        if p is not None and p.exists() and p.stat().st_size > 0
    ]
    if not sources:
        raise ValueError("no source audio files available to mix")

    # Decode each source to float32 mono at the target rate. Both
    # come back as 1-D ndarrays whose length is the frame count.
    decoded = [_decode_to_mono_float32(p, _TARGET_RATE) for p in sources]
    if len(decoded) == 1:
        mixed = decoded[0]
    else:
        mixed = _mix_to_max_length(decoded)

    _encode_mono_float32(
        mixed,
        dst_path=dst_path,
        codec_name=codec_name,
        container_format=container_format,
    )


def _decode_to_mono_float32(src: Path, target_rate: int) -> np.ndarray:
    """Walk src through PyAV + AudioResampler, return float32 mono samples."""
    import av  # noqa: PLC0415

    chunks: list[np.ndarray] = []
    with av.open(str(src)) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(
            format="fltp", layout=_TARGET_LAYOUT, rate=target_rate,
        )
        for frame in container.decode(stream):
            for rs in resampler.resample(frame):
                chunks.append(rs.to_ndarray().reshape(-1).astype(np.float32))
        # Flush.
        for rs in resampler.resample(None) or []:
            chunks.append(rs.to_ndarray().reshape(-1).astype(np.float32))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks)


def _mix_to_max_length(buffers: list[np.ndarray]) -> np.ndarray:
    """Sum 2+ mono float32 buffers, padded to the longest, attenuated.

    Divide by len(buffers) so the merged signal stays bounded by
    [-1, 1] when each input is already inside that range. PyAV's
    fltp samples are exactly in that range, so the result is
    clip-free without additional limiting.
    """
    max_len = max(b.size for b in buffers)
    acc = np.zeros(max_len, dtype=np.float32)
    for b in buffers:
        if b.size < max_len:
            pad = np.zeros(max_len, dtype=np.float32)
            pad[: b.size] = b
            acc += pad
        else:
            acc += b
    return acc / len(buffers)


def _encode_mono_float32(
    samples: np.ndarray,
    *,
    dst_path: Path,
    codec_name: str,
    container_format: Optional[str],
) -> None:
    """Stream samples through the chosen codec, write to dst_path."""
    import av  # noqa: PLC0415
    from av.audio.frame import AudioFrame  # noqa: PLC0415

    try:
        out_container = av.open(
            str(dst_path),
            mode="w",
            format=container_format,
        )
    except Exception:
        log.exception("export_mixed: failed to open container %s", dst_path)
        raise
    try:
        stream = out_container.add_stream(codec_name, rate=_TARGET_RATE)
        stream.layout = _TARGET_LAYOUT
        # Speech-grade bitrates for lossy codecs; lossless ignore.
        if codec_name == "libopus":
            stream.bit_rate = 32_000
        elif codec_name == "libmp3lame":
            stream.bit_rate = 96_000
        elif codec_name == "aac":
            stream.bit_rate = 64_000

        try:
            for i in range(0, samples.size, _FRAMES_PER_CHUNK):
                chunk = samples[i : i + _FRAMES_PER_CHUNK]
                frame = AudioFrame.from_ndarray(
                    chunk.reshape(1, -1), format="flt", layout=_TARGET_LAYOUT,
                )
                frame.rate = _TARGET_RATE
                # pts in stream time-base. Each chunk is _FRAMES_PER_CHUNK
                # samples long at _TARGET_RATE; the encoder's stream
                # time_base is 1/rate after add_stream.
                frame.pts = i
                for packet in stream.encode(frame):
                    out_container.mux(packet)
            for packet in stream.encode(None):
                out_container.mux(packet)
        except Exception:
            log.exception("export_mixed: encode failed -> %s", dst_path)
            raise
    finally:
        out_container.close()


def known_extensions() -> tuple[str, ...]:
    """Return supported export-file extensions, with the leading dot.

    Stable order: lossless first, then descending compatibility. The
    save dialog uses this to build its filter strings.
    """
    return (".flac", ".mp3", ".m4a", ".opus", ".wav")
