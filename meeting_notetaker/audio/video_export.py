"""Export a session as an MP4 slideshow video with mixed audio.

Combines three sources into a single playable file:

* The mixed mic + sys audio (same code path as audio/export.py).
* A H.264 slideshow video that shows the screenshot whose
  recording-relative offset most recently passed the current frame's
  time; black frame while playback precedes the first screenshot.
* A SubRip (SRT) sidecar file built from the transcript's
  [HH:MM:SS] timestamps. Sidecar instead of an embedded subtitle
  track so the user can toggle subs on or off in their player
  (or delete the SRT) without re-rendering.

Output: H.264 video + AAC mono audio in an MP4 container at
1920x1080 / 30 fps, plus a same-named `.srt` file.

Heavy lifting is done by PyAV (already in the bundle). Memory
ceiling for a 1-hour meeting is ~1 GB total (cached scaled
screenshots + decoded audio buffer); each frame's video encode
streams through libx264 frame-by-frame.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .export import (
    _decode_to_mono_float32 as _decode_audio_to_mono,
    _mix_to_max_length,
)
from ..screencap.timestamps import current_screenshot_for_position

log = logging.getLogger(__name__)


_TARGET_WIDTH = 1920
_TARGET_HEIGHT = 1080
_TARGET_FPS = 30
_TARGET_AUDIO_RATE = 48_000

# Cap subtitle cue duration so an inaudible-but-on-screen line
# doesn't sit there forever. The transcript itself will move on
# at the next line's timestamp; this is the upper bound for the
# final line of a meeting (where there's no "next" cue).
_MAX_CUE_SECONDS = 8.0


# Phase budget (cumulative percent) reserved by export_video so the
# overall progress bar doesn't hit 100% before audio mux + close
# actually finish. Frame encode is the dominant phase (issue #52).
_FRAME_PHASE_END = 85
_AUDIO_PHASE_END = 92
# (close is the rest, 92..100)


# Quality presets (issue #54). The user-facing setting maps to a
# (video_bit_rate, audio_bit_rate) tuple here; export_video honors
# whichever the caller passes via the `quality` kwarg. The bundled
# names are the keys the Settings dialog exposes; unknown values
# fall back to "medium".
QUALITY_PRESETS: dict[str, tuple[int, int]] = {
    "low":    (   600_000,  64_000),
    "medium": ( 1_500_000,  96_000),
    "high":   ( 2_500_000, 128_000),
}
DEFAULT_QUALITY = "medium"


def _resolve_quality(quality: Optional[str]) -> tuple[int, int]:
    """Return (video_bps, audio_bps) for the given preset name."""
    return QUALITY_PRESETS.get(
        (quality or DEFAULT_QUALITY).lower(), QUALITY_PRESETS[DEFAULT_QUALITY],
    )


def export_video(
    mic_path: Optional[Path],
    sys_path: Optional[Path],
    screenshots: list[tuple[Path, int]],   # (path, offset_ms), sorted ascending
    transcript_text: str,
    dst_path: Path,
    *,
    progress: Optional[Callable[[int], None]] = None,
    status: Optional[Callable[[str], None]] = None,
    quality: Optional[str] = None,
) -> None:
    """Render the session into an MP4 slideshow video at dst_path.

    ``screenshots`` is the [(path, offset_ms), ...] list from
    ``screencap.timestamps.screenshot_offsets``, sorted ascending by
    offset. ``transcript_text`` is the raw transcript content
    (line-per-segment with `[HH:MM:SS]` prefixes); empty / None
    skips subtitle generation.

    ``progress`` is an optional callback that receives an integer
    0-100 percent value while encoding. Useful for a QProgressDialog
    in the GUI.

    Raises ValueError on no source audio. Raises av.AVError /
    OSError on encoder / I/O failure.
    """
    sources = [
        p for p in (mic_path, sys_path)
        if p is not None and p.exists() and p.stat().st_size > 0
    ]
    if not sources:
        raise ValueError("no source audio files available to render")

    log.info(
        "export_video: starting -- screenshots=%d audio_sources=%d dst=%s",
        len(screenshots), len(sources), dst_path,
    )

    video_bps, audio_bps = _resolve_quality(quality)

    if status is not None:
        status("Decoding audio")
    # ----- decode + mix audio --------------------------------------
    decoded = [_decode_audio_to_mono(p, _TARGET_AUDIO_RATE) for p in sources]
    mixed = _mix_to_max_length(decoded).astype(np.float32, copy=False)
    np.clip(mixed, -1.0, 1.0, out=mixed)
    audio_duration_s = mixed.size / _TARGET_AUDIO_RATE

    # ----- compute total video duration ----------------------------
    last_screenshot_offset_ms = (
        screenshots[-1][1] if screenshots else 0
    )
    total_duration_s = max(
        audio_duration_s, last_screenshot_offset_ms / 1000.0, 1.0,
    )
    total_video_frames = int(total_duration_s * _TARGET_FPS)
    log.info(
        "export_video: duration=%.2f s (audio=%.2f s, last screenshot=%.2f s), "
        "total_video_frames=%d",
        total_duration_s, audio_duration_s,
        last_screenshot_offset_ms / 1000.0, total_video_frames,
    )

    # ----- write the SRT sidecar -----------------------------------
    srt_path = dst_path.with_suffix(".srt")
    if transcript_text:
        try:
            srt_path.write_text(
                _transcript_to_srt(transcript_text),
                encoding="utf-8",
            )
            log.info("export_video: wrote %s", srt_path)
        except Exception:
            log.exception("export_video: failed to write SRT sidecar")

    # ----- render the MP4 ------------------------------------------
    import av  # noqa: PLC0415

    out_container = None
    try:
        out_container = av.open(str(dst_path), mode="w", format="mp4")
        v_stream = out_container.add_stream("libx264", rate=_TARGET_FPS)
        v_stream.width = _TARGET_WIDTH
        v_stream.height = _TARGET_HEIGHT
        v_stream.pix_fmt = "yuv420p"
        v_stream.bit_rate = video_bps
        # H.264 baseline-ish: GOP every 2 s keeps seek-to-here in
        # players cheap without bloating the file.
        v_stream.codec_context.gop_size = _TARGET_FPS * 2

        a_stream = out_container.add_stream("aac", rate=_TARGET_AUDIO_RATE)
        a_stream.layout = "mono"
        a_stream.bit_rate = audio_bps

        if status is not None:
            status("Encoding video frames")
        _encode_video(
            out_container, v_stream, screenshots,
            total_video_frames, progress=progress,
            progress_end=_FRAME_PHASE_END,
        )
        if status is not None:
            status("Encoding audio")
        _encode_audio(
            out_container, a_stream, mixed, progress=progress,
            progress_start=_FRAME_PHASE_END, progress_end=_AUDIO_PHASE_END,
        )
        if status is not None:
            status("Finalizing container")
        out_container.close()
        out_container = None
        if progress is not None:
            progress(100)
    except Exception:
        log.exception("export_video: render failed for %s", dst_path)
        if out_container is not None:
            try:
                out_container.close()
            except Exception:
                pass
            out_container = None
        if dst_path.exists():
            try:
                dst_path.unlink()
            except OSError:
                pass
        raise
    finally:
        if out_container is not None:
            try:
                out_container.close()
            except Exception:
                pass


def _encode_video(
    out_container, v_stream,
    screenshots: list[tuple[Path, int]],
    total_video_frames: int,
    *,
    progress: Optional[Callable[[int], None]],
    progress_end: int = 100,
) -> None:
    """Frame-by-frame slideshow encode driven by sticky-latest lookup.

    Each frame's timestamp picks the screenshot whose offset is the
    greatest <= that timestamp; a None result means "no screenshot
    has appeared yet, show black". Scaled-to-1080p RGB arrays are
    cached per source so a single screenshot occupying many frames
    only pays the scale cost once.
    """
    import av  # noqa: PLC0415

    black_frame = np.zeros(
        (_TARGET_HEIGHT, _TARGET_WIDTH, 3), dtype=np.uint8,
    )
    scaled_cache: dict[Path, np.ndarray] = {}
    last_path: Optional[Path] = None
    last_array: np.ndarray = black_frame
    last_progress_pct = -1

    for frame_idx in range(total_video_frames):
        t_ms = int(frame_idx * 1000 / _TARGET_FPS)
        current_path = current_screenshot_for_position(screenshots, t_ms)
        if current_path != last_path:
            if current_path is None:
                last_array = black_frame
            else:
                if current_path not in scaled_cache:
                    scaled_cache[current_path] = _scale_screenshot_letterbox(
                        current_path,
                    )
                last_array = scaled_cache[current_path]
            last_path = current_path

        av_frame = av.VideoFrame.from_ndarray(last_array, format="rgb24")
        av_frame.pts = frame_idx
        # The stream's time_base is 1/rate after add_stream, so pts in
        # frames maps to seconds correctly. (PyAV sets v_stream's
        # time_base to 1/rate automatically when we set rate at
        # add_stream time.)
        for packet in v_stream.encode(av_frame):
            out_container.mux(packet)

        if progress is not None and total_video_frames > 0:
            pct = int(frame_idx * progress_end / total_video_frames)
            if pct != last_progress_pct:
                progress(pct)
                last_progress_pct = pct

    # Flush the video encoder.
    for packet in v_stream.encode(None):
        out_container.mux(packet)
    if progress is not None:
        progress(progress_end)


def _encode_audio(
    out_container, a_stream, mixed: np.ndarray,
    *,
    progress: Optional[Callable[[int], None]] = None,
    progress_start: int = 0,
    progress_end: int = 100,
) -> None:
    """Chunk the mixed float32 mono buffer through AAC -> MP4.

    Optional progress reporting maps the input-buffer position to the
    [progress_start, progress_end] range so the orchestrator's bar
    keeps moving during the audio mux pass (issue #52). Without this,
    the bar froze at the end of the video frame loop while the audio
    pass and libx264 finalize ran silently.
    """
    import av  # noqa: PLC0415
    from av.audio.frame import AudioFrame  # noqa: PLC0415

    chunk_frames = _TARGET_AUDIO_RATE  # 1 sec per chunk
    total = max(1, mixed.size)
    span = max(0, progress_end - progress_start)
    last_pct = -1
    for i in range(0, mixed.size, chunk_frames):
        chunk = mixed[i : i + chunk_frames]
        frame = AudioFrame.from_ndarray(
            chunk.reshape(1, -1), format="flt", layout="mono",
        )
        frame.rate = _TARGET_AUDIO_RATE
        frame.pts = i
        for packet in a_stream.encode(frame):
            out_container.mux(packet)
        if progress is not None and span > 0:
            pct = progress_start + int(i * span / total)
            if pct != last_pct:
                progress(pct)
                last_pct = pct
    for packet in a_stream.encode(None):
        out_container.mux(packet)
    if progress is not None:
        progress(progress_end)


def _scale_screenshot_letterbox(path: Path) -> np.ndarray:
    """Load + scale a screenshot to 1080p with letterbox bars.

    Returns an (H, W, 3) uint8 RGB array sized exactly
    (TARGET_HEIGHT, TARGET_WIDTH). Preserves the source's aspect
    ratio; unused pixels (top/bottom or left/right) are filled
    black.
    """
    from PIL import Image  # noqa: PLC0415

    canvas = Image.new("RGB", (_TARGET_WIDTH, _TARGET_HEIGHT), (0, 0, 0))
    with Image.open(str(path)) as img:
        img = img.convert("RGB")
        # Fit the image inside the canvas while keeping aspect ratio.
        # PIL's thumbnail mutates in-place but only ever shrinks; we
        # need to be able to scale up too, so compute the ratio
        # explicitly.
        src_w, src_h = img.size
        scale = min(_TARGET_WIDTH / src_w, _TARGET_HEIGHT / src_h)
        new_w = max(1, int(src_w * scale))
        new_h = max(1, int(src_h * scale))
        resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        offset_x = (_TARGET_WIDTH - new_w) // 2
        offset_y = (_TARGET_HEIGHT - new_h) // 2
        canvas.paste(resized, (offset_x, offset_y))
    return np.asarray(canvas, dtype=np.uint8)


_TIMESTAMP_RE = re.compile(r"^\[(\d+):(\d{2}):(\d{2})\]\s*(.*)$")


def _transcript_to_srt(transcript: str) -> str:
    """Convert the app's `[HH:MM:SS] Speaker: text` format to SRT.

    Each line becomes a cue starting at its timestamp and ending at
    the next line's timestamp (or +_MAX_CUE_SECONDS for the final
    line). Lines without a leading timestamp are skipped (status
    lines, blanks). SRT cue indices start at 1.
    """
    cues: list[tuple[int, int, str]] = []  # start_ms, end_ms, text
    for line in transcript.splitlines():
        match = _TIMESTAMP_RE.match(line)
        if match is None:
            continue
        hours, minutes, seconds, text = match.groups()
        start_ms = ((int(hours) * 3600) + (int(minutes) * 60) + int(seconds)) * 1000
        cues.append((start_ms, 0, text.strip() if text else ""))
    # Fill end_ms = next cue's start, capped by max-cue duration.
    for i, (start, _, text) in enumerate(cues):
        if i + 1 < len(cues):
            next_start = cues[i + 1][0]
            end = min(next_start, start + int(_MAX_CUE_SECONDS * 1000))
        else:
            end = start + int(_MAX_CUE_SECONDS * 1000)
        cues[i] = (start, end, text)
    parts: list[str] = []
    for idx, (start, end, text) in enumerate(cues, start=1):
        parts.append(str(idx))
        parts.append(f"{_ms_to_srt(start)} --> {_ms_to_srt(end)}")
        parts.append(text)
        parts.append("")
    return "\n".join(parts)


def _ms_to_srt(ms: int) -> str:
    """SRT-flavored hh:mm:ss,SSS."""
    h = ms // 3_600_000
    m = (ms // 60_000) % 60
    s = (ms // 1_000) % 60
    msec = ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{msec:03d}"
