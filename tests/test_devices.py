"""Tests for audio device name resolution.

Enumeration itself requires PortAudio; the pure logic (resolve_device_index)
is covered here.
"""
from __future__ import annotations

from meeting_notetaker.audio.devices import AudioDevice, resolve_device_index


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
