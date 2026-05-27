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
        # The WAV writer runs on a background thread so the PortAudio
        # callback can return without ever touching the disk -- the
        # fix for the audio-corruption arm of issue #31. None outside
        # an active capture; built in start(), torn down in stop().
        self._writer: Optional[AsyncWavWriter] = None
        self._lock = threading.Lock()
        self._is_recording = False
        self._paused = False
        self._native_rate = 16000
        self._channels = 1
        self._device_index: Optional[int] = None
        # Wall-clock tracking for post-stop WAV alignment. The
        # recorder's WAV ends up holding only the frames the callback
        # actually wrote; pad_wav fills in the leading + trailing
        # silence so the file spans the full [start, stop] window.
        self._start_wallclock: Optional[float] = None
        self._first_sample_wallclock: Optional[float] = None
        self._last_callback_wallclock: Optional[float] = None
        self._stop_wallclock: Optional[float] = None
        # Cumulative silence frames the callback inserted to fill
        # mid-recording WASAPI sleeps. Counted for diagnostics + so
        # compute_pad_frames's trailing math accounts for them.
        self._gap_fill_frames_total: int = 0

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    @property
    def is_paused(self) -> bool:
        return self._paused

    def start(self) -> None:
        import pyaudio  # local import; PortAudio dep
        self._pa = pyaudio.PyAudio()
        device_index, info = _pick_input_device(self._pa, saved_name=self.device_name)
        self._device_index = device_index
        self._native_rate = int(info.get("defaultSampleRate", 16000)) or 16000
        # Force mono on the mic side. Whisper does not benefit from stereo
        # mic and most laptop mics are mono anyway.
        self._channels = 1
        # Wrap so mid-flight failures (e.g., pa.open after the writer
        # thread is already running) tear down the partial state
        # before propagating. The outer controller's cleanup runs
        # stop() only when is_recording is True, leaving a gap that
        # this self-cleanup covers (issue #40).
        try:
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
        except Exception:
            log.exception("MicRecorder.start failed; tearing down partial state")
            self._partial_start_cleanup()
            raise
        log.info(
            "MicRecorder started: device=%s (%s), %d Hz, %d ch -> %s",
            device_index, info.get("name", "?"), self._native_rate, self._channels, self.wav_path,
        )
        self.started.emit()

    def _partial_start_cleanup(self) -> None:
        """Best-effort teardown when start() fails mid-flight."""
        self._is_recording = False
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                log.exception("MicRecorder partial-start stream cleanup failed")
            self._stream = None
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                log.exception("MicRecorder partial-start writer cleanup failed")
            self._writer = None
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                log.exception("MicRecorder partial-start pa terminate failed")
            self._pa = None

    def _callback(self, in_data, frame_count, time_info, status):
        import pyaudio
        if not self._is_recording:
            return (None, pyaudio.paComplete)
        if self._paused:
            # Drop the buffer; do not append to WAV. Live view pauses too.
            return (None, pyaudio.paContinue)
        try:
            now = time.monotonic()
            if self._first_sample_wallclock is None:
                self._first_sample_wallclock = now
            elif self._last_callback_wallclock is not None and self._writer is not None:
                # Mid-callback gap detection. If PyAudio's input stream
                # stalled (rare on mic capture but possible under heavy
                # load), fill the gap with silence so the WAV stays
                # wall-clock-aligned. Mostly a no-op for mic; load-
                # bearing for the symmetric LoopbackRecorder path.
                gap = gap_frames_to_fill(
                    now_wallclock=now,
                    last_callback_wallclock=self._last_callback_wallclock,
                    frame_count=frame_count,
                    sample_rate=self._native_rate,
                )
                if gap > 0:
                    log.info(
                        "MicRecorder gap-fill: %d frames (%.0f ms)",
                        gap, gap * 1000 / self._native_rate,
                    )
                    self._writer.write_silence_frames(gap)
                    self._gap_fill_frames_total += gap
            self._last_callback_wallclock = now
            if self._writer is not None:
                # Non-blocking enqueue; the writer thread does the
                # actual writeframes() so a slow disk can't stall
                # the PortAudio callback.
                self._writer.write_frames(in_data)
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
            self._stop_wallclock = time.monotonic()
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
            writer = self._writer
            self._writer = None
        # close() drains the queue + joins the writer thread + closes
        # the wave file with the final header. Done outside the lock
        # because it can wait up to a few seconds for the writer to
        # finish.
        writer_closed_cleanly = True
        if writer is not None:
            try:
                writer.close()
            except Exception:
                log.exception("MicRecorder: writer.close failed")
                writer_closed_cleanly = False
        # Skip pad_wav if the writer couldn't finalize the header
        # (issue #41) -- rewriting an unfinalized WAV produces a
        # doubly-corrupt file that's worse than the partial original.
        if writer_closed_cleanly:
            self._maybe_pad_wav()
        else:
            log.warning(
                "MicRecorder: skipping pad_wav for %s because writer "
                "did not close cleanly; the partial WAV is left as-is",
                self.wav_path,
            )
        log.info("MicRecorder stopped")
        self.stopped.emit()

    def _maybe_pad_wav(self) -> None:
        """Rewrite the WAV with leading + trailing silence to span
        [start_wallclock, stop_wallclock]. Mic capture is usually
        continuous so this is a near-no-op for the mic side, but
        applying it symmetrically with the loopback recorder keeps
        the two files exactly the same length and naturally aligned
        for transcription / playback / export.
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
            "MicRecorder pad: leading=%d frames (%.0f ms), "
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
            log.exception("MicRecorder: pad_wav failed for %s", self.wav_path)


def _is_store_python() -> bool:
    """Detect Microsoft Store Python (sandboxed AppContainer, no mic capability).

    The Store Python package runs inside a UWP AppContainer that strips
    capabilities the package manifest doesn't declare. Microphone is one
    of them, so PortAudio sees zero input devices regardless of Windows
    Privacy settings. The fix is non-Store Python (python.org installer).
    """
    import sys

    exe = (sys.executable or "").lower()
    return ("windowsapps" in exe) or ("pythonsoftwarefoundation" in exe)


def list_all_devices(pa) -> list[dict]:
    """Return PyAudio's device list as a list of plain dicts (for diagnostics)."""
    out: list[dict] = []
    for i in range(pa.get_device_count()):
        try:
            d = dict(pa.get_device_info_by_index(i))
            d["_index"] = i
            out.append(d)
        except Exception as exc:
            out.append({"_index": i, "_error": str(exc)})
    return out


