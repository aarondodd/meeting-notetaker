"""Structured-format parsers for transcript import (v0.7.8).

Covers WebVTT / SubRip (.srt) / Whisper JSON parsing, the
auto-detect heuristic, and the renderer that produces the
``[HH:MM:SS] Label: text`` shape the player keys playback sync
off of. Pure-Python; no Qt.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from meeting_notetaker.integrations.transcript_import import (
    ALL_FORMATS,
    FORMAT_PLAIN,
    FORMAT_SRT,
    FORMAT_VTT,
    FORMAT_WHISPER_JSON,
    TranscriptCue,
    TranscriptImportError,
    detect_format,
    parse_srt,
    parse_vtt,
    parse_whisper_json,
    segments_to_transcript_md,
)


# ---- VTT --------------------------------------------------------------


def test_parse_vtt_with_voice_tags_extracts_speaker():
    vtt = (
        "WEBVTT\n"
        "\n"
        "00:00:00.000 --> 00:00:03.500\n"
        "<v Jane Smith>Welcome everyone.</v>\n"
        "\n"
        "00:00:03.500 --> 00:00:08.000\n"
        "<v Aaron Dodd>Sounds good.</v>\n"
    )
    cues = parse_vtt(vtt)
    assert len(cues) == 2
    assert cues[0].speaker == "Jane Smith"
    assert cues[0].text == "Welcome everyone."
    assert cues[0].start_seconds == 0.0
    assert cues[0].end_seconds == 3.5
    assert cues[1].speaker == "Aaron Dodd"
    assert cues[1].text == "Sounds good."


def test_parse_vtt_voice_tag_without_closing_tag_works():
    """The spec lets the closing </v> be omitted; the body runs to
    the end of the cue. Some Teams exports skip the close tag."""
    vtt = (
        "WEBVTT\n"
        "\n"
        "00:00:00.000 --> 00:00:03.000\n"
        "<v Speaker A>Body without close tag\n"
    )
    cues = parse_vtt(vtt)
    assert len(cues) == 1
    assert cues[0].speaker == "Speaker A"
    assert cues[0].text == "Body without close tag"


def test_parse_vtt_falls_back_to_name_prefix_when_no_voice_tag():
    """Cue body like "Jane: text" carries the speaker inline; the
    parser pulls it out as canonical attribution."""
    vtt = (
        "WEBVTT\n"
        "\n"
        "00:00:00.000 --> 00:00:03.000\n"
        "Jane Smith: Welcome everyone\n"
    )
    cues = parse_vtt(vtt)
    assert len(cues) == 1
    assert cues[0].speaker == "Jane Smith"
    assert cues[0].text == "Welcome everyone"


def test_parse_vtt_skips_note_and_style_blocks():
    """NOTE / STYLE / REGION run until the next blank line and
    must not contribute cues."""
    vtt = (
        "WEBVTT\n"
        "\n"
        "NOTE\n"
        "Some boilerplate comment\n"
        "\n"
        "STYLE\n"
        "::cue { color: red; }\n"
        "\n"
        "00:00:00.000 --> 00:00:03.000\n"
        "Actual cue body\n"
    )
    cues = parse_vtt(vtt)
    assert len(cues) == 1
    assert cues[0].text == "Actual cue body"


def test_parse_vtt_skips_cue_identifier_line():
    """The optional cue identifier (a line before the timing) must
    not be confused with the cue body."""
    vtt = (
        "WEBVTT\n"
        "\n"
        "cue-1\n"
        "00:00:00.000 --> 00:00:03.000\n"
        "Hello\n"
    )
    cues = parse_vtt(vtt)
    assert len(cues) == 1
    assert cues[0].text == "Hello"


def test_parse_vtt_strips_inline_styling_tags():
    """<i> <b> <c.classname> etc. are visual hints; the spoken
    text shouldn't carry them through to the transcript."""
    vtt = (
        "WEBVTT\n"
        "\n"
        "00:00:00.000 --> 00:00:03.000\n"
        "<v Speaker>Hello <i>world</i> and <c.loud>everyone</c></v>\n"
    )
    cues = parse_vtt(vtt)
    assert cues[0].text == "Hello world and everyone"


def test_parse_vtt_joins_multiline_cue_body():
    vtt = (
        "WEBVTT\n"
        "\n"
        "00:00:00.000 --> 00:00:03.000\n"
        "<v Jane>First line\n"
        "second line</v>\n"
    )
    cues = parse_vtt(vtt)
    assert cues[0].text == "First line second line"


def test_parse_vtt_handles_hour_timestamps():
    """Cues at 1+ hours have an extra HH: group in the timing line."""
    vtt = (
        "WEBVTT\n"
        "\n"
        "01:00:00.000 --> 01:00:03.000\n"
        "Late cue\n"
    )
    cues = parse_vtt(vtt)
    assert cues[0].start_seconds == 3600.0
    assert cues[0].end_seconds == 3603.0


