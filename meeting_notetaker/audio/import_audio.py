"""Decode an arbitrary audio (or video-with-audio) file into the
app-canonical mono int16 WAV (#88).

PyAV (libav* bindings) is already a dependency for the retain-audio
encode path and the highlights-export path. We reuse it here to
accept basically anything ffmpeg accepts: WAV, MP3, M4A / AAC,
OGG / Opus, FLAC, MP4 / MOV / WebM audio tracks, AMR.

Output shape matches what LoopbackRecorder / MicRecorder would have
written: 16 kHz mono int16 PCM in a WAV container. The batch
transcription + speaker refinement pipelines accept this shape
without further conversion.

Pure-Python module aside from the PyAV import; the resampler runs
on a worker thread in the controller path, with a progress callback
for the import dialog.
"""
from __future__ import annotations

import logging
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np


log = logging.getLogger(__name__)


# Output format: matches the in-app canonical so the batch + speaker
# pipelines accept the WAV without further conversion.
CANONICAL_SAMPLE_RATE = 16000
CANONICAL_CHANNELS = 1
CANONICAL_SAMPLE_WIDTH = 2  # int16

# Formats the file picker advertises. PyAV decodes more than this,
# but this is the user-visible filter set.
SUPPORTED_EXTENSIONS: tuple[str, ...] = (
    ".wav",
    ".mp3",
    ".m4a",
    ".aac",
    ".opus",
    ".ogg",
    ".oga",
    ".flac",
    ".mp4",
    ".mov",
    ".webm",
    ".amr",
)


