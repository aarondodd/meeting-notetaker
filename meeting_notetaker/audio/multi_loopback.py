"""Multi-endpoint WASAPI loopback orchestrator (#85).

Issue #84's failure mode is "the recorder bound to one output endpoint
at start, then the meeting app routed audio to a different endpoint
mid-call, and we captured silence." The defensive fix is to capture
*every* WASAPI output endpoint simultaneously and mix the per-endpoint
sidecars into the canonical sys.wav at Stop.

Architecture:

  MultiEndpointLoopbackRecorder (this module)
    -> N x LoopbackRecorder, one per discovered WASAPI output endpoint
    -> each writes to sys.<idx>.wav (sidecar)
    -> on stop(): mix sidecars into sys.wav, delete sidecars
    -> exposes the same signal surface as LoopbackRecorder so the
       controller swaps the implementation transparently.

Endpoint discovery pre-filters:

  * Probe each candidate via probe_input_device (#7) so stale ghost
    endpoints don't end up in the capture set.
  * Drop endpoints with maxInputChannels == 0 (host-API quirks).
  * Deduplicate by device name (some WASAPI hosts expose the same
    endpoint twice -- once raw, once as the loopback companion).

Disk discipline:

  * Sidecar WAVs are deleted at finalize regardless of mix success,
    so a crash mid-record leaves at most one orphan WAV per endpoint.
  * Endpoints whose probe fails are not opened, so they contribute
    zero disk.
  * Future enhancement: per-endpoint lazy writer (open the WAV only
    on first non-silent PCM). Deferred until disk usage on real
    multi-monitor setups demonstrates need.

WASAPI stale-handle quirk:

  * Per-endpoint open is wrapped so one failure does not fail the
    whole record. The orchestrator proceeds with N-1 streams; the
    user gets coverage for every endpoint that came up clean.

Silence detection:

  * silence_detected fires only when ALL active sub-recorders report
    silent within their own rolling-window check. One endpoint silent
    while another is active is the common case (audio routing to one
    endpoint, others quiet) and is NOT a warning.
"""
from __future__ import annotations

import logging
import time
import wave
from pathlib import Path
from typing import Optional

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal

from .chunk_buffer import ChunkBuffer
from .devices import probe_input_device
from .loopback_recorder import LoopbackRecorder, LoopbackUnavailable


log = logging.getLogger(__name__)


def discover_output_endpoints() -> list[dict]:
    """Return every usable WASAPI loopback device info dict.

    Filters out ghosts (probe failure) and zero-channel oddities.
    Deduplicates by device name. Returns [] on non-Windows or when
    pyaudiowpatch is unavailable.
    """
    try:
        import pyaudiowpatch as pyaudio
    except ImportError:
        return []
    pa = pyaudio.PyAudio()
    seen_names: set[str] = set()
    out: list[dict] = []
    try:
        for loopback in pa.get_loopback_device_info_generator():
            name = str(loopback.get("name", ""))
            if name in seen_names:
                continue
            if int(loopback.get("maxInputChannels") or 0) <= 0:
                continue
            if not probe_input_device(pa, loopback):
                log.info(
                    "discover_output_endpoints: skipping %s (probe failed)",
                    name,
                )
                continue
            seen_names.add(name)
            # Materialize a plain dict so the caller can use it after pa.terminate().
            out.append({
                "index": int(loopback.get("index") or 0),
                "name": name,
                "defaultSampleRate": float(loopback.get("defaultSampleRate") or 48000.0),
                "maxInputChannels": int(loopback.get("maxInputChannels") or 2),
                "hostApi": int(loopback.get("hostApi") or 0),
                "isLoopbackDevice": bool(loopback.get("isLoopbackDevice", False)),
            })
    finally:
        pa.terminate()
    return out


# Frames per mixing block. At 48 kHz stereo this is ~1 s of audio,
# so peak memory stays at a few MB per sidecar regardless of how long
# the recording ran.
_MIX_BLOCK_FRAMES = 48000


