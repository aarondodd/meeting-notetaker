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
import sys
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

    `subtitle` is a secondary line rendered below the main label on
    title cards (e.g. "Recorded on 2026-05-24 14:30"). Empty for
    jump cards and highlight segments.

    `source_highlight_index` is the position of the underlying
    highlight in the input list, used by the SRT generator to
    attribute transcript lines.
    """
    kind: str
    duration_ms: int
    output_start_ms: int
    label: str = ""
    subtitle: str = ""
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
    session_title: str = "",
    session_subtitle: str = "",
) -> List[TimelineSegment]:
    """Build the segment plan for a highlight export.

    `mode = 'video'` produces an initial session-title card +
    per-highlight title cards + jump cards. `mode = 'audio'`
    produces just the highlights with short silent gaps between
    them (no surface to render text on -- session-title /
    subtitle kwargs are ignored). Both modes return the segments
    in playback order.

    Highlights are auto-sorted by their `start_ms`.

    `session_title` (non-empty + video mode only) prepends a
    one-shot 2s title card to the output. Its `label` is the
    session title; its `subtitle` is `session_subtitle` (typically
    "Recorded on YYYY-MM-DD HH:MM" via
    format_recorded_on_subtitle). Per-highlight title cards do
    NOT carry the subtitle -- the session-level context appears
    once at the top of the file, not before each highlight.
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

    # Prepend the session-title card if requested and the format
    # supports text overlays.
    if mode == "video" and session_title.strip():
        out.append(TimelineSegment(
            kind=SEGMENT_TITLE,
            duration_ms=title_interstitial_ms,
            output_start_ms=cursor,
            label=session_title.strip(),
            subtitle=session_subtitle,
            source_highlight_index=-1,
        ))
        cursor += title_interstitial_ms

    for n, (orig_idx, h) in enumerate(ordered):
        if mode == "video":
            # Per-highlight title card. Plain title only -- the
            # session-level "Recorded on" lives on the initial
            # card above, not repeated per highlight.
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


