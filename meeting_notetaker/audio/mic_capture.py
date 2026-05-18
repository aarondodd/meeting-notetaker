"""One-shot synchronous mic capture for short clips.

Used by the voice-enrollment dialog: opens the configured input device,
records `duration_sec` of int16 PCM into memory, returns the buffer plus
the native sample rate. Unlike `MicRecorder` (long-running Qt object that
streams to disk + the ChunkBuffer), this is a blocking helper that owns
its PortAudio instance for the duration of the call and disposes it
afterward.

PyAudio import stays inside the function so the module can be imported
in pure-Python test environments without PortAudio installed; the
enrollment dialog also avoids loading PortAudio until the user actually
clicks Record.
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np


log = logging.getLogger(__name__)


class MicCaptureError(RuntimeError):
    """Raised when PyAudio is missing, the device can't be opened, or the
    user aborts capture before the duration elapses."""


def record_mic_pcm(
    duration_sec: float,
    *,
    device_name: str = "",
    target_channels: int = 1,
    cancel_check: Optional[callable] = None,
    progress_cb: Optional[callable] = None,
) -> tuple[np.ndarray, int]:
    """Record `duration_sec` of int16 PCM from the user's mic.

    Returns (pcm, sample_rate) where pcm is shaped (samples,) for mono
    or (samples,) interleaved for stereo capture (we force mono here).

    `cancel_check`, when supplied, is called once per ~50ms chunk and may
    return True to abort early. `progress_cb` receives a float in [0, 1].
    Both run on the calling thread (the Qt event loop in the dialog's
    case) -- the recording itself is driven from the PyAudio callback.
    """
    try:
        import pyaudio  # local import; PortAudio dep
    except ImportError as exc:
        raise MicCaptureError(
            "PyAudio is not available; install it to enable voice enrollment."
        ) from exc

    # Local import to avoid pulling QObject into a pure-Python helper.
    from .mic_recorder import _pick_input_device

    pa = pyaudio.PyAudio()
    try:
        device_index, info = _pick_input_device(pa, saved_name=device_name)
        native_rate = int(info.get("defaultSampleRate", 16000)) or 16000
        frames_per_buffer = max(256, int(native_rate * 0.05))  # ~50ms chunks
        total_frames_needed = int(native_rate * duration_sec)
        captured: list[bytes] = []
        captured_frames = 0

        open_kwargs: dict = dict(
            format=pyaudio.paInt16,
            channels=target_channels,
            rate=native_rate,
            input=True,
            frames_per_buffer=frames_per_buffer,
        )
        if device_index is not None:
            open_kwargs["input_device_index"] = device_index
        try:
            stream = pa.open(**open_kwargs)
        except Exception as exc:
            raise MicCaptureError(
                f"could not open input device "
                f"({info.get('name', 'default')}): {exc}"
            ) from exc

        try:
            while captured_frames < total_frames_needed:
                if cancel_check is not None and cancel_check():
                    raise MicCaptureError("recording cancelled by user")
                want = min(frames_per_buffer, total_frames_needed - captured_frames)
                buf = stream.read(want, exception_on_overflow=False)
                captured.append(buf)
                captured_frames += want
                if progress_cb is not None:
                    progress_cb(captured_frames / total_frames_needed)
        finally:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                log.exception("mic capture stream close failed")
    finally:
        try:
            pa.terminate()
        except Exception:
            log.exception("PyAudio terminate failed in capture helper")

    if not captured:
        raise MicCaptureError("no audio captured")
    pcm = np.frombuffer(b"".join(captured), dtype=np.int16)
    return pcm, native_rate