class AudioImportError(RuntimeError):
    """Raised when the import pipeline can't make progress.

    Distinct from generic Exceptions so the dialog can catch and
    surface a user-readable message without swallowing programming
    errors.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class AudioImportResult:
    """Metadata returned to the dialog after a successful decode."""

    src_path: Path
    dst_path: Path
    src_codec: str
    src_sample_rate: int
    src_channels: int
    duration_seconds: float
    output_frames: int

    @property
    def duration_str(self) -> str:
        secs = int(self.duration_seconds)
        m, s = divmod(secs, 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"


def is_supported_extension(path: Path) -> bool:
    """True if the file extension is in our advertised allowlist."""
    return path.suffix.lower() in SUPPORTED_EXTENSIONS


def describe_source(src: Path) -> dict:
    """Quick metadata peek without decoding the full file.

    Used by the dialog to render the "Source: M4A, 32.6 MB, 14:22"
    line before the user clicks Import. Returns minimal info so the
    cost is bounded -- PyAV opens the container, reads the audio
    stream header, then closes. Sub-second on typical files.

    Returns {} on any failure -- the dialog falls back to extension-
    only description so the user can still proceed.
    """
    try:
        import av
    except ImportError:
        return {}
    try:
        with av.open(str(src)) as container:
            audio_streams = container.streams.audio
            if not audio_streams:
                return {"error": "no audio stream in file"}
            stream = audio_streams[0]
            duration_sec = 0.0
            if stream.duration is not None and stream.time_base is not None:
                duration_sec = float(stream.duration * stream.time_base)
            elif container.duration is not None:
                # Container-level duration is in AV_TIME_BASE (1/1e6).
                duration_sec = float(container.duration) / 1_000_000
            return {
                "codec": getattr(stream.codec_context, "name", "?"),
                "sample_rate": int(stream.codec_context.sample_rate or 0),
                "channels": int(stream.channels or 0),
                "duration_seconds": duration_sec,
                "file_size_bytes": src.stat().st_size,
                "audio_stream_count": len(audio_streams),
            }
    except Exception as exc:
        log.info("describe_source(%s) failed: %s", src, exc)
        return {"error": str(exc)}


def decode_to_canonical_wav(
    src_path: Path,
    dst_path: Path,
    *,
    target_rate: int = CANONICAL_SAMPLE_RATE,
    progress: Optional[Callable[[float], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> AudioImportResult:
    """Decode src_path -> mono 16k int16 WAV at dst_path.

    Walks the source via PyAV's `container.decode(stream)`, runs each
    frame through an `AudioResampler` configured for the canonical
    output shape (s16 / mono / target_rate), and appends the result
    to a wave-module writer in 4096-sample chunks. Progress is
    reported as a 0.0..1.0 fraction whenever a new resampled chunk
    arrives. Cancellation is checked between frames; on cancel the
    partial output WAV is removed before raising AudioImportError.

    Raises AudioImportError on missing file, missing audio stream,
    PyAV decode failure, or cancellation. The destination directory
    is created if missing; an existing dst_path is overwritten.
    """
    try:
        import av
    except ImportError as exc:
        raise AudioImportError(
            "PyAV is not installed; audio import requires it."
        ) from exc

    if not src_path.exists():
        raise AudioImportError(f"File not found: {src_path}")
    if src_path.is_dir():
        raise AudioImportError(f"Not a file: {src_path}")

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    if dst_path.exists():
        try:
            dst_path.unlink()
        except OSError as exc:
            raise AudioImportError(
                f"Could not overwrite destination {dst_path}: {exc}"
            ) from exc

    try:
        container = av.open(str(src_path))
    except Exception as exc:
        raise AudioImportError(
            f"Could not open {src_path.name} (unsupported format or "
            f"corrupted file): {exc}"
        ) from exc

    try:
        audio_streams = container.streams.audio
        if not audio_streams:
            raise AudioImportError(
                f"{src_path.name} has no audio stream. Video files without "
                "an audio track can't be imported."
            )
        stream = audio_streams[0]
        src_codec = getattr(stream.codec_context, "name", "?")
        src_sample_rate = int(stream.codec_context.sample_rate or 0)
        src_channels = int(stream.channels or 0)
        duration_seconds = 0.0
        if stream.duration is not None and stream.time_base is not None:
            duration_seconds = float(stream.duration * stream.time_base)
        elif container.duration is not None:
            duration_seconds = float(container.duration) / 1_000_000

        # Output: signed-16 mono at target_rate, packed format.
        resampler = av.AudioResampler(
            format="s16", layout="mono", rate=target_rate,
        )
        output_frames = 0
        wf = wave.open(str(dst_path), "wb")
        wf.setnchannels(CANONICAL_CHANNELS)
        wf.setsampwidth(CANONICAL_SAMPLE_WIDTH)
        wf.setframerate(target_rate)
        try:
            for frame in container.decode(stream):
                if should_cancel is not None and should_cancel():
                    raise AudioImportError("Import cancelled.")
                for rs in resampler.resample(frame):
                    samples = rs.to_ndarray().reshape(-1).astype(np.int16)
                    wf.writeframes(samples.tobytes())
                    output_frames += samples.size
                    if progress is not None and duration_seconds > 0:
                        decoded_sec = output_frames / target_rate
                        progress(min(1.0, decoded_sec / duration_seconds))
            # Flush the resampler at end-of-stream.
            for rs in resampler.resample(None) or []:
                samples = rs.to_ndarray().reshape(-1).astype(np.int16)
                wf.writeframes(samples.tobytes())
                output_frames += samples.size
        finally:
            wf.close()
    except AudioImportError:
        # Cancellation or our own explicit error: clean up partial WAV.
        try:
            if dst_path.exists():
                dst_path.unlink()
        except OSError:
            log.warning("could not remove partial WAV %s after cancel", dst_path)
        raise
    except Exception as exc:
        try:
            if dst_path.exists():
                dst_path.unlink()
        except OSError:
            pass
        raise AudioImportError(
            f"Decode failed: {exc}"
        ) from exc
    finally:
        container.close()

    if output_frames == 0:
        try:
            dst_path.unlink()
        except OSError:
            pass
        raise AudioImportError(
            f"{src_path.name} decoded to zero frames; the audio track "
            "may be empty."
        )

    if progress is not None:
        progress(1.0)

    return AudioImportResult(
        src_path=src_path,
        dst_path=dst_path,
        src_codec=src_codec,
        src_sample_rate=src_sample_rate,
        src_channels=src_channels,
        duration_seconds=duration_seconds,
        output_frames=output_frames,
    )
