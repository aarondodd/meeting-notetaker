"""Microphone capture via PyAudio.

Opens the system default input device at its native rate, writes raw int16
PCM to <session_audio_dir>/mic.wav (the source of truth), and pushes a
mono-16k downmix into the ChunkBuffer for live transcription.

The PyAudio import is local to start() so that this module can be imported
in test environments that don't have PortAudio installed.
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


class MicRecorder(QObject):
    error = pyqtSignal(str)
    started = pyqtSignal()
    stopped = pyqtSignal()

    def __init__(
        self,
        chunk_buffer: ChunkBuffer,
        wav_path: Path,
        *,
        source_name: str = "mic",
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
        self._native_rate = 16000
        self._channels = 1
        self._device_index: Optional[int] = None

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    @property
    def is_paused(self) -> bool:
        return self._paused

    def start(self) -> None:
        import pyaudio  # local import; PortAudio dep
        self._pa = pyaudio.PyAudio()
        device_index, info = _pick_input_device(self._pa)
        self._device_index = device_index
        self._native_rate = int(info.get("defaultSampleRate", 16000)) or 16000
        # Force mono on the mic side. Whisper does not benefit from stereo
        # mic and most laptop mics are mono anyway.
        self._channels = 1
        self.wav_path.parent.mkdir(parents=True, exist_ok=True)
        self._wf = wave.open(str(self.wav_path), "wb")
        self._wf.setnchannels(self._channels)
        self._wf.setsampwidth(2)
        self._wf.setframerate(self._native_rate)
        self._is_recording = True
        open_kwargs: dict = dict(
            format=pyaudio.paInt16,
            channels=self._channels,
            rate=self._native_rate,
            input=True,
            frames_per_buffer=1024,
            stream_callback=self._callback,
        )
        if device_index is not None:
            open_kwargs["input_device_index"] = device_index
        self._stream = self._pa.open(**open_kwargs)
        self._stream.start_stream()
        log.info(
            "MicRecorder started: device=%s (%s), %d Hz, %d ch -> %s",
            device_index, info.get("name", "?"), self._native_rate, self._channels, self.wav_path,
        )
        self.started.emit()

    def _callback(self, in_data, frame_count, time_info, status):
        import pyaudio
        if not self._is_recording:
            return (None, pyaudio.paComplete)
        if self._paused:
            # Drop the buffer; do not append to WAV. Live view pauses too.
            return (None, pyaudio.paContinue)
        try:
            if self._wf is not None:
                self._wf.writeframes(in_data)
            pcm = np.frombuffer(in_data, dtype=np.int16)
            mono16k = to_mono_16k(pcm, channels=self._channels, src_rate=self._native_rate)
            self.chunk_buffer.write(self.source_name, mono16k)
        except Exception as exc:  # pragma: no cover - audio-callback safety net
            log.exception("mic callback failed")
            self.error.emit(str(exc))
            return (None, pyaudio.paAbort)
        return (None, pyaudio.paContinue)

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    @property
    def device_index(self) -> Optional[int]:
        return self._device_index

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
                log.exception("mic stream close failed")
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
        log.info("MicRecorder stopped")
        self.stopped.emit()


def _pick_input_device(pa) -> tuple[Optional[int], dict]:
    """Choose an input device. Returns (device_index_or_None, device_info).

    PyAudio's `get_default_input_device_info()` is unreliable on Windows --
    it often raises 'No Default Input Device Available' even when working
    input devices exist (especially when MME is the active host API but the
    user's default is WASAPI). We fall back to enumerating all devices and
    prefer the first WASAPI input, then any input, then a helpful error.
    """
    import pyaudio

    try:
        info = pa.get_default_input_device_info()
        log.info("default input device available: %s", info.get("name", "?"))
        return None, info
    except (OSError, IOError) as exc:
        log.warning("get_default_input_device_info failed: %s; enumerating devices", exc)

    candidates: list[tuple[int, dict]] = []
    for i in range(pa.get_device_count()):
        try:
            d = pa.get_device_info_by_index(i)
        except Exception:
            continue
        max_in = int(d.get("maxInputChannels") or 0)
        if max_in <= 0:
            continue
        candidates.append((i, d))

    if not candidates:
        raise RuntimeError(
            "No input audio devices found. On Windows: open Settings -> Privacy & "
            "Security -> Microphone and make sure 'Microphone access' is on for "
            "Desktop apps. If the mic is brand new, unplug + replug it and try again."
        )

    # Prefer WASAPI inputs (most reliable on Windows 11).
    wasapi_api_index: Optional[int] = None
    try:
        for api_idx in range(pa.get_host_api_count()):
            api = pa.get_host_api_info_by_index(api_idx)
            if int(api.get("type") or 0) == pyaudio.paWASAPI:
                wasapi_api_index = api_idx
                break
    except Exception:
        wasapi_api_index = None

    if wasapi_api_index is not None:
        for idx, d in candidates:
            if int(d.get("hostApi") or -1) == wasapi_api_index:
                log.info(
                    "picked WASAPI input device #%d: %s (%d ch, %d Hz)",
                    idx, d.get("name", "?"),
                    int(d.get("maxInputChannels") or 0),
                    int(d.get("defaultSampleRate") or 0),
                )
                return idx, d

    idx, d = candidates[0]
    log.info(
        "picked input device #%d: %s (%d ch, %d Hz)",
        idx, d.get("name", "?"),
        int(d.get("maxInputChannels") or 0),
        int(d.get("defaultSampleRate") or 0),
    )
    return idx, d