def format_recorded_on_subtitle(started_at_iso: str) -> str:
    """Render a UTC ISO timestamp as "Recorded on YYYY-MM-DD HH:MM"
    in the user's local timezone, the standard subtitle line on
    every title card.

    Returns "" on bad / missing input -- callers can pass an empty
    string through to plan_highlight_timeline to suppress the
    subtitle entirely (e.g. for an unrecorded session, which
    shouldn't have highlights anyway but the export path is
    defensive).
    """
    from datetime import datetime as _dt
    iso = (started_at_iso or "").strip()
    if not iso:
        return ""
    try:
        utc_aware = _dt.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    local = utc_aware.astimezone()
    return "Recorded on " + local.strftime("%Y-%m-%d %H:%M")


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
    session_title: str = "",
    session_started_at_iso: str = "",
    progress: Optional[Callable[[int], None]] = None,
) -> None:
    """Render a highlights-only MP4 with title + jump interstitials.

    Concatenates each highlight's slides + audio, inserting a 2s
    title card before every highlight and a 2s jump card between
    consecutive highlights. SRT sidecar is generated against the
    new (output) timeline so subtitles align with the cuts.

    When `session_title` is non-empty, prepends a single 2s
    session-title card at the very beginning of the output with
    the session's title and a "Recorded on YYYY-MM-DD HH:MM"
    subtitle derived from `session_started_at_iso` (UTC ISO from
    the SessionStore). Empty session_title suppresses the initial
    card entirely; per-highlight title cards always render
    regardless.

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

    session_subtitle = format_recorded_on_subtitle(session_started_at_iso)
    plan = plan_highlight_timeline(
        highlights,
        mode="video",
        title_interstitial_ms=title_interstitial_ms,
        jump_interstitial_ms=jump_interstitial_ms,
        session_title=session_title,
        session_subtitle=session_subtitle,
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
    # Key on (label, subtitle) so two title cards with the same
    # title text but different subtitles (shouldn't happen in
    # practice -- title_subtitle is a per-export constant -- but
    # cheap to be correct) get distinct cached frames.
    interstitial_cache: dict[tuple[str, str], np.ndarray] = {}
    last_progress_pct = -1

    frame_idx = 0
    for seg in plan:
        seg_frames = int(seg.duration_ms * _TARGET_FPS / 1000)
        if seg.kind in (SEGMENT_TITLE, SEGMENT_JUMP):
            cache_key = (seg.label, seg.subtitle)
            card = interstitial_cache.get(cache_key)
            if card is None:
                card = _render_interstitial_frame(seg.label, seg.subtitle)
                interstitial_cache[cache_key] = card
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


# Auto-fit constraints for interstitial cards. The text fills the
# canvas aggressively per Aaron's "make it very large" feedback --
# the cards exist for at-a-glance viewing during fast playback, so
# legibility trumps margin. The width budget is 90% of the canvas;
# the height budget is 80% (leaves a small letterbox so single-line
# text doesn't sit edge-to-edge top-and-bottom).
_INTERSTITIAL_WIDTH_PCT = 0.90
_INTERSTITIAL_HEIGHT_PCT = 0.80
# Font-size search bounds. Start at the upper bound and step down
# until both width + height constraints are met. The lower bound
# is the floor where text is still legible on a 1080p slide; tex
# never falls below it, even if it has to wrap to many lines.
_INTERSTITIAL_MAX_FONT_PT = 320
_INTERSTITIAL_MIN_FONT_PT = 60
_INTERSTITIAL_FONT_STEP_PT = 12


def _render_interstitial_frame(text: str, subtitle: str = "") -> np.ndarray:
    """Render a black-background centered-text card to a (H, W, 3)
    uint8 array.

    Auto-sizes the title: starts at _INTERSTITIAL_MAX_FONT_PT and
    steps down until the wrapped text fits inside the canvas
    budget. When `subtitle` is non-empty, leaves room for it below
    the title (smaller font, ~50% of the title's size) and centers
    the combined block vertically. Short labels like "Highlight 1"
    still hit the title-max-size, gaining a small secondary line
    below them. Long titles wrap and shrink as needed; subtitle
    follows suit at half-size.
    """
    from PIL import Image, ImageDraw
    from .video_export import _TARGET_HEIGHT, _TARGET_WIDTH

    canvas = Image.new("RGB", (_TARGET_WIDTH, _TARGET_HEIGHT), (0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    max_w = int(_TARGET_WIDTH * _INTERSTITIAL_WIDTH_PCT)
    # Title budget is the full height when there's no subtitle;
    # leaves ~25% for the subtitle when one is present (title gets
    # ~55%, with the rest going to inter-line gap + subtitle).
    if subtitle:
        title_h_budget = int(_TARGET_HEIGHT * (_INTERSTITIAL_HEIGHT_PCT - 0.25))
    else:
        title_h_budget = int(_TARGET_HEIGHT * _INTERSTITIAL_HEIGHT_PCT)

    title_font, title_lines, title_size = _fit_text_to_card(
        draw, text, max_w, title_h_budget,
    )
    title_line_spacing = max(8, title_size // 6)
    title_line_heights = [
        _line_height_for(draw, title_font) for _ in title_lines
    ]
    title_block_h = (
        sum(title_line_heights)
        + max(0, len(title_lines) - 1) * title_line_spacing
    )

    # Subtitle setup.
    subtitle_lines: list[str] = []
    subtitle_font = None
    subtitle_line_spacing = 0
    subtitle_block_h = 0
    title_subtitle_gap = 0
    if subtitle:
        # Subtitle gets roughly half the title size, with a floor so
        # it stays legible even on a wrap-heavy title that drives
        # the title font down to the min.
        target_subtitle_size = max(48, title_size // 2)
        sub_h_budget = int(_TARGET_HEIGHT * 0.20)
        subtitle_font, subtitle_lines, subtitle_size = _fit_text_to_card(
            draw, subtitle, max_w, sub_h_budget,
            preferred_max_size=target_subtitle_size,
        )
        subtitle_line_spacing = max(6, subtitle_size // 8)
        subtitle_line_heights = [
            _line_height_for(draw, subtitle_font) for _ in subtitle_lines
        ]
        subtitle_block_h = (
            sum(subtitle_line_heights)
            + max(0, len(subtitle_lines) - 1) * subtitle_line_spacing
        )
        # Gap between title and subtitle: ~half a title line height,
        # so the two read as one centered block instead of two
        # disconnected pieces.
        title_subtitle_gap = max(20, title_size // 3)

    total_h = title_block_h + title_subtitle_gap + subtitle_block_h
    y = (_TARGET_HEIGHT - total_h) // 2
    # Title.
    for line, lh in zip(title_lines, title_line_heights):
        w = _line_width_for(draw, title_font, line)
        x = (_TARGET_WIDTH - w) // 2
        draw.text((x, y), line, fill=(255, 255, 255), font=title_font)
        y += lh + title_line_spacing
    # Strip the trailing line_spacing past the last title line; the
    # explicit title_subtitle_gap takes over.
    if title_lines:
        y -= title_line_spacing
    # Subtitle (lighter gray to read as secondary text).
    if subtitle_font is not None:
        y += title_subtitle_gap
        for line in subtitle_lines:
            w = _line_width_for(draw, subtitle_font, line)
            x = (_TARGET_WIDTH - w) // 2
            draw.text(
                (x, y), line, fill=(190, 190, 190), font=subtitle_font,
            )
            y += _line_height_for(draw, subtitle_font) + subtitle_line_spacing
    return np.asarray(canvas, dtype=np.uint8)


def _fit_text_to_card(
    draw,
    text: str,
    max_w: int,
    max_h: int,
    *,
    preferred_max_size: Optional[int] = None,
) -> tuple[object, list[str], int]:
    """Find the largest font size at which `text` fits the card.

    Strategy is single-line-preferred:

    1. Walk sizes max -> min. The largest size at which the text
       fits on ONE line within `max_w` and `max_h` wins.
    2. If no single-line fit exists in the range, walk again
       allowing the wrapper to break into multiple lines, and
       pick the largest size where the wrapped block fits.
    3. Final fallback: minimum size + whatever wrap that gives
       (renderer still produces a valid frame; the user may see
       a clipped corner for absurdly long titles).

    The two-pass approach prevents the "Highlight 1" -> ["Highlight",
    "1"] tradeoff where a slightly larger font wraps to an awkward
    multi-line layout: a single line at a slightly smaller size
    almost always reads better.

    `preferred_max_size` (subtitle path) caps the starting font so
    the subtitle stays visually secondary to the title even when
    the title's auto-fit landed at a lower size than the global
    max. Falls back to _INTERSTITIAL_MAX_FONT_PT when unset.

    Returns (font, wrapped_lines, font_size).
    """
    text = text.strip() or " "
    start_size = (
        preferred_max_size if preferred_max_size is not None
        else _INTERSTITIAL_MAX_FONT_PT
    )
    start_size = max(_INTERSTITIAL_MIN_FONT_PT, start_size)
    # Pass 1: single-line preference.
    for size in range(
        start_size,
        _INTERSTITIAL_MIN_FONT_PT - 1,
        -_INTERSTITIAL_FONT_STEP_PT,
    ):
        font = _load_interstitial_font(size=size)
        width = _line_width_for(draw, font, text)
        if width > max_w:
            continue
        if _line_height_for(draw, font) > max_h:
            continue
        return font, [text], size
    # Pass 2: allow wrapping.
    for size in range(
        start_size,
        _INTERSTITIAL_MIN_FONT_PT - 1,
        -_INTERSTITIAL_FONT_STEP_PT,
    ):
        font = _load_interstitial_font(size=size)
        lines = _wrap_text(text, font, draw, max_w)
        if not lines:
            continue
        line_spacing = max(8, size // 6)
        line_h = _line_height_for(draw, font)
        total_h = (
            len(lines) * line_h
            + max(0, len(lines) - 1) * line_spacing
        )
        widest = max(
            _line_width_for(draw, font, line) for line in lines
        )
        if widest <= max_w and total_h <= max_h:
            return font, lines, size
    # Last resort.
    font = _load_interstitial_font(size=_INTERSTITIAL_MIN_FONT_PT)
    lines = _wrap_text(text, font, draw, max_w)
    return font, lines, _INTERSTITIAL_MIN_FONT_PT


def _line_height_for(draw, font) -> int:
    """Stable per-line height using a 'Mg' probe so descenders are
    accounted for regardless of which words land on which line."""
    bbox = draw.textbbox((0, 0), "Mg", font=font)
    return bbox[3] - bbox[1]


def _line_width_for(draw, font, line: str) -> int:
    if not line:
        return 0
    bbox = draw.textbbox((0, 0), line, font=font)
    return bbox[2] - bbox[0]


_FONT_CANDIDATES_LINUX = (
    "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
_FONT_CANDIDATES_WINDOWS = (
    # Full paths first -- PIL's bare-name lookup on Windows is
    # patchy across versions and has been the source of silent
    # font-load failures (-> load_default fallback at bitmap size).
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    # Bare names as a last-ditch try in case the user has a
    # non-standard install or a portable variant on PATH.
    "arialbd.ttf",
    "arial.ttf",
    "Arial Bold.ttf",
    "Arial.ttf",
)
_FONT_CANDIDATES_MACOS = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)


def _load_interstitial_font(*, size: int):
    """Return a sized TrueType font for the interstitial text card.

    Tries a platform-appropriate list of full paths first, then bare
    names. If everything fails -- shouldn't happen on any standard
    install but possible on a stripped Linux container -- falls
    back to `ImageFont.load_default(size=size)`. Pillow >=10
    (our pinned floor) supports the size kwarg and returns a
    properly-scaled TTF instead of the legacy tiny bitmap.

    The earlier implementation only tried bare names and on Windows
    consistently silently fell through to the un-sized load_default,
    rendering interstitial text at ~10pt regardless of what the
    auto-fit chose -- the bug Aaron reported after v0.7.0 testing.
    """
    from PIL import ImageFont

    # Platform-priority order; cross-platform candidates come last
    # so a Windows box picks an Arial variant before trying DejaVu
    # (rarely installed on Windows) and vice versa.
    if sys.platform.startswith("win"):
        candidates = (
            *_FONT_CANDIDATES_WINDOWS,
            *_FONT_CANDIDATES_LINUX,
            *_FONT_CANDIDATES_MACOS,
        )
    elif sys.platform == "darwin":
        candidates = (
            *_FONT_CANDIDATES_MACOS,
            *_FONT_CANDIDATES_WINDOWS,
            *_FONT_CANDIDATES_LINUX,
        )
    else:
        candidates = (
            *_FONT_CANDIDATES_LINUX,
            *_FONT_CANDIDATES_WINDOWS,
            *_FONT_CANDIDATES_MACOS,
        )

    for name in candidates:
        try:
            font = ImageFont.truetype(name, size)
            log.debug(
                "_load_interstitial_font: loaded %s at %dpt", name, size,
            )
            return font
        except (OSError, IOError):
            continue
    # Last resort: ask Pillow for its bundled default at the right
    # size. Pillow 10+ ships DejaVu and respects the size kwarg;
    # older Pillows would fall back to the un-sized bitmap (we pin
    # Pillow>=10 in requirements.txt to avoid that).
    log.warning(
        "_load_interstitial_font: no system TTF found, falling back "
        "to Pillow's bundled default at %dpt -- interstitial text "
        "may render at the wrong size if Pillow < 10",
        size,
    )
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        # Pillow <10 doesn't accept size kwarg. We require >=10 in
        # requirements.txt; this branch exists only as a paranoid
        # guard for unusual dev environments.
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
