"""Export Recording: mix mic + sys into a user-chosen format.

Pins the round-trip for every supported format and the channel- /
duration-padding behavior. PyAV is required (audio/export imports
av lazily, so the module imports cleanly on hosts without it -- but
the round-trip needs the codec).
"""
from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import pytest

pytest.importorskip("av")

from meeting_notetaker.audio.export import (  # noqa: E402
    export_mixed,
    known_extensions,
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


@pytest.fixture
def sources(tmp_path):
    mic = tmp_path / "mic.wav"
    _write_sine_wav(mic, sample_rate=48000, channels=1, duration_s=0.5, freq_hz=440)
    sys_ = tmp_path / "sys.wav"
    _write_sine_wav(sys_, sample_rate=48000, channels=2, duration_s=0.5, freq_hz=330)
    return mic, sys_


def test_known_extensions_returns_supported_formats():
    exts = known_extensions()
    assert set(exts) == {".flac", ".mp3", ".m4a", ".opus", ".wav"}
    # MP3 first: the QFileDialog default filter is index 0, and MP3
    # is the safest "share with a colleague" pick (plays in every
    # default Windows player without codec packs).
    assert exts[0] == ".mp3"


@pytest.mark.parametrize("ext", [".flac", ".mp3", ".m4a", ".opus", ".wav"])
def test_export_round_trip_each_format(tmp_path, sources, ext):
    """Each supported extension produces a playable file with the
    right rate and channel count."""
    import av  # noqa: PLC0415
    mic, sys_ = sources
    dst = tmp_path / f"mixed{ext}"
    export_mixed(mic, sys_, dst)
    assert dst.exists()
    assert dst.stat().st_size > 0
    with av.open(str(dst)) as c:
        s = c.streams.audio[0]
        # Target mix is mono 48k; pin both.
        assert s.channels == 1
        assert s.rate == 48000


def test_export_unknown_format_raises(tmp_path, sources):
    mic, sys_ = sources
    with pytest.raises(ValueError, match="unsupported export format"):
        export_mixed(mic, sys_, tmp_path / "x.aiff")


def test_export_pads_shorter_side_with_silence(tmp_path):
    """Mic 2s + sys 1s -> output 2s. The shorter side rides as
    silence beyond its end; no truncation of the longer input."""
    import av  # noqa: PLC0415
    mic = tmp_path / "mic.wav"
    sys_ = tmp_path / "sys.wav"
    _write_sine_wav(mic, sample_rate=48000, channels=1, duration_s=2.0)
    _write_sine_wav(sys_, sample_rate=48000, channels=2, duration_s=1.0)
    dst = tmp_path / "mix.flac"
    export_mixed(mic, sys_, dst)
    with av.open(str(dst)) as c:
        # Duration tagged on the container in microseconds.
        duration_s = float(c.duration) / 1_000_000 if c.duration else 0
        assert duration_s >= 1.9


def test_export_handles_mic_only_session(tmp_path):
    """Mic file present, sys file absent -> exports mic only."""
    mic = tmp_path / "mic.wav"
    _write_sine_wav(mic, sample_rate=48000, channels=1, duration_s=0.25)
    sys_ = tmp_path / "sys.wav"  # never written
    dst = tmp_path / "mix.flac"
    export_mixed(mic, sys_, dst)
    assert dst.exists()
    assert dst.stat().st_size > 0


def test_export_handles_sys_only_session(tmp_path):
    """Sys file present, mic file absent -> exports sys only."""
    sys_ = tmp_path / "sys.wav"
    _write_sine_wav(sys_, sample_rate=48000, channels=2, duration_s=0.25)
    mic = tmp_path / "mic.wav"  # never written
    dst = tmp_path / "mix.flac"
    export_mixed(mic, sys_, dst)
    assert dst.exists()
    assert dst.stat().st_size > 0


def test_export_raises_when_both_sources_missing(tmp_path):
    mic = tmp_path / "mic.wav"  # never written
    sys_ = tmp_path / "sys.wav"  # never written
    with pytest.raises(ValueError, match="no source audio"):
        export_mixed(mic, sys_, tmp_path / "mix.flac")


def test_export_resamples_44100_source(tmp_path):
    """44.1 kHz source -> target 48 kHz output (the export pipeline
    resamples to a common rate up front so different source rates
    mix cleanly)."""
    import av  # noqa: PLC0415
    mic = tmp_path / "mic.wav"
    _write_sine_wav(mic, sample_rate=44100, channels=1, duration_s=0.25)
    sys_ = tmp_path / "sys.wav"
    _write_sine_wav(sys_, sample_rate=48000, channels=2, duration_s=0.25)
    dst = tmp_path / "mix.flac"
    export_mixed(mic, sys_, dst)
    with av.open(str(dst)) as c:
        assert c.streams.audio[0].rate == 48000
