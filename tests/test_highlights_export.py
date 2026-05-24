"""Highlights export plan + SRT remap.

The encoder paths (PyAV, PIL) are tested live on Windows in the
release pipeline; the load-bearing pure-Python piece is the timeline
planner + the transcript->concatenated-timeline remap. Those produce
the (segment kind, start, end) tuples every encoder downstream
depends on.
"""
from __future__ import annotations

import pytest

from meeting_notetaker.audio.highlights_export import (
    DEFAULT_JUMP_INTERSTITIAL_MS,
    DEFAULT_TITLE_INTERSTITIAL_MS,
    SEGMENT_HIGHLIGHT,
    SEGMENT_JUMP,
    SEGMENT_TITLE,
    format_srt,
    plan_highlight_timeline,
    remap_transcript_to_highlights,
    total_output_duration_ms,
)
from meeting_notetaker.models.highlights import Highlight


# ---- plan_highlight_timeline (video mode) -----------------------------


def test_plan_video_single_highlight_has_title_card_then_segment():
    plan = plan_highlight_timeline(
        [Highlight(10_000, 20_000, "Decision on MDM")],
        mode="video",
    )
    kinds = [s.kind for s in plan]
    assert kinds == [SEGMENT_TITLE, SEGMENT_HIGHLIGHT]
    assert plan[0].duration_ms == DEFAULT_TITLE_INTERSTITIAL_MS
    assert plan[0].label == "Decision on MDM"
    assert plan[1].duration_ms == 10_000


def test_plan_video_untitled_highlight_falls_back_to_index_label():
    plan = plan_highlight_timeline(
        [Highlight(0, 1000)],
        mode="video",
    )
    assert plan[0].label == "Highlight 1"


def test_plan_video_two_highlights_interleaves_title_jump_title():
    h1 = Highlight(10_000, 15_000, "First")
    h2 = Highlight(30_000, 32_000, "Second")
    plan = plan_highlight_timeline([h1, h2], mode="video")
    kinds = [s.kind for s in plan]
    assert kinds == [
        SEGMENT_TITLE,     # before highlight 1
        SEGMENT_HIGHLIGHT, # highlight 1
        SEGMENT_JUMP,      # cut to highlight 2
        SEGMENT_TITLE,     # before highlight 2
        SEGMENT_HIGHLIGHT, # highlight 2
    ]


def test_plan_video_jump_card_shows_destination_mmss():
    h1 = Highlight(0, 5_000, "A")
    h2 = Highlight(125_000, 130_000, "B")
    plan = plan_highlight_timeline([h1, h2], mode="video")
    jump_seg = [s for s in plan if s.kind == SEGMENT_JUMP][0]
    assert "02:05" in jump_seg.label


def test_plan_video_jump_card_shows_hours_for_long_sessions():
    h1 = Highlight(0, 5_000, "A")
    h2 = Highlight(3_725_000, 3_730_000, "B")  # 1:02:05
    plan = plan_highlight_timeline([h1, h2], mode="video")
    jump_seg = [s for s in plan if s.kind == SEGMENT_JUMP][0]
    assert "01:02:05" in jump_seg.label


def test_plan_video_output_timeline_monotonic():
    h1 = Highlight(0, 5_000, "A")
    h2 = Highlight(20_000, 25_000, "B")
    h3 = Highlight(60_000, 65_000, "C")
    plan = plan_highlight_timeline([h1, h2, h3], mode="video")
    # Each segment starts exactly where the previous one ended.
    for prev, curr in zip(plan, plan[1:]):
        assert curr.output_start_ms == prev.output_end_ms


def test_plan_handles_unsorted_input():
    """Issue #26's bar widget keeps highlights in user-insertion
    order; the planner has to time-sort them itself."""
    later = Highlight(50_000, 55_000, "Later")
    earlier = Highlight(10_000, 15_000, "Earlier")
    plan = plan_highlight_timeline([later, earlier], mode="video")
    highlight_segments = [s for s in plan if s.kind == SEGMENT_HIGHLIGHT]
    assert highlight_segments[0].source_start_ms < highlight_segments[1].source_start_ms


