"""Audio device enumeration + name-based resolution.

Devices persist to config by NAME (substring) rather than index because
PyAudio enumeration order is not stable across boots -- a USB mic at
index 3 today may be index 5 tomorrow after a driver update or replug.
Name-based resolution survives that.

Resolution policy: exact match, then case-insensitive exact, then
case-insensitive substring. Empty saved name (or no match) returns None,
which signals "use system default" to the caller.

Enumeration is intentionally lazy: pyaudio + pyaudiowpatch only import
inside the list_* functions so the module can be imported in headless
test environments without PortAudio installed.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Optional


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    host_api_index: int
    max_input_channels: int
    sample_rate: int


def resolve_device_index(saved_name: str, devices: list[AudioDevice]) -> Optional[int]:
    """Match saved_name to an available device. Returns index or None.

    None means "the caller should fall back to system default". An empty
    saved_name always returns None (explicit default selection).
    """
    saved = (saved_name or "").strip()
    if not saved:
        return None
    for d in devices:
        if d.name == saved:
            return d.index
    saved_lower = saved.lower()
    for d in devices:
        if d.name.lower() == saved_lower:
            return d.index
    for d in devices:
        if saved_lower in d.name.lower():
            return d.index
    return None


def list_input_devices() -> list[AudioDevice]:
    """Enumerate PyAudio input devices. Returns [] if PyAudio is unavailable."""
    try:
        import pyaudio
    except ImportError:
        return []
    out: list[AudioDevice] = []
    pa = pyaudio.PyAudio()
    try:
        for i in range(pa.get_device_count()):
            try:
                d = pa.get_device_info_by_index(i)
            except Exception:
                continue
            max_in = int(d.get("maxInputChannels") or 0)
            if max_in <= 0:
                continue
            out.append(
                AudioDevice(
                    index=i,
                    name=str(d.get("name", "?")),
                    host_api_index=int(d.get("hostApi") or 0),
                    max_input_channels=max_in,
                    sample_rate=int(d.get("defaultSampleRate") or 0),
                )
            )
    finally:
        pa.terminate()
    return out


def probe_input_device(
    pa, device_info: dict, *, channels: Optional[int] = None,
) -> bool:
    """Return True if the device is currently openable as an input stream.

    Used to guard against locking onto a "ghost" endpoint that's still
    enumerated by Windows after a topology change (sleep/wake, USB
    replug, monitor power cycle). A substring saved-name match can pick
    a stale device whose entry survives in PortAudio's device list but
    whose underlying endpoint no longer accepts streams; the recorder
    binds successfully, then captures nothing.

    The probe uses `pa.is_format_supported` -- it is metadata-level
    (no PCM flows yet), but exercises enough of the WASAPI session
    creation path that defunct endpoints reject it. Any exception or
    falsy return means "do not use."
    """
    try:
        rate = int(device_info.get("defaultSampleRate") or 0) or 48000
        ch = channels if channels is not None else int(
            device_info.get("maxInputChannels") or 0
        ) or 1
        idx = int(device_info.get("index") or 0)
        ok = pa.is_format_supported(
            rate=rate,
            input_device=idx,
            input_channels=ch,
            input_format=getattr(pa, "paInt16", 8),
        )
        return bool(ok)
    except Exception as exc:
        log.info(
            "probe_input_device: %s rejected by is_format_supported (%s)",
            device_info.get("name", "?"), exc,
        )
        return False


def list_loopback_devices() -> list[AudioDevice]:
    """Enumerate WASAPI loopback devices via pyaudiowpatch.

    Returns [] on non-Windows or when pyaudiowpatch is unavailable.
    """
    if not sys.platform.startswith("win"):
        return []
    try:
        import pyaudiowpatch as pyaudio
    except ImportError:
        return []
    out: list[AudioDevice] = []
    pa = pyaudio.PyAudio()
    try:
        for d in pa.get_loopback_device_info_generator():
            out.append(
                AudioDevice(
                    index=int(d["index"]),
                    name=str(d.get("name", "?")),
                    host_api_index=int(d.get("hostApi") or 0),
                    max_input_channels=int(d.get("maxInputChannels") or 0),
                    sample_rate=int(d.get("defaultSampleRate") or 0),
                )
            )
    finally:
        pa.terminate()
    return out
