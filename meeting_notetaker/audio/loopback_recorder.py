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
import time
import wave
from pathlib import Path
from typing import Optional

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal

from .chunk_buffer import ChunkBuffer
from .resample import to_mono_16k
from .wav_align import compute_pad_frames, gap_frames_to_fill, pad_wav


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
        device_name: str = "",
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.chunk_buffer = chunk_buffer
        self.wav_path = Path(wav_path)
        self.source_name = source_name
        self.device_name = device_name
        self._pa = None
        self._stream = None
        self._wf: Optional[wave.Wave_write] = None
        self._lock = threading.Lock()
        self._is_recording = False
        self._paused = False
        self._native_rate = 48000
        self._channels = 2
        # Wall-clock tracking for post-stop WAV alignment. WASAPI
        # loopback typically does not deliver any frames until
        # something actually plays through the speakers (the audio
        # engine is asleep until then), so first_sample_wallclock
        # can be many seconds after start_wallclock. pad_wav at stop
        # fills in that leading silence so sys.wav spans the same
        # wall-clock window as mic.wav.
        self._start_wallclock: Optional[float] = None
        self._first_sample_wallclock: Optional[float] = None
        self._last_callback_wallclock: Optional[float] = None
        self._stop_wallclock: Optional[float] = None
        # Cumulative silence frames the callback inserted to fill
        # mid-recording WASAPI sleeps. WASAPI loopback often goes
        # idle between audio bursts; without this, two separate
        # audio clips would land back-to-back in sys.wav with no
        # silence between them -- the bug Aaron flagged where the
        # second music clip played at the wrong wall-clock moment.
        self._gap_fill_frames_total: int = 0

    @staticmethod
    def is_available() -> bool:
        try:
            import pyaudiowpatch  # noqa: F401
            return True
        except ImportError:
            return False

    @staticmethod
    def find_loopback_device(saved_name: str = ""):
        """Return a loopback device info dict, or None.

        If `saved_name` is set, prefer the loopback whose name matches
        (exact, then case-insensitive substring). Otherwise return the
        loopback paired with the system default output.
        """
        try:
            import pyaudiowpatch as pyaudio
        except ImportError:
            return None
        pa = pyaudio.PyAudio()
        try:
            if saved_name:
                target = saved_name.strip()
                target_lower = target.lower()
                substring_match = None
                for loopback in pa.get_loopback_device_info_generator():
                    name = str(loopback.get("name", ""))
                    if name == target:
                        return loopback
                    if substring_match is None and target_lower and target_lower in name.lower():
                        substring_match = loopback
                if substring_match is not None:
                    log.info(
                        "picked loopback device (substring match for saved %r): %s",
                        saved_name, substring_match.get("name", "?"),
                    )
                    return substring_match
                log.warning(
                    "saved loopback device %r not found; falling back to default-output loopback",
                    saved_name,
                )

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
        device = self.find_loopback_device(saved_name=self.device_name)
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
        self._start_wallclock = time.monotonic()
        self._first_sample_wallclock = None
        self._last_callback_wallclock = None
        self._stop_wallclock = None
        self._gap_fill_frames_total = 0
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
            now = time.monotonic()
            if self._first_sample_wallclock is None:
                self._first_sample_wallclock = now
            elif self._last_callback_wallclock is not None and self._wf is not None:
                # WASAPI loopback can stop firing callbacks during
                # silence (no renderer active -> audio engine sleeps).
                # Detect the gap by wall-clock vs frame-time delta;
                # fill with silence so the WAV stays continuously
                # wall-clock-aligned. Without this, two separate
                # audio bursts land back-to-back in sys.wav and the
                # second one plays at the wrong moment in playback.
                gap = gap_frames_to_fill(
                    now_wallclock=now,
                    last_callback_wallclock=self._last_callback_wallclock,
                    frame_count=frame_count,
                    sample_rate=self._native_rate,
                )
                if gap > 0:
                    log.info(
                        "LoopbackRecorder gap-fill: %d frames (%.0f ms)",
                        gap, gap * 1000 / self._native_rate,
                    )
                    self._write_silence_frames(gap)
                    self._gap_fill_frames_total += gap
            self._last_callback_wallclock = now
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
            self._stop_wallclock = time.monotonic()
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
        self._maybe_pad_wav()
        log.info("LoopbackRecorder stopped")
        self.stopped.emit()

    def _write_silence_frames(self, frame_count: int) -> None:
        """Append `frame_count` of all-zero PCM to the current WAV.

        Sampwidth is 2 (int16); channels = self._channels. Stream
        in 64 KB blocks so a long gap (rare but possible) doesn't
        materialize a giant byte string.
        """
        if self._wf is None or frame_count <= 0:
            return
        bytes_per_frame = self._channels * 2
        BLOCK_FRAMES = 8192
        block = b"\x00" * (BLOCK_FRAMES * bytes_per_frame)
        remaining = frame_count
        while remaining >= BLOCK_FRAMES:
            self._wf.writeframes(block)
            remaining -= BLOCK_FRAMES
        if remaining > 0:
            self._wf.writeframes(b"\x00" * (remaining * bytes_per_frame))

    def _maybe_pad_wav(self) -> None:
        """Rewrite the WAV with leading + trailing silence to span
        [start_wallclock, stop_wallclock]. Loopback's leading
        silence can be several seconds when the Windows audio
        engine sleeps until first audio plays.
        """
        if (
            self._start_wallclock is None
            or self._stop_wallclock is None
        ):
            return
        try:
            with wave.open(str(self.wav_path), "rb") as rf:
                actual_frames = rf.getnframes()
        except (FileNotFoundError, wave.Error):
            return
        leading, trailing = compute_pad_frames(
            start_wallclock=self._start_wallclock,
            first_sample_wallclock=self._first_sample_wallclock,
            stop_wallclock=self._stop_wallclock,
            actual_frames=actual_frames,
            sample_rate=self._native_rate,
        )
        if leading == 0 and trailing == 0:
            return
        log.info(
            "LoopbackRecorder pad: leading=%d frames (%.0f ms), "
            "trailing=%d frames (%.0f ms), rate=%d",
            leading, leading * 1000 / self._native_rate,
            trailing, trailing * 1000 / self._native_rate,
            self._native_rate,
        )
        try:
            pad_wav(
                self.wav_path,
                leading_frames=leading, trailing_frames=trailing,
            )
        except Exception:
            log.exception(
                "LoopbackRecorder: pad_wav failed for %s", self.wav_path,
            )
