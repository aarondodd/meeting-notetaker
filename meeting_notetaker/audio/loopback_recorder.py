"""WASAPI system-audio loopback capture (Windows-only).

Cribbed from WhisperType (Danaor/WhisperType, whispertype.py:1390-1500).
Finds the WASAPI loopback virtual device for the system default output,
opens an input stream at the device's native rate, writes the native-format
PCM to a WAV, and pushes a mono-16k downmix into the ChunkBuffer.

WASAPI stale-handle bug: PyAudioWPatch sometimes fails on the second-or-
later open() against WASAPI within a single process. Our mitigation is the
SubprocessLoopbackRecorder fallback (TODO: implement when first observed in
the wild). For v0.1 we surface the start() failure with a clear error and
let the user restart the app.
"""
from __future__ import annotations

import logging
import threading
import wave
from pathlib import Path
from typing import Optional

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal

from .chunk_buffer import ChunkBuffer
from .resample import to_mono_16k


log = logging.getLogger(__name__)


class LoopbackUnavailable(RuntimeError):
    """Raised when pyaudiowpatch can't find a loopback device on the current host."""


class LoopbackRecorder(QObject):
    error = pyqtSignal(str)
    started = pyqtSignal()
    stopped = pyqtSignal()

    def __init__(
        self,
        chunk_buffer: ChunkBuffer,
        wav_path: Path,
        *,
        source_name: str = "sys",
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.chunk_buffer = chunk_buffer
        self.wav_path = Path(wav_path)
        self.source_name = source_name
        self._pa = None
        self._stream = None
        self._wf: Optional[wave.Wave_write] = None
        self._lock = threading.Lock()
        self._is_recording = False
        self._paused = False
        self._native_rate = 48000
        self._channels = 2

    @staticmethod
    def is_available() -> bool:
        try:
            import pyaudiowpatch  # noqa: F401
            return True
        except ImportError:
            return False

    @staticmethod
    def find_loopback_device():
        """Return the default-output loopback device info dict, or None."""
        try:
            import pyaudiowpatch as pyaudio
        except ImportError:
            return None
        pa = pyaudio.PyAudio()
        try:
            wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            default_speakers = pa.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
            if default_speakers.get("isLoopbackDevice"):
                return default_speakers
            for loopback in pa.get_loopback_device_info_generator():
                if default_speakers["name"] in loopback["name"]:
                    return loopback
        finally:
            pa.terminate()
        return None

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    @property
    def is_paused(self) -> bool:
        return self._paused

    def start(self) -> None:
        import pyaudiowpatch as pyaudio
        device = self.find_loopback_device()
        if device is None:
            raise LoopbackUnavailable("no WASAPI loopback device found for default output")
        self._native_rate = int(device["defaultSampleRate"])
        self._channels = int(device["maxInputChannels"]) or 2
        self._pa = pyaudio.PyAudio()
        self.wav_path.parent.mkdir(parents=True, exist_ok=True)
        self._wf = wave.open(str(self.wav_path), "wb")
        self._wf.setnchannels(self._channels)
        self._wf.setsampwidth(2)
        self._wf.setframerate(self._native_rate)
        self._is_recording = True
        self._stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=self._channels,
            rate=self._native_rate,
            input=True,
            input_device_index=device["index"],
            frames_per_buffer=1024,
            stream_callback=self._callback,
        )
        self._stream.start_stream()
        log.info(
            "LoopbackRecorder started: %s, %d Hz, %d ch -> %s",
            device.get("name", "?"),
            self._native_rate,
            self._channels,
            self.wav_path,
        )
        self.started.emit()

    def _callback(self, in_data, frame_count, time_info, status):
        import pyaudiowpatch as pyaudio
        if not self._is_recording:
            return (None, pyaudio.paComplete)
        if self._paused:
            return (None, pyaudio.paContinue)
        try:
            if self._wf is not None:
                self._wf.writeframes(in_data)
            pcm = np.frombuffer(in_data, dtype=np.int16)
            mono16k = to_mono_16k(pcm, channels=self._channels, src_rate=self._native_rate)
            self.chunk_buffer.write(self.source_name, mono16k)
        except Exception as exc:  # pragma: no cover - audio-callback safety net
            log.exception("loopback callback failed")
            self.error.emit(str(exc))
            return (None, pyaudio.paAbort)
        return (None, pyaudio.paContinue)

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def stop(self) -> None:
        with self._lock:
            if not self._is_recording and self._stream is None:
                return
            self._is_recording = False
            try:
                if self._stream is not None:
                    self._stream.stop_stream()
                    self._stream.close()
            except Exception:
                log.exception("loopback stream close failed")
            self._stream = None
            try:
                if self._pa is not None:
                    self._pa.terminate()
            except Exception:
                log.exception("PyAudio terminate failed")
            self._pa = None
            if self._wf is not None:
                try:
                    self._wf.close()
                finally:
                    self._wf = None
        log.info("LoopbackRecorder stopped")
        self.stopped.emit()