def test_parse_vtt_empty_returns_empty():
    assert parse_vtt("") == []
    assert parse_vtt("WEBVTT\n\n") == []


def test_parse_vtt_handles_crlf_line_endings():
    """Windows-saved files commonly use CRLF; the parser must
    handle them without leaving \\r in the rendered body."""
    vtt = (
        "WEBVTT\r\n"
        "\r\n"
        "00:00:00.000 --> 00:00:03.000\r\n"
        "Body\r\n"
    )
    cues = parse_vtt(vtt)
    assert len(cues) == 1
    assert "\r" not in cues[0].text


# ---- SRT --------------------------------------------------------------


def test_parse_srt_basic_three_block_form():
    srt = (
        "1\n"
        "00:00:00,000 --> 00:00:03,500\n"
        "Jane Smith: Welcome\n"
        "\n"
        "2\n"
        "00:00:03,500 --> 00:00:08,000\n"
        "Aaron Dodd: Sounds good\n"
    )
    cues = parse_srt(srt)
    assert len(cues) == 2
    assert cues[0].start_seconds == 0.0
    assert cues[0].end_seconds == 3.5
    assert cues[0].speaker == "Jane Smith"
    assert cues[0].text == "Welcome"
    assert cues[1].speaker == "Aaron Dodd"


def test_parse_srt_handles_missing_cue_numbers():
    """Some exports omit the cue number. Parser tolerates either."""
    srt = (
        "00:00:00,000 --> 00:00:03,000\n"
        "Body one\n"
        "\n"
        "00:00:03,000 --> 00:00:06,000\n"
        "Body two\n"
    )
    cues = parse_srt(srt)
    assert len(cues) == 2
    assert cues[0].text == "Body one"
    assert cues[1].text == "Body two"


def test_parse_srt_uses_comma_decimal_in_timings():
    """SRT uses comma decimals where VTT uses periods. The parsed
    fractional second must come out correctly."""
    srt = (
        "1\n"
        "00:00:00,500 --> 00:00:01,750\n"
        "Body\n"
    )
    cues = parse_srt(srt)
    assert cues[0].start_seconds == pytest.approx(0.5)
    assert cues[0].end_seconds == pytest.approx(1.75)


def test_parse_srt_empty_returns_empty():
    assert parse_srt("") == []


# ---- Whisper JSON -----------------------------------------------------


def test_parse_whisper_json_extracts_segments():
    body = json.dumps({
        "segments": [
            {"start": 0.0, "end": 3.5, "text": "Welcome everyone"},
            {"start": 3.5, "end": 7.0, "text": "Thanks for joining"},
        ],
        "language": "en",
    })
    cues = parse_whisper_json(body)
    assert len(cues) == 2
    assert cues[0].text == "Welcome everyone"
    assert cues[0].speaker == ""  # vanilla Whisper carries no speaker
    assert cues[1].start_seconds == 3.5


def test_parse_whisper_json_respects_optional_speaker_field():
    """Some Whisper forks emit per-segment 'speaker' fields. When
    present, the parser threads them through."""
    body = json.dumps({
        "segments": [
            {"start": 0.0, "end": 3.0, "text": "Hello", "speaker": "Spk-1"},
        ],
    })
    cues = parse_whisper_json(body)
    assert cues[0].speaker == "Spk-1"


def test_parse_whisper_json_raises_on_bad_json():
    with pytest.raises(TranscriptImportError) as exc_info:
        parse_whisper_json("not json at all")
    assert "Whisper JSON" in exc_info.value.reason


def test_parse_whisper_json_raises_on_missing_segments_key():
    with pytest.raises(TranscriptImportError) as exc_info:
        parse_whisper_json('{"something": "else"}')
    assert "segments" in exc_info.value.reason


def test_parse_whisper_json_skips_empty_text_segments():
    body = json.dumps({
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "  "},
            {"start": 1.0, "end": 2.0, "text": "actual content"},
        ],
    })
    cues = parse_whisper_json(body)
    assert len(cues) == 1
    assert cues[0].text == "actual content"


# ---- detect_format ----------------------------------------------------


def test_detect_format_returns_one_of_known_constants():
    """Defensive: any sample we throw at it should resolve to a
    constant the dialog's format dropdown handles."""
    samples = [
        ("plain body, no shape", None),
        ("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nx\n", None),
        ('{"segments":[]}', None),
        ("1\n00:00:00,000 --> 00:00:01,000\nx\n", None),
    ]
    for body, name in samples:
        fmt = detect_format(body, filename=name)
        assert fmt in ALL_FORMATS