def test_plan_empty_highlights_returns_empty():
    assert plan_highlight_timeline([], mode="video") == []
    assert plan_highlight_timeline([], mode="audio") == []


# ---- plan_highlight_timeline (audio mode) -----------------------------


def test_plan_audio_skips_title_cards():
    """Audio-only has no surface to render text on, so just clip +
    silent gaps."""
    plan = plan_highlight_timeline(
        [Highlight(0, 1000, "A"), Highlight(2000, 3000, "B")],
        mode="audio",
        audio_gap_ms=500,
    )
    kinds = [s.kind for s in plan]
    assert kinds == [SEGMENT_HIGHLIGHT, SEGMENT_JUMP, SEGMENT_HIGHLIGHT]
    # Total output = h1 + gap + h2 = 1000 + 500 + 1000.
    assert total_output_duration_ms(plan) == 2500


def test_plan_audio_no_leading_gap():
    """First highlight starts at output_ms=0; no silent prefix."""
    plan = plan_highlight_timeline(
        [Highlight(0, 1000)], mode="audio",
    )
    assert plan[0].output_start_ms == 0
    assert plan[0].kind == SEGMENT_HIGHLIGHT


def test_plan_unknown_mode_raises():
    with pytest.raises(ValueError):
        plan_highlight_timeline([Highlight(0, 1000)], mode="foo")


# ---- total_output_duration_ms -----------------------------------------


def test_total_output_duration_sums_segments():
    plan = plan_highlight_timeline(
        [Highlight(0, 5_000, "A"), Highlight(10_000, 12_000, "B")],
        mode="video",
        title_interstitial_ms=2000,
        jump_interstitial_ms=2000,
    )
    # title(2) + h1(5) + jump(2) + title(2) + h2(2) = 13s
    assert total_output_duration_ms(plan) == 13_000


def test_total_output_duration_empty_plan():
    assert total_output_duration_ms([]) == 0


# ---- transcript remap -------------------------------------------------


def _video_plan(highlights):
    return plan_highlight_timeline(
        highlights,
        mode="video",
        title_interstitial_ms=2000,
        jump_interstitial_ms=2000,
    )


def test_remap_transcript_keeps_in_window_cues():
    transcript = (
        "[00:00:05] Alice: introduction.\n"
        "[00:00:12] Bob: response.\n"
        "[00:01:00] Alice: discussion.\n"
    )
    # Highlight covers 00:00:10 - 00:00:20 of source -- only Bob's line
    # falls inside.
    plan = _video_plan([Highlight(10_000, 20_000, "")])
    cues = remap_transcript_to_highlights(transcript, plan)
    assert len(cues) == 1
    assert cues[0][2] == "Bob: response."


def test_remap_transcript_offsets_into_output_timeline():
    """A transcript line at source 00:00:15 inside a highlight
    starting at source 00:00:10 should land at
    (highlight's output start + 5s) in the new timeline. With the
    2s title card preceding the highlight, that's title(2000) +
    5000 = 7000."""
    transcript = "[00:00:15] Bob: hi.\n"
    plan = _video_plan([Highlight(10_000, 20_000, "")])
    cues = remap_transcript_to_highlights(transcript, plan)
    # title interstitial = 2000ms; offset inside highlight = 5000ms.
    assert cues[0][0] == 2000 + 5000


def test_remap_transcript_drops_out_of_window_cues():
    transcript = (
        "[00:00:05] Alice: before.\n"
        "[00:01:00] Alice: between.\n"
        "[00:05:00] Alice: after.\n"
    )
    plan = _video_plan([
        Highlight(15_000, 25_000, "A"),
        Highlight(120_000, 130_000, "B"),
    ])
    cues = remap_transcript_to_highlights(transcript, plan)
    # None of the three lines falls inside either highlight.
    assert cues == []


def test_remap_transcript_empty_inputs():
    assert remap_transcript_to_highlights("", []) == []
    assert remap_transcript_to_highlights("[00:00:05] x", []) == []


