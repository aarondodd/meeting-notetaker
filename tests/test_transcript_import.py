"""Tests for the transcript-import helper (#80).

Pure-Python -- no Qt, no app context. Covers:
  - load_transcript_from_file: .txt / .md / .docx (mocked) / unsupported
  - normalize_text: Teams chrome stripping, blank collapsing, speaker
    line reassembly, .docx + web-paste formats
  - detect_speakers: ordering + dedupe + edge cases
  - apply_speaker_map: rewrite anchored at line start, leaves prose alone
  - iter_speakers_with_counts: counts in descending order
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from meeting_notetaker.integrations.transcript_import import (
    SpeakerOccurrence,
    TranscriptImportError,
    apply_speaker_map,
    detect_speakers,
    iter_speakers_with_counts,
    load_transcript_from_file,
    normalize_text,
)


# ---- load_transcript_from_file -------------------------------------------

def test_load_txt(tmp_path):
    p = tmp_path / "t.txt"
    p.write_text("hello\nworld\n", encoding="utf-8")
    assert load_transcript_from_file(p) == "hello\nworld\n"


def test_load_md(tmp_path):
    p = tmp_path / "t.md"
    p.write_text("# h\nbody", encoding="utf-8")
    assert load_transcript_from_file(p) == "# h\nbody"


def test_load_falls_back_to_latin1_on_bad_utf8(tmp_path):
    p = tmp_path / "t.txt"
    p.write_bytes(b"Aaron \xe9 Smith")  # latin-1 e-acute
    out = load_transcript_from_file(p)
    assert "Aaron" in out and "Smith" in out


def test_load_missing_file(tmp_path):
    with pytest.raises(TranscriptImportError) as exc_info:
        load_transcript_from_file(tmp_path / "nope.txt")
    assert "File not found" in exc_info.value.reason


def test_load_unsupported_extension(tmp_path):
    p = tmp_path / "t.pdf"
    p.write_bytes(b"%PDF-1.4 not really")
    with pytest.raises(TranscriptImportError) as exc_info:
        load_transcript_from_file(p)
    assert ".pdf" in exc_info.value.reason
    assert exc_info.value.suggest_paste is True


def test_load_docx_missing_dep_surfaces_paste_hint(tmp_path):
    """If python-docx isn't installed (likely in test env), the error
    message must steer the user to the paste path rather than dying."""
    p = tmp_path / "t.docx"
    p.write_bytes(b"PK\x03\x04 -- fake but the importer never opens it")
    try:
        with patch.dict("sys.modules", {"docx": None}):
            with pytest.raises(TranscriptImportError) as exc_info:
                load_transcript_from_file(p)
        assert "python-docx" in exc_info.value.reason
        assert "Paste" in exc_info.value.reason
        assert exc_info.value.suggest_paste is True
    finally:
        # patch.dict already restored; nothing else needed.
        pass


# ---- normalize_text ------------------------------------------------------

def test_normalize_strips_teams_banners():
    body = (
        "Started transcription\n"
        "May 14, 2026, 10:00 AM\n"
        "\n"
        "Jane: hello there\n"
        "View original meeting\n"
    )
    out = normalize_text(body)
    assert "Started transcription" not in out
    assert "View original meeting" not in out
    assert "May 14, 2026" not in out
    assert "Jane: hello there" in out


def test_normalize_folds_web_paste_speaker_timestamp():
    """Web copy/paste form: speaker / bare timestamp / body across 3 lines.
    The normalizer should fold the timestamp away and tack a colon onto
    the speaker line if it's missing."""
    body = (
        "Jane Smith\n"
        "0:00:01\n"
        "All right, let's get started.\n"
        "Aaron Dodd\n"
        "0:00:08\n"
        "Sounds good.\n"
    )
    out = normalize_text(body)
    assert "0:00:01" not in out
    assert "0:00:08" not in out
    assert "Jane Smith:" in out
    assert "Aaron Dodd:" in out
    assert "All right" in out
    assert "Sounds good" in out


def test_normalize_strips_trailing_timestamp_from_docx_speaker_line():
    """.docx export form: "Name 0:00:01.234" on one line, body on next.
    The trailing timestamp should be cleaned off and a colon attached."""
    body = (
        "Jane Smith   0:00:01.234\n"
        "All right, let's get started.\n"
        "Aaron Dodd   0:00:08.117\n"
        "Sounds good.\n"
    )
    out = normalize_text(body)
    assert "0:00:01" not in out
    assert "Jane Smith:" in out
    assert "Aaron Dodd:" in out
    assert "All right" in out


def test_normalize_collapses_multiple_blank_lines():
    body = "Jane: hi\n\n\n\n\nAaron: hello\n"
    out = normalize_text(body)
    # At most one blank line between paragraphs.
    assert "\n\n\n" not in out