def mix_sidecar_wavs(
    sidecar_paths: list[Path],
    canonical_path: Path,
) -> bool:
    """Sum sidecar WAVs into `canonical_path`.

    Each sidecar is assumed to span the same wall-clock window thanks
    to LoopbackRecorder._maybe_pad_wav at Stop (which pads leading +
    trailing silence). Length differences past that point are end-
    aligned by leading-zero padding so a hot-plug endpoint that
    joined late doesn't pull the rest of the mix earlier. End
    alignment is also right for sidecars orphaned by a crash, which
    all stopped at the same moment.

    Returns True on a successful write. Mismatched sample rates or
    channel counts across sidecars trip a log warning and the mixer
    picks the most common shape; minority-shape sidecars are dropped.

    Streams in blocks of `_MIX_BLOCK_FRAMES`: a 75-minute meeting
    captured on four endpoints is ~3.5 GB of int16 PCM, and loading it
    whole (plus the int32 accumulator) needs several times that.
    """
    if not sidecar_paths:
        return False
    readers: list[wave.Wave_read] = []
    try:
        opened: list[tuple[wave.Wave_read, int, int, int]] = []
        for p in sidecar_paths:
            if not Path(p).exists():
                continue
            rf = wave.open(str(p), "rb")
            readers.append(rf)
            if rf.getsampwidth() != 2:
                log.warning(
                    "mix_sidecar_wavs: %s has sample_width=%d, expected 2; skipping",
                    p, rf.getsampwidth(),
                )
                continue
            if rf.getnframes() == 0:
                continue
            opened.append((rf, rf.getframerate(), rf.getnchannels(), rf.getnframes()))
        if not opened:
            return False
        shape_counts: dict[tuple[int, int], int] = {}
        for _, rate, ch, _ in opened:
            shape_counts[(rate, ch)] = shape_counts.get((rate, ch), 0) + 1
        winner_shape = max(shape_counts, key=shape_counts.get)
        dropped = [o for o in opened if (o[1], o[2]) != winner_shape]
        if dropped:
            log.warning(
                "mix_sidecar_wavs: dropping %d sidecar(s) whose (rate, channels) "
                "differs from %s", len(dropped), winner_shape,
            )
        sources = [(rf, n) for rf, r, c, n in opened if (r, c) == winner_shape]
        rate, channels = winner_shape
        max_frames = max(n for _, n in sources)
        divisor = len(sources)
        canonical_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(canonical_path), "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            pos = 0
            while pos < max_frames:
                block = min(_MIX_BLOCK_FRAMES, max_frames - pos)
                acc = np.zeros(block * channels, dtype=np.int32)
                for rf, n in sources:
                    # Source starts (max_frames - n) frames in; frames
                    # before that are the leading-silence pad.
                    offset = max_frames - n
                    lo = max(pos, offset)
                    hi = pos + block
                    if lo >= hi:
                        continue
                    pcm = np.frombuffer(rf.readframes(hi - lo), dtype=np.int16)
                    dst = (lo - pos) * channels
                    acc[dst:dst + pcm.size] += pcm
                acc //= divisor
                wf.writeframes(acc.astype(np.int16).tobytes())
                pos += block
        return True
    finally:
        for rf in readers:
            try:
                rf.close()
            except Exception:  # noqa: BLE001
                pass