# ---- format_srt -------------------------------------------------------


def test_format_srt_basic():
    cues = [(0, 2000, "first cue"), (3000, 5000, "second cue")]
    out = format_srt(cues)
    assert "1" in out
    assert "00:00:00,000 --> 00:00:02,000" in out
    assert "first cue" in out
    assert "00:00:03,000 --> 00:00:05,000" in out
    assert "second cue" in out


def test_format_srt_empty_input_returns_empty_string():
    assert format_srt([]) == ""


def test_format_srt_handles_long_durations():
    """Hours-mm-ss formatting works past the 1-hour boundary."""
    cues = [(3_661_500, 3_665_000, "after an hour")]
    out = format_srt(cues)
    assert "01:01:01,500 --> 01:01:05,000" in out


# ----------------------------------------------------------------------
# Interstitial frame rendering. Aaron flagged post-PR-#27 that the
# initial 72pt-hardcoded text was unreadable on a 1080p slide; the
# renderer now auto-fits the font size so the text fills ~90% of the
# canvas width. These tests pin the contract: short labels land on
# the maximum font size, long labels wrap (and shrink if needed),
# and the rendered frame is the right shape regardless.

import numpy as np
import pytest

from meeting_notetaker.audio.highlights_export import (
    _INTERSTITIAL_MAX_FONT_PT,
    _INTERSTITIAL_MIN_FONT_PT,
    _INTERSTITIAL_WIDTH_PCT,
    _fit_text_to_card,
    _render_interstitial_frame,
)


PIL = pytest.importorskip("PIL.ImageDraw")


def _draw_for_fit():
    from PIL import Image, ImageDraw
    return ImageDraw.Draw(Image.new("RGB", (1920, 1080), (0, 0, 0)))


def test_render_interstitial_frame_returns_1080p_rgb():
    """Output shape must match video_export's encoder expectations
    (height, width, channels) -- a wrong shape would silently
    produce a malformed MP4."""
    arr = _render_interstitial_frame("Highlight 1")
    assert arr.shape == (1080, 1920, 3)
    assert arr.dtype == np.uint8


def test_render_interstitial_frame_has_visible_text():
    """At least some pixels are non-black (the rendered text).
    Black-only output would mean the font failed silently."""
    arr = _render_interstitial_frame("Hello")
    # Sum across all three channels; non-black pixels accumulate.
    assert arr.sum() > 0
    # And the text takes a meaningful share of the canvas -- a
    # one-letter fluke wouldn't reach this floor.
    bright_pixels = (arr > 200).any(axis=2).sum()
    assert bright_pixels > 5_000, (
        f"only {bright_pixels} bright pixels; text likely too small"
    )


def test_fit_short_label_fills_card_at_very_large_size():
    """A short label like "Highlight 1" should pick the LARGEST
    font size that fits the width budget -- not the configured
    max (which might not fit a 1080p card for a string with this
    char count, so the auto-fit steps down). The criterion is
    'noticeably larger than the original 72pt' and 'actually
    fills the width budget'."""
    draw = _draw_for_fit()
    max_w = int(1920 * _INTERSTITIAL_WIDTH_PCT)
    max_h = int(1080 * 0.80)
    font, lines, size = _fit_text_to_card(draw, "Highlight 1", max_w, max_h)
    assert len(lines) == 1
    # Well above the legacy 72pt baseline (which prompted Aaron's
    # "too small" feedback). 200pt is roughly 3x larger.
    assert size >= 200, (
        f"short label landed at {size}pt -- expected >=200pt to "
        "look right on a 1080p slide"
    )
    # And it should actually fill most of the width budget, not
    # leave huge whitespace either side.
    from meeting_notetaker.audio.highlights_export import _line_width_for
    rendered_width = _line_width_for(draw, font, lines[0])
    assert rendered_width >= int(max_w * 0.70), (
        f"rendered width {rendered_width} px below 70% of budget "
        f"{max_w} px -- font sized down too aggressively"
    )


