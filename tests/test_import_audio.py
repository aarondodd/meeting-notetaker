"""Tests for the audio-file import decoder (#88).

Drives PyAV against an on-the-fly generated WAV fixture so the test
runs anywhere PyAV is installed (already a project dep). MP3 / M4A /
Opus / FLAC paths share the exact same code path -- PyAV abstracts
the codec away. The pure-format exotica is covered by integration
tests gated behind the `audio` marker.
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from meeting_notetaker.audio.import_audio import (
    CANONICAL_CHANNELS,
    CANONICAL_SAMPLE_RATE,
    CANONICAL_SAMPLE_WIDTH,
    SUPPORTED_EXTENSIONS,
    AudioImportError,
    AudioImportResult,
    decode_to_canonical_wav,
    describe_source,
    is_supported_extension,
)


# ---- helpers -------------------------------------------------------------

def _write_test_wav(
    path: Path,
    *,
    duration_sec: float = 1.0,
    sample_rate: int = 48000,
    channels: int = 2,
    freq_hz: float = 440.0,
) -> Path:
    """Write a sine-wave WAV the decoder can consume. Default shape
    (48 kHz stereo) exercises the resample + downmix path."""
    n = int(duration_sec * sample_rate)
    t = np.arange(n) / sample_rate
    sine = (np.sin(2 * np.pi * freq_hz * t) * 16000).astype(np.int16)
    if channels == 1:
        pcm = sine
    else:
        # Stereo: pack L/R samples interleaved.
        stereo = np.zeros(n * channels, dtype=np.int16)
        for c in range(channels):
            stereo[c::channels] = sine
        pcm = stereo
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
    return path


def _read_wav_meta(path: Path) -> dict:
    with wave.open(str(path), "rb") as rf:
        return {
            "rate": rf.getframerate(),
            "channels": rf.getnchannels(),
            "sample_width": rf.getsampwidth(),
            "frames": rf.getnframes(),
        }


# ---- is_supported_extension ---------------------------------------------

def test_supported_extensions_recognized():
    for ext in (".wav", ".mp3", ".m4a", ".opus", ".flac", ".mp4"):
        assert is_supported_extension(Path(f"foo{ext}"))
        assert is_supported_extension(Path(f"foo{ext.upper()}"))


def test_unsupported_extensions_rejected():
    for ext in (".txt", ".pdf", ".docx", ".png", ".docm"):
        assert not is_supported_extension(Path(f"foo{ext}"))


def test_extension_allowlist_includes_voice_memo_formats():
    """iPhone / Android voice recordings are M4A or 3GP/AMR; both must
    work because they're the most common user inputs."""
    assert ".m4a" in SUPPORTED_EXTENSIONS
    assert ".amr" in SUPPORTED_EXTENSIONS


# ---- decode_to_canonical_wav: shape pin ---------------------------------

def test_decode_writes_canonical_16k_mono_int16(tmp_path):
    src = _write_test_wav(tmp_path / "sine.wav", duration_sec=2.0)
    dst = tmp_path / "out.wav"
    result = decode_to_canonical_wav(src, dst)
    meta = _read_wav_meta(dst)
    assert meta["rate"] == CANONICAL_SAMPLE_RATE == 16000
    assert meta["channels"] == CANONICAL_CHANNELS == 1
    assert meta["sample_width"] == CANONICAL_SAMPLE_WIDTH == 2
    # ~2 seconds * 16 kHz = ~32000 frames; allow modest resampler slop.
    assert 31500 <= meta["frames"] <= 32500


def test_decode_returns_audio_import_result(tmp_path):
    src = _write_test_wav(tmp_path / "sine.wav", duration_sec=1.5)
    dst = tmp_path / "out.wav"
    result = decode_to_canonical_wav(src, dst)
    assert isinstance(result, AudioImportResult)
    assert result.src_path == src
    assert result.dst_path == dst
    assert result.src_sample_rate == 48000
    assert result.src_channels == 2
    assert 1.4 < result.duration_seconds < 1.6
    assert result.output_frames > 0
    assert result.duration_str in ("0:01", "0:02")  # rounded


def test_decode_progress_callback_fires_to_one(tmp_path):
    src = _write_test_wav(tmp_path / "sine.wav", duration_sec=1.0)
    dst = tmp_path / "out.wav"
    seen: list[float] = []
    decode_to_canonical_wav(src, dst, progress=seen.append)
    assert seen, "progress callback never fired"
    assert seen[-1] == pytest.approx(1.0)
    assert all(0.0 <= p <= 1.0 for p in seen)


