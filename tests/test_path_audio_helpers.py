"""session_audio_files() + has_retained_audio() path helpers.

The right-click context menu in the session list uses these to know
whether to enable Open recording / Delete recording. The implementation
walks the audio dir for any of mic|sys + .wav|.opus|.flac.
"""
from __future__ import annotations

from meeting_notetaker.utils.paths import (
    has_retained_audio,
    session_audio_dir,
    session_audio_files,
)


def test_returns_empty_when_no_audio(isolated_data_dir):
    # session_audio_dir creates the dir as a side effect; an empty
    # dir still means no recording on disk.
    session_audio_dir("session-empty")
    assert session_audio_files("session-empty") == []
    assert has_retained_audio("session-empty") is False


def test_returns_empty_when_dir_missing(isolated_data_dir):
    """A session that's never had audio at all has no audio dir.
    The helpers must not raise -- they just report no files."""
    assert session_audio_files("never-existed") == []
    assert has_retained_audio("never-existed") is False


def test_finds_wav_pair(isolated_data_dir):
    sid = "s1"
    audio_dir = session_audio_dir(sid)
    (audio_dir / "mic.wav").write_bytes(b"riff...")
    (audio_dir / "sys.wav").write_bytes(b"riff...")
    files = session_audio_files(sid)
    names = sorted(p.name for p in files)
    assert names == ["mic.wav", "sys.wav"]
    assert has_retained_audio(sid) is True


def test_finds_opus_pair_post_reencode(isolated_data_dir):
    """After the v0.6.5 re-encode, the WAVs are deleted and .opus
    files remain. The helpers must surface the compressed files."""
    sid = "s2"
    audio_dir = session_audio_dir(sid)
    (audio_dir / "mic.opus").write_bytes(b"OggS...")
    (audio_dir / "sys.opus").write_bytes(b"OggS...")
    files = session_audio_files(sid)
    names = sorted(p.name for p in files)
    assert names == ["mic.opus", "sys.opus"]


def test_finds_flac_pair(isolated_data_dir):
    sid = "s3"
    audio_dir = session_audio_dir(sid)
    (audio_dir / "mic.flac").write_bytes(b"fLaC...")
    (audio_dir / "sys.flac").write_bytes(b"fLaC...")
    files = session_audio_files(sid)
    names = sorted(p.name for p in files)
    assert names == ["mic.flac", "sys.flac"]


def test_mixed_extensions_returns_one_per_side(isolated_data_dir):
    """A session that has both mic.wav and mic.opus (transient state
    during a crash-interrupted re-encode) returns just one entry per
    side, preferring .wav. The compressed path is a sibling so this
    state is recoverable: the WAV is the source of truth."""
    sid = "s4"
    audio_dir = session_audio_dir(sid)
    (audio_dir / "mic.wav").write_bytes(b"riff...")
    (audio_dir / "mic.opus").write_bytes(b"OggS...")
    files = session_audio_files(sid)
    mic_files = [p for p in files if p.stem == "mic"]
    assert len(mic_files) == 1
    assert mic_files[0].suffix == ".wav"


def test_mic_only_session_returns_just_mic(isolated_data_dir):
    sid = "s5"
    audio_dir = session_audio_dir(sid)
    (audio_dir / "mic.wav").write_bytes(b"riff...")
    files = session_audio_files(sid)
    assert [p.name for p in files] == ["mic.wav"]