def test_fit_jump_label_fills_card_at_very_large_size():
    """The "Jumping to MM:SS" cards have a couple more characters,
    but the same expectation applies: large + width-filling. The
    19-char string at DejaVuSans-Bold ends up around 140pt -- still
    ~2x the old 72pt baseline and right at the width budget."""
    draw = _draw_for_fit()
    max_w = int(1920 * _INTERSTITIAL_WIDTH_PCT)
    max_h = int(1080 * 0.80)
    font, lines, size = _fit_text_to_card(
        draw, "Jumping to 00:30:15", max_w, max_h,
    )
    assert len(lines) == 1
    assert size >= 140
    from meeting_notetaker.audio.highlights_export import _line_width_for
    rendered_width = _line_width_for(draw, font, lines[0])
    assert rendered_width >= int(max_w * 0.70)


def test_fit_long_title_wraps_or_shrinks():
    """A long user-supplied title that wouldn't fit on one line at
    max size has to either wrap or shrink. The constraint is that
    the rendered output respects the width budget."""
    long_title = (
        "Decision on whether to roll out MDM phase 3 next quarter "
        "given the licensing changes from Informatica"
    )
    draw = _draw_for_fit()
    max_w = int(1920 * _INTERSTITIAL_WIDTH_PCT)
    max_h = int(1080 * 0.80)
    font, lines, size = _fit_text_to_card(draw, long_title, max_w, max_h)
    # Either size dropped below max, or text wrapped to multiple
    # lines (or both). One-line-at-max would mean we overflowed
    # the width budget, which is the bug.
    assert size <= _INTERSTITIAL_MAX_FONT_PT
    assert size >= _INTERSTITIAL_MIN_FONT_PT
    fits_one_line_at_max = (
        size == _INTERSTITIAL_MAX_FONT_PT and len(lines) == 1
    )
    assert not fits_one_line_at_max, (
        "long title shouldn't fit on one line at max font size"
    )


def test_fit_empty_string_renders_safely():
    """An empty title would otherwise blow up the wrap loop; the
    fallback to " " keeps the card non-empty (and the frame
    properly black)."""
    draw = _draw_for_fit()
    font, lines, size = _fit_text_to_card(
        draw, "", int(1920 * 0.9), int(1080 * 0.8),
    )
    # Lines list must be non-empty so the renderer's loop runs.
    assert lines
    # Falls back to MAX (the placeholder space fits trivially).
    assert size == _INTERSTITIAL_MAX_FONT_PT


# ---- font-load fallback regression -----------------------------------
# v0.7.0 first cut shipped with a 3-name bare-string list. On Windows
# PIL couldn't resolve "Arial.ttf" / "Helvetica.ttf" from a bare name
# in some installs and silently fell through to ImageFont.load_default,
# which ignores the size parameter -> rendered text at ~10pt regardless
# of what the auto-fit picked. Aaron caught this on first Windows run.
# These tests pin the fix: full Windows paths come first, and the
# default fallback uses Pillow 10+'s sized load_default.


def test_font_loader_returns_sized_truetype_on_dev_path():
    """In a dev env with DejaVuSans-Bold available, the loader must
    return a FreeTypeFont (sized) -- never the un-sized
    ImageFont.load_default bitmap."""
    from meeting_notetaker.audio.highlights_export import _load_interstitial_font

    font = _load_interstitial_font(size=300)
    # FreeTypeFont is the sized TTF class; ImageFont.load_default
    # returns a different class with no `size` attribute (or one
    # that ignores set values).
    klass_name = type(font).__name__
    assert klass_name == "FreeTypeFont", (
        f"loaded {klass_name} -- if this is ImageFont it'd ignore the "
        "size parameter and render tiny text (the bug we're fixing)"
    )
    # And the size sticks (load_default historically returned 10pt
    # regardless of what was passed).
    assert getattr(font, "size", 0) >= 200