def test_decode_creates_parent_directory(tmp_path):
    src = _write_test_wav(tmp_path / "sine.wav", duration_sec=0.5)
    dst = tmp_path / "nested" / "audio" / "out.wav"
    decode_to_canonical_wav(src, dst)
    assert dst.exists()


def test_decode_overwrites_existing_destination(tmp_path):
    src = _write_test_wav(tmp_path / "sine.wav", duration_sec=0.5)
    dst = tmp_path / "out.wav"
    dst.write_bytes(b"junk that should be gone after decode")
    decode_to_canonical_wav(src, dst)
    # The new file is a real WAV, not the junk.
    meta = _read_wav_meta(dst)
    assert meta["rate"] == CANONICAL_SAMPLE_RATE


# ---- cancellation -------------------------------------------------------

def test_decode_respects_should_cancel(tmp_path):
    src = _write_test_wav(tmp_path / "sine.wav", duration_sec=2.0)
    dst = tmp_path / "out.wav"
    with pytest.raises(AudioImportError) as exc_info:
        decode_to_canonical_wav(
            src, dst,
            should_cancel=lambda: True,
        )
    assert "cancel" in exc_info.value.reason.lower()
    assert not dst.exists(), "partial WAV must be cleaned up on cancel"


# ---- error paths --------------------------------------------------------

def test_decode_missing_file_raises(tmp_path):
    with pytest.raises(AudioImportError) as exc_info:
        decode_to_canonical_wav(tmp_path / "nope.wav", tmp_path / "out.wav")
    assert "not found" in exc_info.value.reason.lower()


def test_decode_directory_input_raises(tmp_path):
    with pytest.raises(AudioImportError) as exc_info:
        decode_to_canonical_wav(tmp_path, tmp_path / "out.wav")
    assert "not a file" in exc_info.value.reason.lower()


def test_decode_corrupted_input_raises_user_readable(tmp_path):
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"not really a wav file")
    with pytest.raises(AudioImportError) as exc_info:
        decode_to_canonical_wav(junk, tmp_path / "out.wav")
    # Could be either codec-detect failure or zero-frames after decode;
    # in both cases we want the user to see something they can act on.
    msg = exc_info.value.reason.lower()
    assert any(s in msg for s in (
        "unsupported", "corrupted", "decode failed", "zero frames",
    ))


# ---- describe_source ----------------------------------------------------

def test_describe_source_returns_metadata(tmp_path):
    src = _write_test_wav(tmp_path / "sine.wav", duration_sec=1.5)
    info = describe_source(src)
    assert info.get("codec") in ("pcm_s16le", "pcm_s16be", "wav")
    assert info.get("sample_rate") == 48000
    assert info.get("channels") == 2
    assert 1.4 < info.get("duration_seconds", 0) < 1.6
    assert info.get("file_size_bytes", 0) > 0


def test_describe_source_returns_empty_dict_on_missing(tmp_path):
    info = describe_source(tmp_path / "nope.wav")
    # Either {} (PyAV missing) or {"error": "..."} (PyAV present but
    # couldn't open). Both indicate "no metadata available."
    assert "codec" not in info


# ---- duration formatting ------------------------------------------------

def test_duration_str_under_one_minute():
    r = AudioImportResult(
        src_path=Path("a"), dst_path=Path("b"),
        src_codec="x", src_sample_rate=48000, src_channels=2,
        duration_seconds=42.0, output_frames=0,
    )
    assert r.duration_str == "0:42"


def test_duration_str_under_one_hour():
    r = AudioImportResult(
        src_path=Path("a"), dst_path=Path("b"),
        src_codec="x", src_sample_rate=48000, src_channels=2,
        duration_seconds=14 * 60 + 22, output_frames=0,
    )
    assert r.duration_str == "14:22"


def test_duration_str_over_one_hour():
    r = AudioImportResult(
        src_path=Path("a"), dst_path=Path("b"),
        src_codec="x", src_sample_rate=48000, src_channels=2,
        duration_seconds=2 * 3600 + 5 * 60 + 7, output_frames=0,
    )
    assert r.duration_str == "2:05:07"
