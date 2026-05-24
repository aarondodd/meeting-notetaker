"""Audio re-encode helpers used at session retention time.

Verifies the encoder produces playable Opus / FLAC, handles 44.1 kHz
sources via internal resample (Opus's strict native-rate requirement),
and degrades gracefully when a side fails to encode.
"""
from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import pytest

# PyAV isn't installed in every dev venv; skip the whole module when
# missing so the suite stays green on hosts without it. The frozen
# Windows .exe bundles it via faster-whisper.
pytest.importorskip("av")

from meeting_notetaker.audio.encode import (  # noqa: E402
    encode_audio,
    encode_pair,
    extension_for,
)


def _write_sine_wav(
    path: Path, *, sample_rate: int, channels: int, duration_s: float,
    freq_hz: float = 440.0,
) -> None:
    n = int(sample_rate * duration_s)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        for i in range(n):
            sample = int(32767 * 0.3 * math.sin(2 * math.pi * freq_hz * i / sample_rate))
            if channels == 1:
                w.writeframes(struct.pack("<h", sample))
            else:
                w.writeframes(struct.pack("<" + "h" * channels, *([sample] * channels)))


def test_encode_opus_48k_mono_no_resample(tmp_path):
    """48 kHz mono is Opus's preferred rate -- no resample, fast path."""
    import av
    src = tmp_path / "mic.wav"
    _write_sine_wav(src, sample_rate=48000, channels=1, duration_s=0.5)
    dst = tmp_path / "mic.opus"
    encode_audio(src, dst, "opus")
    assert dst.exists()
    assert dst.stat().st_size > 0
    # Decode and pin the metadata: rate stays 48k, channel count
    # matches, duration is approximately preserved (Opus rounds to
    # the nearest 20ms frame).
    with av.open(str(dst)) as c:
        s = c.streams.audio[0]
        assert s.rate == 48000
        assert s.channels == 1


def test_encode_opus_44100_resamples_to_48000(tmp_path):
    """44.1 kHz isn't an Opus native rate; the encoder must resample
    transparently to 48 kHz."""
    import av
    src = tmp_path / "mic.wav"
    _write_sine_wav(src, sample_rate=44100, channels=1, duration_s=0.5)
    dst = tmp_path / "mic.opus"
    encode_audio(src, dst, "opus")
    with av.open(str(dst)) as c:
        s = c.streams.audio[0]
        # The output stream's declared rate is 48k because Opus stores
        # all audio internally at 48k regardless of source.
        assert s.rate == 48000


def test_encode_flac_round_trip_exact_rate(tmp_path):
    """FLAC is lossless and rate-agnostic: 44.1 kHz in -> 44.1 kHz out."""
    import av
    src = tmp_path / "mic.wav"
    _write_sine_wav(src, sample_rate=44100, channels=1, duration_s=0.25)
    dst = tmp_path / "mic.flac"
    encode_audio(src, dst, "flac")
    with av.open(str(dst)) as c:
        s = c.streams.audio[0]
        assert s.rate == 44100


def test_encode_opus_much_smaller_than_wav(tmp_path):
    """Pin the size-reduction promise: Opus should be at least 5x
    smaller than the source WAV for typical speech-range content.
    A pure sine compresses well so the actual ratio is much better,
    but 5x is a safe regression floor."""
    src = tmp_path / "mic.wav"
    _write_sine_wav(src, sample_rate=48000, channels=2, duration_s=1.0)
    src_size = src.stat().st_size
    dst = tmp_path / "mic.opus"
    encode_audio(src, dst, "opus")
    assert dst.stat().st_size * 5 < src_size, (
        f"opus output {dst.stat().st_size} not at least 5x smaller "
        f"than source {src_size}"
    )


def test_encode_audio_unknown_format_raises(tmp_path):
    src = tmp_path / "mic.wav"
    _write_sine_wav(src, sample_rate=48000, channels=1, duration_s=0.1)
    with pytest.raises(ValueError, match="unknown retain_format"):
        encode_audio(src, tmp_path / "x.mp3", "mp3")


def test_encode_pair_handles_both_sides(tmp_path):
    """encode_pair re-encodes both files when present and unlinks
    the WAVs on success."""
    mic_src = tmp_path / "mic.wav"
    sys_src = tmp_path / "sys.wav"
    _write_sine_wav(mic_src, sample_rate=48000, channels=1, duration_s=0.25)
    _write_sine_wav(sys_src, sample_rate=48000, channels=2, duration_s=0.25)
    mic_out, sys_out = encode_pair(mic_src, sys_src, "opus")
    assert mic_out is not None and mic_out.name == "mic.opus"
    assert sys_out is not None and sys_out.name == "sys.opus"
    assert mic_out.exists() and sys_out.exists()
    # Source WAVs deleted after successful encode.
    assert not mic_src.exists()
    assert not sys_src.exists()


def test_encode_pair_skips_missing_side(tmp_path):
    """A mic-only session (no sys.wav) encodes mic only; the sys side
    reports None without raising."""
    mic_src = tmp_path / "mic.wav"
    sys_src = tmp_path / "sys.wav"  # Intentionally absent.
    _write_sine_wav(mic_src, sample_rate=48000, channels=1, duration_s=0.25)
    mic_out, sys_out = encode_pair(mic_src, sys_src, "opus")
    assert mic_out is not None
    assert sys_out is None


def test_encode_pair_skips_zero_byte_side(tmp_path):
    """An empty WAV from a recording that started but never captured
    anything is a zero-byte file. Skip it."""
    mic_src = tmp_path / "mic.wav"
    sys_src = tmp_path / "sys.wav"
    _write_sine_wav(mic_src, sample_rate=48000, channels=1, duration_s=0.25)
    sys_src.touch()
    assert sys_src.stat().st_size == 0
    mic_out, sys_out = encode_pair(mic_src, sys_src, "opus")
    assert mic_out is not None
    assert sys_out is None


def test_encode_pair_wav_format_is_noop(tmp_path):
    """retain_format='wav' (the escape hatch) skips encoding entirely
    and returns the source paths."""
    mic_src = tmp_path / "mic.wav"
    sys_src = tmp_path / "sys.wav"
    _write_sine_wav(mic_src, sample_rate=48000, channels=1, duration_s=0.25)
    _write_sine_wav(sys_src, sample_rate=48000, channels=2, duration_s=0.25)
    mic_out, sys_out = encode_pair(mic_src, sys_src, "wav")
    assert mic_out == mic_src and sys_out == sys_src
    # Source files left in place.
    assert mic_src.exists() and sys_src.exists()


def test_extension_for_known_formats():
    assert extension_for("opus") == ".opus"
    assert extension_for("flac") == ".flac"
    assert extension_for("wav") == ".wav"


def test_extension_for_unknown_falls_back_to_wav():
    """Bad input doesn't raise -- this helper is also used by code that
    can't easily validate its input, so the safe fallback is .wav so
    nothing claims to be a format it isn't."""
    assert extension_for("mp3") == ".wav"