def test_font_loader_falls_back_to_sized_load_default(monkeypatch):
    """When every full-path + bare-name candidate fails (the Windows
    failure mode), the loader must hit ImageFont.load_default(size=N)
    -- not the un-sized variant that silently shipped tiny text.

    Care: ImageFont.load_default(size=N) itself uses truetype()
    internally against a BytesIO payload, so we only fail the
    string-path calls and let the BytesIO ones through. That
    matches what would happen on a real Windows install where the
    file lookups fail but the Pillow-bundled font is reachable.
    """
    from PIL import ImageFont
    from meeting_notetaker.audio import highlights_export

    real_truetype = ImageFont.truetype

    def _fail_string_paths_only(*args, **kwargs):
        if args and isinstance(args[0], str):
            raise OSError("simulated font-not-found")
        return real_truetype(*args, **kwargs)
    monkeypatch.setattr(ImageFont, "truetype", _fail_string_paths_only)

    # Capture which load_default form was used. Pillow 10's
    # load_default(size=N) returns a sized TTF; the un-sized
    # load_default() returns a tiny bitmap that ignores size.
    captured = {}
    real_load_default = ImageFont.load_default

    def _spy_load_default(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = dict(kwargs)
        return real_load_default(*args, **kwargs)
    monkeypatch.setattr(ImageFont, "load_default", _spy_load_default)

    highlights_export._load_interstitial_font(size=240)
    assert "size" in captured["kwargs"], (
        "fallback called load_default() without size= -- on Pillow >=10 "
        "this is the bug: text will render at the bitmap default size, "
        "not the requested 240pt"
    )
    assert captured["kwargs"]["size"] == 240


def test_font_loader_prefers_windows_paths_on_win32(monkeypatch):
    """On Windows the loader must try the full C:/Windows/Fonts/ paths
    before bare names. Otherwise the bare-name path that historically
    failed for Aaron's install would still be tried first."""
    from PIL import ImageFont
    from meeting_notetaker.audio import highlights_export

    monkeypatch.setattr(highlights_export.sys, "platform", "win32")
    attempts: list[str] = []

    def _record_attempts(name, size):
        attempts.append(name)
        if name.startswith("C:/Windows/Fonts/"):
            # Pretend a Windows TTF loaded successfully.
            return _RealFakeFont(size)
        raise OSError("simulated miss for: " + name)
    monkeypatch.setattr(ImageFont, "truetype", _record_attempts)

    highlights_export._load_interstitial_font(size=200)
    # First successful candidate has to be a Windows full path.
    successful = next(n for n in attempts if n.startswith("C:/Windows/Fonts/"))
    assert successful, "no Windows path attempted at all"
    # Specifically: arialbd.ttf (bold) gets first crack.
    assert attempts[0] == "C:/Windows/Fonts/arialbd.ttf"


class _RealFakeFont:
    """Stand-in 'TTF-like' object for the monkey-patched truetype."""
    def __init__(self, size: int) -> None:
        self.size = size


# ---- title-card subtitle ("Recorded on ...") -------------------------
# Title interstitials carry a second line with the session's recording
# date/time below the highlight title. Tests pin both the planner-side
# propagation and the renderer-side layout.


def test_format_recorded_on_subtitle_renders_local_time(monkeypatch):
    import time
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset not available on this platform")
    monkeypatch.setenv("TZ", "Pacific/Honolulu")
    time.tzset()
    from meeting_notetaker.audio.highlights_export import (
        format_recorded_on_subtitle,
    )
    out = format_recorded_on_subtitle("2026-05-24T21:00:00Z")
    # 21:00 UTC -> 11:00 HST (UTC-10) -- same convention the rest
    # of the UI uses for session-list dates.
    assert out == "Recorded on 2026-05-24 11:00"


def test_format_recorded_on_subtitle_empty_or_garbage_returns_empty():
    from meeting_notetaker.audio.highlights_export import (
        format_recorded_on_subtitle,
    )
    assert format_recorded_on_subtitle("") == ""
    assert format_recorded_on_subtitle("not-a-date") == ""
    assert format_recorded_on_subtitle(None) == ""  # defensive


def test_plan_propagates_title_subtitle_to_title_segments_only():
    """The subtitle goes on title cards. Jump cards and highlight
    segments must NOT carry it -- the planner is the right place to
    enforce that so downstream renderers don't have to special-case."""
    h1 = Highlight(0, 5_000, "First")
    h2 = Highlight(20_000, 25_000, "Second")
    plan = plan_highlight_timeline(
        [h1, h2], mode="video",
        title_subtitle="Recorded on 2026-05-24 14:30",
    )
    titles = [s for s in plan if s.kind == SEGMENT_TITLE]
    jumps = [s for s in plan if s.kind == SEGMENT_JUMP]
    highlights = [s for s in plan if s.kind == SEGMENT_HIGHLIGHT]
    assert len(titles) == 2
    assert all(t.subtitle == "Recorded on 2026-05-24 14:30" for t in titles)
    assert all(j.subtitle == "" for j in jumps)
    assert all(h.subtitle == "" for h in highlights)


def test_plan_audio_mode_ignores_subtitle():
    """Audio export has no surface to render text on; the subtitle
    kwarg is harmless but should produce empty subtitle fields on
    the planned segments."""
    plan = plan_highlight_timeline(
        [Highlight(0, 1000)], mode="audio",
        title_subtitle="ignored",
    )
    # Audio mode has no title segments at all -- the subtitle has
    # nowhere to land.
    assert all(s.subtitle == "" for s in plan)


def test_plan_default_subtitle_is_empty():
    """Backwards-compat: callers that don't pass title_subtitle
    must get the same plan they got before the kwarg existed."""
    plan = plan_highlight_timeline(
        [Highlight(0, 1000, "x")], mode="video",
    )
    titles = [s for s in plan if s.kind == SEGMENT_TITLE]
    assert titles
    assert all(t.subtitle == "" for t in titles)


def test_render_with_subtitle_produces_more_painted_pixels_than_without():
    """Direct evidence the subtitle landed on the canvas. A two-line
    render must paint more non-black pixels than the same title
    rendered alone. We count `> 0` (any non-black) because the
    subtitle is drawn in gray (190,190,190) to read as secondary,
    not pure white -- a `> 200` threshold would miss it."""
    from meeting_notetaker.audio.highlights_export import (
        _render_interstitial_frame,
    )
    bare = _render_interstitial_frame("Highlight 1")
    with_sub = _render_interstitial_frame(
        "Highlight 1", "Recorded on 2026-05-24 14:30",
    )
    bare_painted = int((bare > 0).any(axis=2).sum())
    sub_painted = int((with_sub > 0).any(axis=2).sum())
    assert sub_painted > bare_painted, (
        f"subtitle should add pixels: bare={bare_painted}, "
        f"with_sub={sub_painted}"
    )


def test_render_with_subtitle_returns_full_canvas():
    """Shape stays at 1080p regardless of subtitle presence."""
    from meeting_notetaker.audio.highlights_export import (
        _render_interstitial_frame,
    )
    arr = _render_interstitial_frame(
        "Highlight 1", "Recorded on 2026-05-24 14:30",
    )
    assert arr.shape == (1080, 1920, 3)


def test_render_with_long_subtitle_does_not_crash():
    """Defensive: a long subtitle wraps + shrinks the same way the
    title does, but doesn't raise."""
    from meeting_notetaker.audio.highlights_export import (
        _render_interstitial_frame,
    )
    arr = _render_interstitial_frame(
        "Decision on MDM phase 3 quarterly rollout",
        "Recorded on 2026-05-24 14:30 by the platform team during "
        "the long planning meeting",
    )
    assert arr.shape == (1080, 1920, 3)


def test_render_interstitial_frame_long_title_does_not_crash():
    """End-to-end: a long title renders without raising. Detects
    regressions in the wrap+shrink interaction that would only
    surface at frame-render time."""
    long_title = (
        "An extremely long title that the user typed in a fit of "
        "enthusiasm and which absolutely cannot fit on one line "
        "at any reasonable font size for a 1080p video"
    )
    arr = _render_interstitial_frame(long_title)
    assert arr.shape == (1080, 1920, 3)
    assert arr.sum() > 0   # something painted