def test_detect_format_extension_wins_when_supplied():
    """The extension is the strongest hint; body sniff is only the
    fallback when the path is None."""
    body = "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nx\n"
    assert detect_format(body, filename=Path("foo.srt")) == FORMAT_SRT
    assert detect_format(body, filename=Path("foo.json")) == FORMAT_WHISPER_JSON
    assert detect_format(body, filename=Path("foo.vtt")) == FORMAT_VTT


def test_detect_format_content_sniffs_webvtt_header():
    """A .txt file whose body starts with WEBVTT is actually VTT;
    the content sniff catches this when no path is supplied."""
    body = "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nx\n"
    assert detect_format(body) == FORMAT_VTT


def test_detect_format_content_sniffs_whisper_json_braces():
    assert detect_format('{"segments": [{"start": 0}]}') == FORMAT_WHISPER_JSON


def test_detect_format_content_sniffs_srt_timing_in_first_lines():
    """Cue-numberless SRT (timing on line 1) is still detectable."""
    body = "00:00:00,500 --> 00:00:01,750\nBody\n"
    assert detect_format(body) == FORMAT_SRT


def test_detect_format_falls_back_to_plain():
    assert detect_format("just prose, no transcript shape") == FORMAT_PLAIN


# ---- renderer --------------------------------------------------------


def test_segments_to_transcript_md_produces_player_friendly_shape():
    """The output must match the regex the player parses
    (session_view.py _TIMESTAMP_RE: ^\\[(\\d+):(\\d{2}):(\\d{2})\\])."""
    import re

    cues = [
        TranscriptCue(0.0, 3.5, "Jane Smith", "Welcome everyone"),
        TranscriptCue(3.5, 8.0, "Aaron Dodd", "Sounds good"),
    ]
    out = segments_to_transcript_md(cues)
    line_re = re.compile(r"^\[(\d+):(\d{2}):(\d{2})\] ([^:]+): (.+)$")
    for line in out.strip().splitlines():
        assert line_re.match(line), f"line does not match player regex: {line!r}"


def test_segments_to_transcript_md_uses_default_speaker_when_empty():
    """Whisper JSON without diarization emits cues with empty
    speaker; the renderer fills in the default label."""
    cues = [TranscriptCue(0.0, 3.0, "", "Welcome everyone")]
    out = segments_to_transcript_md(cues, default_speaker="Speaker")
    assert "[00:00:00] Speaker: Welcome everyone" in out


def test_segments_to_transcript_md_empty_cues_returns_empty_string():
    assert segments_to_transcript_md([]) == ""


def test_segments_to_transcript_md_handles_hour_timestamps():
    cues = [TranscriptCue(3661.0, 3665.0, "Spk", "Late")]
    out = segments_to_transcript_md(cues)
    # 3661s = 1h 1m 1s; formatted as [01:01:01].
    assert "[01:01:01] Spk: Late" in out


def test_segments_to_transcript_md_negative_seconds_clamped_to_zero():
    """Defensive: a malformed source could emit negative seconds;
    clamp rather than render a malformed timestamp."""
    cues = [TranscriptCue(-5.0, 0.0, "Spk", "Body")]
    out = segments_to_transcript_md(cues)
    assert "[00:00:00] Spk: Body" in out


# ---- end-to-end parser -> render integration --------------------------


def test_vtt_round_trip_produces_player_lines():
    """The whole pipeline: VTT input -> parse -> render to the
    player-friendly shape. Regression pin: a VTT file should
    become a transcript that unlocks click-to-seek + position
    highlighting after audio is added."""
    vtt = (
        "WEBVTT\n"
        "\n"
        "00:00:00.000 --> 00:00:03.500\n"
        "<v Jane>Hello</v>\n"
        "\n"
        "00:00:03.500 --> 00:00:08.000\n"
        "<v Aaron>Hi</v>\n"
    )
    out = segments_to_transcript_md(parse_vtt(vtt))
    assert "[00:00:00] Jane: Hello" in out
    assert "[00:00:03] Aaron: Hi" in out


def test_srt_round_trip_produces_player_lines():
    srt = (
        "1\n"
        "00:00:00,000 --> 00:00:03,500\n"
        "Jane Smith: Welcome\n"
        "\n"
        "2\n"
        "00:00:03,500 --> 00:00:08,000\n"
        "Aaron Dodd: Sounds good\n"
    )
    out = segments_to_transcript_md(parse_srt(srt))
    assert "[00:00:00] Jane Smith: Welcome" in out
    assert "[00:00:03] Aaron Dodd: Sounds good" in out


def test_whisper_json_round_trip_uses_default_speaker():
    body = json.dumps({
        "segments": [
            {"start": 0.0, "end": 3.5, "text": "Welcome"},
            {"start": 3.5, "end": 8.0, "text": "Thanks"},
        ],
    })
    out = segments_to_transcript_md(
        parse_whisper_json(body), default_speaker="Speaker",
    )
    assert "[00:00:00] Speaker: Welcome" in out
    assert "[00:00:03] Speaker: Thanks" in out