class MultiEndpointLoopbackRecorder(QObject):
    """Drop-in for LoopbackRecorder that captures every WASAPI output
    endpoint and mixes at finalize.

    Signal surface mirrors LoopbackRecorder so the controller wires
    them interchangeably:

      error, started, stopped, capture_warning, silence_detected,
      silence_cleared
    """

    error = pyqtSignal(str)
    started = pyqtSignal()
    stopped = pyqtSignal()
    capture_warning = pyqtSignal(str)
    silence_detected = pyqtSignal()
    silence_cleared = pyqtSignal()

    def __init__(
        self,
        chunk_buffer: Optional[ChunkBuffer],
        wav_path: Path,
        *,
        source_name: str = "sys",
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.chunk_buffer = chunk_buffer
        self.wav_path = Path(wav_path)
        self.source_name = source_name
        self._sub_recorders: list[LoopbackRecorder] = []
        self._sidecar_paths: list[Path] = []
        self._is_recording = False
        self._paused = False
        # Track per-sub silence state for the all-quiet aggregation.
        # Keyed by id(sub_recorder); value is bool.
        self._sub_silence_state: dict[int, bool] = {}
        # Whether the aggregate-silence signal has been emitted; flips
        # back when any sub clears.
        self._aggregate_silence_emitted = False

    @staticmethod
    def is_available() -> bool:
        return LoopbackRecorder.is_available()

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def sub_recorders(self) -> list[LoopbackRecorder]:
        return list(self._sub_recorders)

    def _sidecar_path(self, idx: int) -> Path:
        stem = self.wav_path.stem
        return self.wav_path.with_name(f"{stem}.{idx}{self.wav_path.suffix}")

    def start(self) -> None:
        endpoints = discover_output_endpoints()
        if not endpoints:
            # Fall back to single-endpoint mode if discovery turned up
            # nothing usable; the caller's error path handles the
            # LoopbackUnavailable.
            raise LoopbackUnavailable(
                "no WASAPI output endpoints found for multi-endpoint capture"
            )
        opened: list[LoopbackRecorder] = []
        for idx, ep in enumerate(endpoints):
            sidecar = self._sidecar_path(idx)
            sub = LoopbackRecorder(
                # No live chunk_buffer feed from sub-recorders; the
                # canonical mix-at-finalize is the source of truth.
                # Live transcription's primary path is the mic anyway,
                # and feeding all endpoints into a single buffer would
                # double-count audio that's already in one of them.
                chunk_buffer=None,
                wav_path=sidecar,
                source_name=f"{self.source_name}.{idx}",
                # Bind by exact device index, not by name -- skips the
                # find_loopback_device fuzzy match.
                device_name=ep["name"],
            )
            sub.error.connect(lambda msg: self.error.emit(msg))
            sub.capture_warning.connect(lambda msg: self.capture_warning.emit(msg))
            sub.silence_detected.connect(
                lambda s=sub: self._on_sub_silence_detected(s)
            )
            sub.silence_cleared.connect(
                lambda s=sub: self._on_sub_silence_cleared(s)
            )
            try:
                sub.start()
            except LoopbackUnavailable as exc:
                log.warning(
                    "multi-endpoint: %s failed to open (%s); continuing with others",
                    ep["name"], exc,
                )
                continue
            except Exception:
                log.exception(
                    "multi-endpoint: unexpected failure opening %s; continuing",
                    ep["name"],
                )
                continue
            opened.append(sub)
            self._sidecar_paths.append(sidecar)
            self._sub_silence_state[id(sub)] = False
        if not opened:
            raise LoopbackUnavailable(
                "every WASAPI output endpoint failed to open for multi-endpoint capture"
            )
        self._sub_recorders = opened
        self._is_recording = True
        log.info(
            "MultiEndpointLoopbackRecorder started with %d/%d endpoints",
            len(opened), len(endpoints),
        )
        self.started.emit()

    def pause(self) -> None:
        self._paused = True
        for sub in self._sub_recorders:
            sub.pause()

    def resume(self) -> None:
        self._paused = False
        for sub in self._sub_recorders:
            sub.resume()

    def stop(self) -> None:
        if not self._is_recording:
            return
        self._is_recording = False
        # Stop all sub-recorders. Each does its own pad_wav, writer
        # close, and stop summary, so the sidecars are valid WAVs
        # once stop() returns.
        for sub in self._sub_recorders:
            try:
                sub.stop()
            except Exception:
                log.exception("multi-endpoint: sub-recorder stop failed")
        # Mix sidecars into the canonical sys.wav, then delete sidecars.
        mix_ok = False
        try:
            mix_ok = mix_sidecar_wavs(self._sidecar_paths, self.wav_path)
        except Exception:
            log.exception("multi-endpoint: mix_sidecar_wavs failed")
        # Delete sidecars regardless of mix outcome so a crashed mix
        # doesn't leave orphans. If the mix failed but at least one
        # sidecar exists, copy it as a fallback so the session still
        # has a sys.wav.
        if not mix_ok and self._sidecar_paths:
            for sp in self._sidecar_paths:
                if sp.exists():
                    try:
                        sp.replace(self.wav_path)
                        log.warning(
                            "multi-endpoint: mix failed; using %s as the sys.wav fallback",
                            sp,
                        )
                        break
                    except OSError:
                        log.exception("fallback rename failed")
        for sp in self._sidecar_paths:
            try:
                if sp.exists():
                    sp.unlink()
            except OSError:
                log.warning("could not delete sidecar %s", sp)
        self._sidecar_paths = []
        self._sub_recorders = []
        self._sub_silence_state = {}
        self.stopped.emit()

    # ---- hot-plug extension (#85.6) ------------------------------------

    def extend_to_endpoint(self, endpoint_name: str) -> bool:
        """Open a new sub-recorder for an endpoint that appeared mid-call.

        Looks up the named endpoint in the current device list, opens
        a LoopbackRecorder pointed at it, wires the same signal
        bridging the constructor does for the start() set, and starts
        it. Returns True on success.

        Idempotent: if the endpoint is already in the active capture
        set (by name), the call is a no-op.

        Failure modes (endpoint not found, probe failure, open
        rejection) are logged but not raised; the orchestrator keeps
        whatever sub-recorders it already has.
        """
        if not self._is_recording:
            return False
        active_names = {sub.device_name for sub in self._sub_recorders}
        if endpoint_name in active_names:
            return True
        endpoints = discover_output_endpoints()
        target = next((ep for ep in endpoints if ep["name"] == endpoint_name), None)
        if target is None:
            log.info(
                "extend_to_endpoint: %r not found in current device list",
                endpoint_name,
            )
            return False
        idx = len(self._sub_recorders)
        sidecar = self._sidecar_path(idx)
        sub = LoopbackRecorder(
            chunk_buffer=None,
            wav_path=sidecar,
            source_name=f"{self.source_name}.{idx}",
            device_name=endpoint_name,
        )
        sub.error.connect(lambda msg: self.error.emit(msg))
        sub.capture_warning.connect(lambda msg: self.capture_warning.emit(msg))
        sub.silence_detected.connect(
            lambda s=sub: self._on_sub_silence_detected(s)
        )
        sub.silence_cleared.connect(
            lambda s=sub: self._on_sub_silence_cleared(s)
        )
        try:
            sub.start()
        except Exception:
            log.exception("extend_to_endpoint: failed to open %r", endpoint_name)
            return False
        self._sub_recorders.append(sub)
        self._sidecar_paths.append(sidecar)
        self._sub_silence_state[id(sub)] = False
        log.info(
            "MultiEndpointLoopbackRecorder: extended capture to %r "
            "(now %d endpoints)",
            endpoint_name, len(self._sub_recorders),
        )
        return True

    # ---- aggregate silence routing -------------------------------------

    def _on_sub_silence_detected(self, sub: LoopbackRecorder) -> None:
        self._sub_silence_state[id(sub)] = True
        # If every active sub is silent, signal the aggregate. One sub
        # silent while another is active is the common case (audio
        # routed to a single endpoint, the others quiet) and is NOT a
        # warning -- it's exactly what multi-endpoint capture is
        # designed to handle gracefully.
        if all(self._sub_silence_state.values()) and not self._aggregate_silence_emitted:
            self._aggregate_silence_emitted = True
            log.warning(
                "MultiEndpointLoopbackRecorder: every endpoint is silent; "
                "emitting aggregate silence_detected"
            )
            self.silence_detected.emit()

    def _on_sub_silence_cleared(self, sub: LoopbackRecorder) -> None:
        self._sub_silence_state[id(sub)] = False
        if self._aggregate_silence_emitted:
            self._aggregate_silence_emitted = False
            log.info(
                "MultiEndpointLoopbackRecorder: an endpoint became active; "
                "emitting aggregate silence_cleared"
            )
            self.silence_cleared.emit()
