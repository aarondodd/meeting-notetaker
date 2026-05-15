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

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    @property
    def is_paused(self) -> bool:
        return self._paused

    def start(self) -> None:
        import pyaudio  # local import; PortAudio dep
        self._pa = pyaudio.PyAudio()
        info = self._pa.get_default_input_device_info()
        self._native_rate = int(info.get("defaultSampleRate", 16000))
        # Force mono on the mic side. Whisper does not benefit from stereo
        # mic and most laptop mics are mono anyway.
        self._channels = 1
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
            frames_per_buffer=1024,
            stream_callback=self._callback,
        )
        self._stream.start_stream()
        log.info("MicRecorder started: %d Hz, %d ch -> %s", self._native_rate, self._channels, self.wav_path)
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
