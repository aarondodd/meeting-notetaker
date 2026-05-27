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
from .wav_writer import AsyncWavWriter


log = logging.getLogger(__name__)


class LoopbackUnavailable(RuntimeError):
    """Raised when pyaudiowpatch can't find a loopback device on the current host."""


class LoopbackRecorder(QObject):
    error = pyqtSignal(str)
    started = pyqtSignal()
    stopped = pyqtSignal()
    # Non-fatal warning: loopback callback stopped firing well before
    # Stop, leaving N seconds of trailing silence in sys.wav. Issue #44.
    # Distinct from mic capture stall in expected causes (WASAPI engine
    # idle vs USB driver) but same UX impact and same signal shape.
    capture_warning = pyqtSignal(str)

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
        # Background WAV writer (issue #31). PortAudio's loopback
        # callback fires at ~21 ms cadence on Windows; doing disk I/O
        # in the callback exposes us to filesystem-cache + antivirus
        # latency spikes that grow more frequent as the WAV size
        # grows past a few hundred MB, which is exactly when the
        # observed corruption kicks in at ~10-15 minutes.
        self._writer: Optional[AsyncWavWriter] = None
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
        # All resource creation past this point is wrapped so a
        # mid-flight failure (e.g., pa.open raising OSError after the
        # writer thread is already running) tears down what was built
        # before propagating. Without this, the outer controller's
        # cleanup only ran when `is_recording` was True, which left a
        # gap between writer.start() and the flag flip where the
        # writer thread would leak (issue #40).
        try:
            self._pa = pyaudio.PyAudio()
            self._writer = AsyncWavWriter(
                self.wav_path,
                channels=self._channels,
                sample_width=2,
                sample_rate=self._native_rate,
            )
            self._writer.start()
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
        except Exception:
            log.exception("LoopbackRecorder.start failed; tearing down partial state")
            self._partial_start_cleanup()
            raise
        log.info(
            "LoopbackRecorder started: %s, %d Hz, %d ch -> %s",
            device.get("name", "?"),
            self._native_rate,
            self._channels,
            self.wav_path,
        )

    def _partial_start_cleanup(self) -> None:
        """Best-effort teardown when start() fails mid-flight.

        Called from start()'s except clause -- the controller's outer
        handler also calls stop() if is_recording is True, but the
        is_recording flag isn't set early enough to cover every
        path. Owning the cleanup here means start() is the single
        source of truth for "what was built; tear it down."
        """
        self._is_recording = False
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                log.exception("LoopbackRecorder partial-start stream cleanup failed")
            self._stream = None
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                log.exception("LoopbackRecorder partial-start writer cleanup failed")
            self._writer = None
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                log.exception("LoopbackRecorder partial-start pa terminate failed")
            self._pa = None
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
            elif self._last_callback_wallclock is not None and self._writer is not None:
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
                    self._writer.write_silence_frames(gap)
                    self._gap_fill_frames_total += gap
            self._last_callback_wallclock = now
            if self._writer is not None:
                # Non-blocking enqueue -- writer thread drains to disk.
                self._writer.write_frames(in_data)
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
            writer = self._writer
            self._writer = None
        # Drain queue + join writer + finalize WAV. Outside the lock
        # because it may wait several seconds for the writer to
        # flush any backlog.
        writer_closed_cleanly = True
        if writer is not None:
            try:
                writer.close()
            except Exception:
                log.exception("LoopbackRecorder: writer.close failed")
                writer_closed_cleanly = False
        # Skip pad_wav if the writer couldn't finalize the header
        # (issue #41) -- the partial WAV would be made worse by a
        # subsequent rewrite against an unfinalized header.
        if writer_closed_cleanly:
            self._maybe_pad_wav()
        else:
            log.warning(
                "LoopbackRecorder: skipping pad_wav for %s because writer "
                "did not close cleanly; the partial WAV is left as-is",
                self.wav_path,
            )
        self._check_for_trailing_capture_stall()
        log.info("LoopbackRecorder stopped")
        self.stopped.emit()

    # See MicRecorder._TRAILING_STALL_THRESHOLD_S for the rationale.
    # WASAPI loopback is slightly more prone to multi-second idle
    # spans (audio engine sleeps when no renderer is active), so the
    # 10 s threshold is generous on this side too. Issue #44.
    _TRAILING_STALL_THRESHOLD_S = 10.0

    def _check_for_trailing_capture_stall(self) -> None:
        """Warn if WASAPI loopback stopped delivering audio well before Stop."""
        if (
            self._last_callback_wallclock is None
            or self._stop_wallclock is None
        ):
            return
        stall_s = self._stop_wallclock - self._last_callback_wallclock
        if stall_s < self._TRAILING_STALL_THRESHOLD_S:
            return
        msg = (
            f"System-audio loopback stopped delivering audio "
            f"{stall_s:.1f} s before Stop -- the last {stall_s:.0f} s "
            f"of the system-audio track is silence. Likely cause: the "
            f"Windows audio engine went idle (nothing playing through "
            f"the speakers)."
        )
        log.warning("LoopbackRecorder: %s", msg)
        self.capture_warning.emit(msg)

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