def test_normalize_handles_crlf():
    body = "Jane: hi\r\nAaron: hello\r\n"
    out = normalize_text(body)
    assert "\r" not in out
    assert "Jane: hi" in out
    assert "Aaron: hello" in out


def test_normalize_empty_input():
    assert normalize_text("") == ""


def test_normalize_strip_off_preserves_chrome():
    body = "Started transcription\nJane: hi\n"
    out = normalize_text(body, strip_teams_chrome=False)
    assert "Started transcription" in out


def test_normalize_does_not_eat_real_content_that_resembles_chrome():
    """A speaker line that mentions one of the banner words verbatim
    should survive. The banner check is anchored at the start of the
    stripped line, so 'I started transcription late' doesn't match."""
    body = "Jane: I started transcription late today\n"
    out = normalize_text(body)
    assert "Jane: I started transcription late today" in out


def test_normalize_trims_leading_and_trailing_blanks():
    body = "\n\n\nJane: hi\n\n\n"
    out = normalize_text(body)
    assert out.startswith("Jane:")
    assert out.endswith("\n")


def test_normalize_bare_timestamp_line_dropped_when_no_preceding_label():
    """A bare timestamp not preceded by a plausible speaker line should
    just be dropped -- there's no point keeping it without context."""
    body = "0:01:23\nJust a body line here\n"
    out = normalize_text(body)
    assert "0:01:23" not in out
    assert "body line" in out


# ---- detect_speakers -----------------------------------------------------

def test_detect_speakers_first_appearance_order():
    body = (
        "Jane: hi\n"
        "Aaron: hello\n"
        "Jane: how are you\n"
        "Chris: doing well\n"
    )
    speakers = detect_speakers(body)
    assert [s.name for s in speakers] == ["Jane", "Aaron", "Chris"]


def test_detect_speakers_double_barreled_name():
    body = "Jane Smith-Jones: hi\nAaron O'Connor: hello\n"
    names = [s.name for s in detect_speakers(body)]
    assert "Jane Smith-Jones" in names
    assert "Aaron O'Connor" in names


def test_detect_speakers_does_not_match_prose_with_colon():
    """A prose line ending with a colon ("The rule was:") should not
    be detected as a speaker -- the regex is anchored on the line start
    AND requires a Capital-first label up to ~60 chars."""
    body = (
        "Jane: the rule was: never deploy on Friday\n"
        "Aaron: agreed\n"
    )
    names = [s.name for s in detect_speakers(body)]
    assert names == ["Jane", "Aaron"]


def test_detect_speakers_returns_dataclass_with_index():
    body = "Jane: hi\nAaron: hello\n"
    speakers = detect_speakers(body)
    assert all(isinstance(s, SpeakerOccurrence) for s in speakers)
    assert speakers[0].first_index < speakers[1].first_index


def test_detect_speakers_empty():
    assert detect_speakers("") == []


# ---- apply_speaker_map ---------------------------------------------------

def test_apply_speaker_map_rewrites_prefix():
    body = "Jane: hi\nAaron: hello\n"
    out = apply_speaker_map(body, {"Jane": "Jane Smith"})
    assert "Jane Smith: hi" in out
    assert "Aaron: hello" in out


def test_apply_speaker_map_ignores_empty_values():
    body = "Jane: hi\n"
    out = apply_speaker_map(body, {"Jane": ""})
    assert out == body


def test_apply_speaker_map_leaves_prose_mention_alone():
    body = "Jane: I just spoke with Aaron about it\nAaron: what about it\n"
    out = apply_speaker_map(body, {"Aaron": "Aaron Dodd"})
    # The Aaron at the start of line 2 is rewritten; the Aaron in
    # the middle of Jane's line is left alone.
    assert "Aaron Dodd: what about it" in out
    assert "I just spoke with Aaron about it" in out


def test_apply_speaker_map_noop_when_mapping_empty():
    body = "Jane: hi\n"
    assert apply_speaker_map(body, {}) == body


def test_apply_speaker_map_skips_identity_mapping():
    body = "Jane: hi\n"
    assert apply_speaker_map(body, {"Jane": "Jane"}) == body


# ---- iter_speakers_with_counts ------------------------------------------

def test_iter_speakers_with_counts_descending():
    body = (
        "Jane: a\n"
        "Aaron: b\n"
        "Jane: c\n"
        "Jane: d\n"
        "Aaron: e\n"
    )
    pairs = list(iter_speakers_with_counts(body))
    assert pairs[0] == ("Jane", 3)
    assert pairs[1] == ("Aaron", 2)


def test_iter_speakers_with_counts_alphabetical_tie_break():
    body = "Aaron: a\nJane: b\n"
    pairs = list(iter_speakers_with_counts(body))
    # Both 1; alphabetical tiebreak -> Aaron first.
    assert pairs == [("Aaron", 1), ("Jane", 1)]
