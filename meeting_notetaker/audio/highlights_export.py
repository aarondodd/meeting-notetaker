"""Highlights-only audio/video export (Issue #26).

Concatenates the user-marked highlight ranges into a single playable
file with title + jump interstitials separating them. Two output
modes:

* `export_highlights_audio` -- single-file mixed mic+sys audio in
  the format the user picks (mp3 / flac / aac / opus / wav). Each
  highlight is preceded by a short configurable silent gap; no
  text interstitial because audio-only playback has no surface to
  show it on.
* `export_highlights_video` -- MP4 slideshow with text
  interstitials between highlights (the issue's primary export
  shape). 2-second title card per highlight + 2-second jump card
  between consecutive highlights, plus an aligned SRT sidecar.

Both paths share a planner (`plan_highlight_timeline`) that maps
each segment of the output to its source range. The planner is the
load-bearing piece; tests pin its math so encoder swaps don't
quietly shift segment boundaries.

Most of the heavy work is delegated to the existing audio/export
(audio mixing) and audio/video_export (frame encoding) modules.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional

import numpy as np

from ..models.highlights import Highlight

log = logging.getLogger(__name__)


# Two-second interstitials -- the issue specifies "2 second image"
# for both title and jump cards. Tunable per export call if a
# future need surfaces (a longer card for accessibility, say).
DEFAULT_TITLE_INTERSTITIAL_MS = 2_000
DEFAULT_JUMP_INTERSTITIAL_MS = 2_000

# Audio-mode silent gap between concatenated highlights. Short so
# the listener notices the cut without losing flow.
DEFAULT_AUDIO_GAP_MS = 500


SEGMENT_TITLE = "title"
SEGMENT_HIGHLIGHT = "highlight"
SEGMENT_JUMP = "jump"


@dataclass(frozen=True)
class TimelineSegment:
    """One segment in the concatenated output.

    For SEGMENT_HIGHLIGHT, (source_start_ms, source_end_ms) point at
    the source recording's timeline so the encoder knows what to
    decode. For interstitial segments those fields are 0.

    `output_start_ms` is the segment's start position in the
    concatenated output -- the encoder uses it to compute SRT cue
    timings and frame indices.

    `label` carries the interstitial text (e.g. "Highlight 1" or
    "Jumping to 00:30:15") for SEGMENT_TITLE / SEGMENT_JUMP, or
    empty string for SEGMENT_HIGHLIGHT.

    `source_highlight_index` is the position of the underlying
    highlight in the input list, used by the SRT generator to
    attribute transcript lines.
    """
    kind: str
    duration_ms: int
    output_start_ms: int
    label: str = ""
    source_start_ms: int = 0
    source_end_ms: int = 0
    source_highlight_index: int = -1

    @property
    def output_end_ms(self) -> int:
        return self.output_start_ms + self.duration_ms


def plan_highlight_timeline(
    highlights: list[Highlight],
    *,
    mode: str = "video",
    title_interstitial_ms: int = DEFAULT_TITLE_INTERSTITIAL_MS,
    jump_interstitial_ms: int = DEFAULT_JUMP_INTERSTITIAL_MS,
    audio_gap_ms: int = DEFAULT_AUDIO_GAP_MS,
) -> List[TimelineSegment]:
    """Build the segment plan for a highlight export.

    `mode = 'video'` produces title + jump interstitials separating
    the highlights as the issue specifies. `mode = 'audio'`
    produces just the highlights, with a short silent gap between
    them (no on-screen text to read). Both modes return the
    segments in playback order.

    Highlights are auto-sorted by their `start_ms` so the planner
    accepts an unsorted input -- the bar widget keeps them in
    insertion order on disk, but the export expects time order.
    """
    if mode not in {"video", "audio"}:
        raise ValueError(f"unknown mode {mode!r} (use 'video' or 'audio')")
    if not highlights:
        return []
    ordered = sorted(
        enumerate(highlights), key=lambda kv: kv[1].start_ms,
    )

    out: List[TimelineSegment] = []
    cursor = 0
    for n, (orig_idx, h) in enumerate(ordered):
        if mode == "video":
            # Title card before every highlight (including the
            # first -- the first one's title gives the viewer context
            # at t=0).
            title_text = h.title or f"Highlight {orig_idx + 1}"
            out.append(TimelineSegment(
                kind=SEGMENT_TITLE,
                duration_ms=title_interstitial_ms,
                output_start_ms=cursor,
                label=title_text,
            ))
            cursor += title_interstitial_ms
        elif n > 0:
            # Audio-mode silent gap between highlights only (no
            # leading gap; the listener doesn't need a hush before
            # the first clip).
            out.append(TimelineSegment(
                kind=SEGMENT_JUMP,
                duration_ms=audio_gap_ms,
                output_start_ms=cursor,
                label="",
            ))
            cursor += audio_gap_ms

        out.append(TimelineSegment(
            kind=SEGMENT_HIGHLIGHT,
            duration_ms=h.duration_ms(),
            output_start_ms=cursor,
            source_start_ms=h.start_ms,
            source_end_ms=h.end_ms,
            source_highlight_index=orig_idx,
        ))
        cursor += h.duration_ms()

        # Jump-card AFTER each highlight except the last (video mode).
        if mode == "video" and n < len(ordered) - 1:
            next_h = ordered[n + 1][1]
            out.append(TimelineSegment(
                kind=SEGMENT_JUMP,
                duration_ms=jump_interstitial_ms,
                output_start_ms=cursor,
                label=f"Jumping to {_format_mmss(next_h.start_ms)}",
            ))
            cursor += jump_interstitial_ms

    return out


def total_output_duration_ms(plan: Iterable[TimelineSegment]) -> int:
    plan_list = list(plan)
    return plan_list[-1].output_end_ms if plan_list else 0


def _format_mmss(ms: int) -> str:
    """MM:SS for short clips, HH:MM:SS for long ones. Matches the
    transcript timestamp prefix the user already sees."""
    seconds = int(ms / 1000)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


# ----------------------------------------------------------------------
# SRT generator for video exports


def remap_transcript_to_highlights(
    transcript_text: str,
    plan: list[TimelineSegment],
) -> list[tuple[int, int, str]]:
    """Translate the transcript's [HH:MM:SS] cues into the new
    timeline so subtitles still line up with the cuts.

    Each transcript line whose source timestamp falls inside a
    SEGMENT_HIGHLIGHT becomes a cue at the corresponding
    output-timeline position. Lines outside any highlight are
    dropped. Returns (start_ms, end_ms, text) tuples; the caller
    formats them into SRT.
    """
    import re as _re
    timestamp_re = _re.compile(r"^\[(\d+):(\d{2}):(\d{2})\]\s*(.*)$")
    if not transcript_text:
        return []
    highlights_only = [
        s for s in plan if s.kind == SEGMENT_HIGHLIGHT
    ]
    cues: list[tuple[int, int, str]] = []
    for line in transcript_text.splitlines():
        match = timestamp_re.match(line)
        if match is None:
            continue
        hours, minutes, seconds, text = match.groups()
        source_ms = (
            (int(hours) * 3600 + int(minutes) * 60 + int(seconds)) * 1000
        )
        # Find the highlight segment that contains this source_ms.
        for seg in highlights_only:
            if seg.source_start_ms <= source_ms < seg.source_end_ms:
                offset_in_seg = source_ms - seg.source_start_ms
                out_start = seg.output_start_ms + offset_in_seg
                # End at the next cue inside the same segment, or
                # at the segment end. Filled in by the caller.
                cues.append((out_start, 0, text.strip()))
                break
    # Fill end_ms = next cue's start (or +5s for the last cue),
    # capped to the output end so a tail cue doesn't overflow.
    last_output_end = highlights_only[-1].output_end_ms if highlights_only else 0
    for i, (start, _placeholder, text) in enumerate(cues):
        if i + 1 < len(cues):
            end = min(cues[i + 1][0], last_output_end)
        else:
            end = min(start + 5_000, last_output_end)
        if end < start:
            end = start
        cues[i] = (start, end, text)
    return cues


def format_srt(cues: list[tuple[int, int, str]]) -> str:
    """Standard SubRip format. Cue indices start at 1."""
    if not cues:
        return ""
    lines: list[str] = []
    for idx, (start_ms, end_ms, text) in enumerate(cues, 1):
        lines.append(str(idx))
        lines.append(f"{_ms_to_srt(start_ms)} --> {_ms_to_srt(end_ms)}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


def _ms_to_srt(ms: int) -> str:
    hours, rem = divmod(int(ms), 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, ms_rem = divmod(rem, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ms_rem:03d}"


# ----------------------------------------------------------------------
# Audio export (no interstitial frames; just silent gap)


def export_highlights_audio(
    mic_path: Optional[Path],
    sys_path: Optional[Path],
    highlights: list[Highlight],
    dst_path: Path,
    *,
    audio_gap_ms: int = DEFAULT_AUDIO_GAP_MS,
    progress: Optional[Callable[[int], None]] = None,
) -> None:
    """Concatenate the audio inside each highlight range, separated
    by `audio_gap_ms` of silence, into a single file at dst_path.

    Mirrors the existing single-file audio export's encoding path
    (audio/export.py) -- we just feed it a constructed buffer
    instead of the full meeting. Output format is inferred from
    dst_path.suffix the same way export_mixed does it.
    """
    from .export import (
        _EXPORT_FORMATS,
        _TARGET_RATE,
        _decode_to_mono_float32,
        _encode_mono_float32,
        _mix_to_max_length,
    )

    plan = plan_highlight_timeline(
        highlights, mode="audio", audio_gap_ms=audio_gap_ms,
    )
    if not plan:
        raise ValueError("no highlights to export")

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
        raise ValueError("no source audio files available")

    # Decode + mix once; slice per-highlight against the resulting
    # mono buffer.
    decoded = [_decode_to_mono_float32(p, _TARGET_RATE) for p in sources]
    mixed = _mix_to_max_length(decoded).astype(np.float32, copy=False)
    np.clip(mixed, -1.0, 1.0, out=mixed)

    pieces: list[np.ndarray] = []
    gap_samples = int(audio_gap_ms * _TARGET_RATE / 1000)
    silent_gap = np.zeros(gap_samples, dtype=np.float32)
    for seg in plan:
        if seg.kind == SEGMENT_JUMP:
            pieces.append(silent_gap)
            continue
        start_samp = int(seg.source_start_ms * _TARGET_RATE / 1000)
        end_samp = int(seg.source_end_ms * _TARGET_RATE / 1000)
        start_samp = max(0, min(start_samp, mixed.size))
        end_samp = max(start_samp, min(end_samp, mixed.size))
        pieces.append(mixed[start_samp:end_samp])
        if progress is not None:
            done = seg.source_highlight_index + 1
            pct = int(done * 100 / max(1, len(highlights)))
            progress(pct)

    concatenated = (
        np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.float32)
    )
    _encode_mono_float32(
        concatenated,
        dst_path=dst_path,
        codec_name=codec_name,
        container_format=container_format,
    )
    if progress is not None:
        progress(100)


# ----------------------------------------------------------------------
# Video export


def export_highlights_video(
    mic_path: Optional[Path],
    sys_path: Optional[Path],
    screenshots: list[tuple[Path, int]],
    transcript_text: str,
    highlights: list[Highlight],
    dst_path: Path,
    *,
    title_interstitial_ms: int = DEFAULT_TITLE_INTERSTITIAL_MS,
    jump_interstitial_ms: int = DEFAULT_JUMP_INTERSTITIAL_MS,
    progress: Optional[Callable[[int], None]] = None,
) -> None:
    """Render a highlights-only MP4 with title + jump interstitials.

    Concatenates each highlight's slides + audio, inserting a 2s
    title card before every highlight and a 2s jump card between
    consecutive highlights. SRT sidecar is generated against the
    new (output) timeline so subtitles align with the cuts.

    Mirrors export_video's encoder configuration (1920x1080 / 30fps
    H.264 + AAC mono); the only structural difference is the
    timeline -- highlight segments use a screenshot lookup against
    the source offset; interstitial segments paint a single text
    frame.
    """
    from .video_export import (
        _TARGET_AUDIO_RATE, _TARGET_FPS, _TARGET_HEIGHT, _TARGET_WIDTH,
        _AUDIO_BIT_RATE, _VIDEO_BIT_RATE,
        _decode_audio_to_mono, _mix_to_max_length,
        _scale_screenshot_letterbox,
    )
    from ..screencap.timestamps import current_screenshot_for_position

    plan = plan_highlight_timeline(
        highlights,
        mode="video",
        title_interstitial_ms=title_interstitial_ms,
        jump_interstitial_ms=jump_interstitial_ms,
    )
    if not plan:
        raise ValueError("no highlights to export")

    sources = [
        p for p in (mic_path, sys_path)
        if p is not None and p.exists() and p.stat().st_size > 0
    ]
    if not sources:
        raise ValueError("no source audio files available")

    log.info(
        "export_highlights_video: %d highlights, %d screenshots, dst=%s",
        len(highlights), len(screenshots), dst_path,
    )

    # ---- decode + slice + concat audio (matches highlight slicing
    # used in the audio-only export above) ----
    decoded = [_decode_audio_to_mono(p, _TARGET_AUDIO_RATE) for p in sources]
    mixed = _mix_to_max_length(decoded).astype(np.float32, copy=False)
    np.clip(mixed, -1.0, 1.0, out=mixed)

    output_audio_chunks: list[np.ndarray] = []
    for seg in plan:
        seg_samples = int(seg.duration_ms * _TARGET_AUDIO_RATE / 1000)
        if seg.kind == SEGMENT_HIGHLIGHT:
            start_samp = int(seg.source_start_ms * _TARGET_AUDIO_RATE / 1000)
            end_samp = int(seg.source_end_ms * _TARGET_AUDIO_RATE / 1000)
            start_samp = max(0, min(start_samp, mixed.size))
            end_samp = max(start_samp, min(end_samp, mixed.size))
            chunk = mixed[start_samp:end_samp]
            # Pad in case the source ended early for the requested
            # range (defensive against off-by-one at the tail).
            if chunk.size < seg_samples:
                chunk = np.concatenate(
                    [chunk, np.zeros(seg_samples - chunk.size, dtype=np.float32)]
                )
            elif chunk.size > seg_samples:
                chunk = chunk[:seg_samples]
            output_audio_chunks.append(chunk)
        else:
            output_audio_chunks.append(np.zeros(seg_samples, dtype=np.float32))
    output_audio = np.concatenate(output_audio_chunks)

    # ---- write SRT sidecar from remapped transcript ----
    srt_path = dst_path.with_suffix(".srt")
    if transcript_text:
        try:
            cues = remap_transcript_to_highlights(transcript_text, plan)
            srt_path.write_text(format_srt(cues), encoding="utf-8")
        except Exception:
            log.exception("export_highlights_video: SRT write failed")

    # ---- render the MP4 ----
    import av  # noqa: PLC0415

    total_video_frames = int(
        total_output_duration_ms(plan) * _TARGET_FPS / 1000
    )

    out_container = None
    try:
        out_container = av.open(str(dst_path), mode="w", format="mp4")
        v_stream = out_container.add_stream("libx264", rate=_TARGET_FPS)
        v_stream.width = _TARGET_WIDTH
        v_stream.height = _TARGET_HEIGHT
        v_stream.pix_fmt = "yuv420p"
        v_stream.bit_rate = _VIDEO_BIT_RATE
        v_stream.codec_context.gop_size = _TARGET_FPS * 2
        a_stream = out_container.add_stream("aac", rate=_TARGET_AUDIO_RATE)
        a_stream.layout = "mono"
        a_stream.bit_rate = _AUDIO_BIT_RATE

        _encode_highlight_video(
            out_container, v_stream, plan, screenshots,
            total_video_frames, progress=progress,
            current_screenshot_for_position=current_screenshot_for_position,
            scale_screenshot=_scale_screenshot_letterbox,
        )
        _encode_audio_buffer(
            out_container, a_stream, output_audio, _TARGET_AUDIO_RATE,
        )
    except Exception:
        log.exception("export_highlights_video failed; cleaning up")
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


def _encode_highlight_video(
    out_container,
    v_stream,
    plan: list[TimelineSegment],
    screenshots: list[tuple[Path, int]],
    total_frames: int,
    *,
    progress: Optional[Callable[[int], None]],
    current_screenshot_for_position,
    scale_screenshot,
) -> None:
    """Frame-by-frame slideshow encode for the highlight output.

    Title / jump segments paint a single text card (rendered once,
    reused across all 2 seconds of frames). Highlight segments
    look up the matching screenshot from the source-time offset
    via the existing sticky-latest helper.
    """
    import av  # noqa: PLC0415
    from .video_export import _TARGET_FPS, _TARGET_HEIGHT, _TARGET_WIDTH

    black_frame = np.zeros(
        (_TARGET_HEIGHT, _TARGET_WIDTH, 3), dtype=np.uint8,
    )
    scaled_cache: dict[Path, np.ndarray] = {}
    interstitial_cache: dict[str, np.ndarray] = {}
    last_progress_pct = -1

    frame_idx = 0
    for seg in plan:
        seg_frames = int(seg.duration_ms * _TARGET_FPS / 1000)
        if seg.kind in (SEGMENT_TITLE, SEGMENT_JUMP):
            card = interstitial_cache.get(seg.label)
            if card is None:
                card = _render_interstitial_frame(seg.label)
                interstitial_cache[seg.label] = card
            for _ in range(seg_frames):
                av_frame = av.VideoFrame.from_ndarray(card, format="rgb24")
                av_frame.pts = frame_idx
                for packet in v_stream.encode(av_frame):
                    out_container.mux(packet)
                frame_idx += 1
                if progress is not None and total_frames > 0:
                    pct = int(frame_idx * 100 / total_frames)
                    if pct != last_progress_pct:
                        progress(pct)
                        last_progress_pct = pct
            continue

        # Highlight segment -- sticky-latest screenshot per
        # source-time offset, scaled + cached.
        for local_frame in range(seg_frames):
            source_ms = seg.source_start_ms + int(
                local_frame * 1000 / _TARGET_FPS
            )
            current_path = current_screenshot_for_position(
                screenshots, source_ms,
            )
            if current_path is None:
                arr = black_frame
            else:
                arr = scaled_cache.get(current_path)
                if arr is None:
                    arr = scale_screenshot(current_path)
                    scaled_cache[current_path] = arr
            av_frame = av.VideoFrame.from_ndarray(arr, format="rgb24")
            av_frame.pts = frame_idx
            for packet in v_stream.encode(av_frame):
                out_container.mux(packet)
            frame_idx += 1
            if progress is not None and total_frames > 0:
                pct = int(frame_idx * 100 / total_frames)
                if pct != last_progress_pct:
                    progress(pct)
                    last_progress_pct = pct

    # Flush.
    for packet in v_stream.encode(None):
        out_container.mux(packet)
    if progress is not None:
        progress(100)


def _render_interstitial_frame(text: str) -> np.ndarray:
    """Render a black-background centered-text card to a (H, W, 3)
    uint8 array. Uses PIL (already a hard dependency for the
    existing slideshow path) so we don't introduce a new package."""
    from PIL import Image, ImageDraw, ImageFont
    from .video_export import _TARGET_HEIGHT, _TARGET_WIDTH

    canvas = Image.new("RGB", (_TARGET_WIDTH, _TARGET_HEIGHT), (0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    # Pick a reasonable font size relative to canvas height; PIL's
    # default font is bitmap-only and unreadable at 1080p, but
    # `load_default` covers the worst case where no system font is
    # available. Try Helvetica/Arial first.
    font = _load_interstitial_font(size=72)
    # Multi-line wrap by hand: split on words and pack into lines
    # no wider than 80% of the canvas. PIL's textbbox tells us the
    # actual rendered width for the chosen font.
    max_w = int(_TARGET_WIDTH * 0.8)
    lines = _wrap_text(text, font, draw, max_w)
    line_heights = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_heights.append(bbox[3] - bbox[1])
    total_h = sum(line_heights) + max(0, len(lines) - 1) * 12
    y = (_TARGET_HEIGHT - total_h) // 2
    for line, lh in zip(lines, line_heights):
        bbox = draw.textbbox((0, 0), line, font=font)
        w = bbox[2] - bbox[0]
        x = (_TARGET_WIDTH - w) // 2
        draw.text((x, y), line, fill=(255, 255, 255), font=font)
        y += lh + 12
    return np.asarray(canvas, dtype=np.uint8)


def _load_interstitial_font(*, size: int):
    """Try a couple of platform-portable font names before falling
    back to PIL's default bitmap font. Wrapped so the test path
    can monkey-patch without poking PIL internals."""
    from PIL import ImageFont
    for name in ("DejaVuSans-Bold.ttf", "Arial.ttf", "Helvetica.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _wrap_text(text: str, font, draw, max_width: int) -> list[str]:
    """Greedy word-wrap to keep each line under max_width pixels."""
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current: list[str] = [words[0]]
    for word in words[1:]:
        trial = " ".join(current + [word])
        bbox = draw.textbbox((0, 0), trial, font=font)
        if (bbox[2] - bbox[0]) <= max_width:
            current.append(word)
        else:
            lines.append(" ".join(current))
            current = [word]
    lines.append(" ".join(current))
    return lines


def _encode_audio_buffer(
    out_container, a_stream, buffer: np.ndarray, rate: int,
) -> None:
    """Stream a mono float32 PCM buffer through AAC -> MP4. Mirrors
    video_export._encode_audio (kept here to avoid the cross-module
    dependency edge case)."""
    import av  # noqa: PLC0415
    from av.audio.frame import AudioFrame  # noqa: PLC0415

    chunk_frames = rate  # 1 sec per chunk
    for i in range(0, buffer.size, chunk_frames):
        chunk = buffer[i : i + chunk_frames]
        frame = AudioFrame.from_ndarray(
            chunk.reshape(1, -1), format="flt", layout="mono",
        )
        frame.rate = rate
        frame.pts = i
        for packet in a_stream.encode(frame):
            out_container.mux(packet)
    for packet in a_stream.encode(None):
        out_container.mux(packet)
