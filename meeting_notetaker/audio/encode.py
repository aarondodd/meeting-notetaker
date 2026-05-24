"""Re-encode raw PCM WAVs to a compressed format for retained sessions.

The recorder writes 16-bit int PCM to ``mic.wav`` / ``sys.wav`` as the
session's source of truth. The batch transcription pass consumes those
WAVs at full quality. After both passes finish, if the user asked us
to keep the recording, we re-encode the WAVs to the configured
compressed format (Opus or FLAC) and delete the WAVs.

Opus at speech-grade (~32 kbps mono / 64 kbps stereo) gives roughly a
24x size reduction over WAV for typical meeting audio with no
practically meaningful quality loss for ASR or human listening. FLAC
preserves the exact PCM round-trip at roughly 2x reduction. WAV is
preserved by skipping this module entirely (the controller branches on
the config before calling).

Implementation uses PyAV (libav/ffmpeg bindings) which is already a
transitive dependency of faster-whisper, so this adds no new wheels to
the frozen .exe. PyAV's AudioResampler handles the 44.1 kHz -> 48 kHz
conversion Opus requires.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# Sample rates Opus accepts natively. Anything else has to be resampled
# to 48000 (the highest supported rate, the codec's native processing
# rate) before encoding.
_OPUS_NATIVE_RATES = frozenset({8000, 12000, 16000, 24000, 48000})

# Per-channel target bitrate for Opus. Speech-grade defaults: 32 kbps
# mono is near-transparent for spoken word; stereo gets 2x (libopus
# allocates the second channel intelligently).
_OPUS_BITRATE_PER_CHANNEL = 32000


def encode_audio(src_wav: Path, dst: Path, fmt: str) -> None:
    """Re-encode ``src_wav`` to ``dst`` using ``fmt`` ('opus' or 'flac').

    Raises ValueError for an unknown format string. Raises av.AVError or
    OSError on encoder / I/O failure -- the caller is expected to log
    and fall back to keeping the WAV in place.
    """
    if fmt not in ("opus", "flac"):
        raise ValueError(f"unknown retain_format: {fmt!r}")

    # Import inside the function so the module imports cheaply when av
    # isn't installed in the runtime (e.g. a dev venv without
    # faster-whisper). The controller catches the ImportError and falls
    # back to keeping the WAV.
    import av  # noqa: PLC0415

    with av.open(str(src_wav)) as in_container:
        in_stream = in_container.streams.audio[0]
        src_rate = int(in_stream.rate)
        src_channels = int(in_stream.channels)

        if fmt == "opus":
            out_rate = 48000 if src_rate not in _OPUS_NATIVE_RATES else src_rate
            target_bitrate = _OPUS_BITRATE_PER_CHANNEL * src_channels
        else:  # flac
            out_rate = src_rate
            target_bitrate = None

        # Opus supports up to 8 channels but our recorder only ever
        # writes mono (mic) or stereo (loopback). Match the channel
        # layout the source declared.
        layout = "mono" if src_channels == 1 else "stereo"
        resampler = None
        if out_rate != src_rate:
            resampler = av.AudioResampler(
                format="s16",
                layout=layout,
                rate=out_rate,
            )

        with av.open(str(dst), mode="w") as out_container:
            codec_name = "libopus" if fmt == "opus" else "flac"
            out_stream = out_container.add_stream(codec_name, rate=out_rate)
            out_stream.layout = layout
            if target_bitrate is not None:
                out_stream.bit_rate = target_bitrate
            try:
                for frame in in_container.decode(in_stream):
                    frames = [frame] if resampler is None else resampler.resample(frame)
                    for f in frames:
                        for packet in out_stream.encode(f):
                            out_container.mux(packet)
                # Flush in the right order: resampler first (it can
                # hold a tail of samples back), then encoder. Reversing
                # this order closes the encoder before the resampler's
                # trailing frames arrive and PyAV raises EOFError.
                if resampler is not None:
                    for frame in resampler.resample(None) or []:
                        for packet in out_stream.encode(frame):
                            out_container.mux(packet)
                # Encoder flush: forgetting this leaves the final ~20 ms
                # of audio out of the file.
                for packet in out_stream.encode(None):
                    out_container.mux(packet)
            except Exception:
                log.exception("encode_audio: codec error src=%s dst=%s", src_wav, dst)
                raise


def extension_for(fmt: str) -> str:
    """Return the filename extension to use for a given retain_format.

    Centralized so the controller and the path helpers agree on the
    suffix. The Opus container is Ogg; .opus is the convention modern
    players (VLC, Windows Media Player on Win11) handle as Ogg/Opus.
    """
    return {
        "opus": ".opus",
        "flac": ".flac",
        "wav": ".wav",
    }.get(fmt, ".wav")


def encode_pair(
    mic_wav: Path,
    sys_wav: Path,
    fmt: str,
    *,
    delete_source: bool = True,
) -> tuple[Optional[Path], Optional[Path]]:
    """Re-encode mic + system WAVs to ``fmt`` side-by-side; delete sources.

    Either WAV may be absent (mic-only or system-only session); that
    side is skipped silently. Returns the pair of output paths (None
    for any side that was skipped).

    On encoder failure for a given side, the source WAV is preserved
    and that side's output is None. The other side is still attempted.
    """
    ext = extension_for(fmt)
    if ext == ".wav":
        # No-op format. Callers should ideally not even invoke us here.
        return (mic_wav if mic_wav.exists() else None,
                sys_wav if sys_wav.exists() else None)

    outputs: list[Optional[Path]] = []
    for src in (mic_wav, sys_wav):
        if not src.exists() or src.stat().st_size == 0:
            outputs.append(None)
            continue
        dst = src.with_suffix(ext)
        try:
            encode_audio(src, dst, fmt)
        except Exception:
            log.exception("encode_pair: failed to encode %s", src)
            # Clean up a half-written output if one was produced.
            if dst.exists():
                try:
                    dst.unlink()
                except OSError:
                    pass
            outputs.append(None)
            continue
        if delete_source:
            try:
                src.unlink()
            except OSError:
                log.exception("encode_pair: could not delete source %s", src)
        outputs.append(dst)
    return outputs[0], outputs[1]