def _pick_input_device(pa, saved_name: str = "") -> tuple[Optional[int], dict]:
    """Choose an input device. Returns (device_index_or_None, device_info).

    If `saved_name` is set and matches an available device (exact, then
    case-insensitive substring), use it. Otherwise fall back to:
    PyAudio's `get_default_input_device_info()` (often unreliable on
    Windows -- raises 'No Default Input Device Available' even when working
    input devices exist), then enumerate and prefer the first WASAPI input,
    then any input, then a helpful error.
    """
    import pyaudio
    import sys

    if saved_name:
        target = saved_name.strip()
        target_lower = target.lower()
        # Exact, then case-insensitive substring across all input-capable devices.
        substring_match: Optional[tuple[int, dict]] = None
        for i in range(pa.get_device_count()):
            try:
                d = pa.get_device_info_by_index(i)
            except Exception:
                continue
            if int(d.get("maxInputChannels") or 0) <= 0:
                continue
            name = str(d.get("name", ""))
            if name == target:
                log.info("picked saved input device #%d: %s", i, name)
                return i, d
            if substring_match is None and target_lower and target_lower in name.lower():
                substring_match = (i, d)
        if substring_match is not None:
            idx, d = substring_match
            log.info(
                "picked input device #%d: %s (substring match for saved %r)",
                idx, d.get("name", "?"), saved_name,
            )
            return idx, d
        log.warning("saved input device %r not found; falling back to default", saved_name)

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

    # Log every device we did see, so the meeting_notetaker.log shows enough
    # context for future "why no mic" triage. Includes 0-input rows.
    try:
        all_devices = list_all_devices(pa)
        log.info("PyAudio device enumeration (%d total):", len(all_devices))
        for d in all_devices:
            log.info(
                "  #%s  name=%r hostApi=%s maxIn=%s maxOut=%s rate=%s",
                d.get("_index", "?"),
                d.get("name", "?"),
                d.get("hostApi", "?"),
                d.get("maxInputChannels", "?"),
                d.get("maxOutputChannels", "?"),
                d.get("defaultSampleRate", "?"),
            )
    except Exception:
        log.exception("device-enumeration logging failed")

    if not candidates:
        # Most common Windows root cause first.
        if sys.platform.startswith("win") and _is_store_python():
            raise RuntimeError(
                "Microsoft Store Python detected (interpreter path contains "
                "'WindowsApps'). The Store Python runs inside a UWP AppContainer "
                "that blocks microphone access at the OS level -- PyAudio sees "
                "zero input devices regardless of Windows Privacy settings.\n\n"
                "Fix: install Python from https://www.python.org/downloads/ "
                "(not the Microsoft Store), then rebuild the venv:\n\n"
                "    deactivate\n"
                "    Remove-Item -Recurse -Force .venv\n"
                "    py -3.12 -m venv .venv\n"
                "    .\\.venv\\Scripts\\Activate.ps1\n"
                "    pip install -r requirements-dev.txt\n\n"
                "Then run 'python main.py' again. The mic will be visible."
            )
        raise RuntimeError(
            "No input audio devices found. Check:\n"
            "  1) Settings -> Privacy & Security -> Microphone:\n"
            "     - 'Microphone access' = On\n"
            "     - 'Let desktop apps access your microphone' = On\n"
            "  2) The mic is plugged in and works in another app.\n"
            "  3) Use Help -> Audio Devices... in the app to see exactly what "
            "PyAudio enumerates."
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
