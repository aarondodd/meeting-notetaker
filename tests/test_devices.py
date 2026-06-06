"""Tests for audio device name resolution.

Enumeration itself requires PortAudio; the pure logic (resolve_device_index)
is covered here.
"""
from __future__ import annotations

from meeting_notetaker.audio.devices import (
    AudioDevice,
    probe_input_device,
    resolve_device_index,
)


def _dev(idx: int, name: str) -> AudioDevice:
    return AudioDevice(
        index=idx, name=name, host_api_index=0, max_input_channels=1, sample_rate=48000
    )


def test_empty_saved_name_returns_none():
    devices = [_dev(0, "Microphone (Realtek)"), _dev(1, "Headset Mic")]
    assert resolve_device_index("", devices) is None
    assert resolve_device_index("   ", devices) is None


def test_exact_match_wins():
    devices = [
        _dev(0, "Microphone (Realtek)"),
        _dev(1, "microphone (realtek)"),
        _dev(2, "Microphone (Realtek)"),
    ]
    # The first exact match wins, not the case-insensitive duplicate.
    assert resolve_device_index("Microphone (Realtek)", devices) == 0


def test_case_insensitive_exact_when_no_exact_match():
    devices = [_dev(3, "Headset Microphone (Plantronics)")]
    assert (
        resolve_device_index("headset microphone (plantronics)", devices) == 3
    )


def test_substring_match_when_no_exact():
    devices = [
        _dev(5, "Microphone Array (Intel Smart Sound)"),
        _dev(6, "Headset Microphone (Plantronics Voyager 5200)"),
    ]
    assert resolve_device_index("Plantronics", devices) == 6
    assert resolve_device_index("Intel Smart", devices) == 5


def test_substring_match_is_case_insensitive():
    devices = [_dev(2, "Headset Microphone (Plantronics)")]
    assert resolve_device_index("PLANTRONICS", devices) == 2


def test_no_match_returns_none():
    devices = [_dev(0, "Microphone (Realtek)")]
    assert resolve_device_index("Bluetooth Headset", devices) is None


def test_empty_device_list():
    assert resolve_device_index("anything", []) is None


def test_match_against_first_when_multiple_substring_candidates():
    # Two devices both match "Microphone"; we pick the first in enumeration order.
    devices = [
        _dev(0, "Microphone (Realtek)"),
        _dev(1, "Microphone Array (Intel)"),
    ]
    assert resolve_device_index("Microphone", devices) == 0


# ---- probe_input_device --------------------------------------------------

class _FakePa:
    """Minimal pyaudio.PyAudio stand-in. Records the calls so the test
    can assert what the probe asked, and configurable to fail.
    """
    paInt16 = 8

    def __init__(self, *, fail_with: Exception | None = None,
                 result_for: dict | None = None) -> None:
        self.fail_with = fail_with
        self.result_for = result_for or {}
        self.calls: list[dict] = []

    def is_format_supported(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_with is not None:
            raise self.fail_with
        idx = kwargs.get("input_device")
        return self.result_for.get(idx, True)


def test_probe_returns_true_when_format_supported():
    pa = _FakePa()
    info = {"index": 3, "name": "Speakers", "defaultSampleRate": 48000,
            "maxInputChannels": 2}
    assert probe_input_device(pa, info) is True
    assert pa.calls == [
        {"rate": 48000, "input_device": 3, "input_channels": 2,
         "input_format": _FakePa.paInt16}
    ]


def test_probe_returns_false_on_exception():
    pa = _FakePa(fail_with=OSError("Device is invalid"))
    info = {"index": 9, "name": "Ghost Speakers", "defaultSampleRate": 48000,
            "maxInputChannels": 2}
    assert probe_input_device(pa, info) is False


def test_probe_returns_false_on_falsy_result():
    pa = _FakePa(result_for={5: False})
    info = {"index": 5, "name": "Disabled Speakers", "defaultSampleRate": 44100,
            "maxInputChannels": 2}
    assert probe_input_device(pa, info) is False


def test_probe_fills_defaults_when_metadata_missing():
    """Defunct endpoints sometimes report 0 channels / 0 rate. The probe
    falls back to safe defaults (48 kHz, mono) so it still exercises
    PortAudio rather than crashing on an arithmetic-on-zero error."""
    pa = _FakePa()
    info = {"index": 0, "name": "Weird Device"}
    assert probe_input_device(pa, info) is True
    assert pa.calls[0]["rate"] == 48000
    assert pa.calls[0]["input_channels"] == 1


def test_probe_explicit_channels_override():
    pa = _FakePa()
    info = {"index": 0, "name": "Stereo", "defaultSampleRate": 48000,
            "maxInputChannels": 2}
    probe_input_device(pa, info, channels=1)
    assert pa.calls[0]["input_channels"] == 1
