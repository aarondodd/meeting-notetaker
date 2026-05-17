"""Resemblyzer voice-embedding wrapper.

We isolate Resemblyzer behind a thin facade so the rest of the diarization
pipeline can be unit-tested with a mock embedder. Real imports happen
inside `VoiceEncoder._lazy_import` so that test environments without
Resemblyzer installed can still exercise the segmenter, clusterer, and
store.

Resemblyzer's `VoiceEncoder.embed_utterance` accepts a float32 numpy array
sampled at 16kHz, returns a 256-dim L2-normalized embedding. The wrapper
handles the int16 -> float32 conversion + sample rate resampling using our
existing audio.resample helpers (no librosa just for that step).

A short minimum-duration guard avoids feeding tiny snippets to the encoder
where the embedding would be unreliable.
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np

from ..audio.resample import to_mono_int16, resample_linear_int16


log = logging.getLogger(__name__)

EMBEDDING_DIM = 256
TARGET_SAMPLE_RATE = 16000
MIN_DURATION_SEC = 0.5


class EmbedderUnavailable(RuntimeError):
    """Raised when Resemblyzer can't be imported (missing dep) or fails to load."""


class VoiceEncoder:
    """Thin wrapper around resemblyzer.VoiceEncoder.

    The underlying model loads on first `embed_*` call so that simply
    constructing a refiner doesn't drag torch into memory. Subsequent
    calls reuse the loaded model.
    """

    def __init__(self) -> None:
        self._model = None
        self._load_error: Optional[Exception] = None

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def is_available(self) -> bool:
        """True if Resemblyzer can be imported. Does not load the model."""
        try:
            self._lazy_import()
            return True
        except EmbedderUnavailable:
            return False

    def _lazy_import(self):
        try:
            from resemblyzer import VoiceEncoder as _Encoder
            return _Encoder
        except ImportError as exc:
            self._load_error = exc
            raise EmbedderUnavailable(
                f"Resemblyzer is not installed: {exc}. Install with "
                "`pip install Resemblyzer` (pulls librosa, scipy, torch)."
            ) from exc
        except Exception as exc:  # pragma: no cover - defensive
            self._load_error = exc
            raise EmbedderUnavailable(
                f"Resemblyzer failed to import: {exc}"
            ) from exc

    def _load(self) -> None:
        if self._model is not None:
            return
        encoder_cls = self._lazy_import()
        try:
            self._model = encoder_cls(verbose=False)
        except Exception as exc:
            self._load_error = exc
            raise EmbedderUnavailable(
                f"Resemblyzer model failed to load: {exc}"
            ) from exc
        log.info("Resemblyzer VoiceEncoder loaded (dim=%d)", EMBEDDING_DIM)

    # ---- public API ----

    def embed_pcm(
        self,
        pcm: np.ndarray,
        sample_rate: int,
        *,
        channels: int = 1,
    ) -> np.ndarray:
        """Return a 256-dim embedding for the given PCM clip.

        `pcm` is int16 or float32 (interleaved if `channels > 1`).
        Silently downmixes to mono + resamples to 16kHz; returns a
        zero-norm vector if the input is too short to embed.
        """
        if pcm.size == 0:
            return np.zeros(EMBEDDING_DIM, dtype=np.float32)
        if pcm.dtype != np.int16:
            pcm = np.clip(pcm, -32768, 32767).astype(np.int16)
        mono = to_mono_int16(pcm, channels=channels) if channels > 1 else pcm
        resampled = resample_linear_int16(
            mono, src_rate=sample_rate, target_rate=TARGET_SAMPLE_RATE
        )
        duration = resampled.size / TARGET_SAMPLE_RATE
        if duration < MIN_DURATION_SEC:
            log.debug(
                "skipping embedding for %.2fs clip (min %.2fs)",
                duration,
                MIN_DURATION_SEC,
            )
            return np.zeros(EMBEDDING_DIM, dtype=np.float32)

        self._load()
        # Resemblyzer expects float32 in [-1, 1] at 16kHz.
        float_pcm = resampled.astype(np.float32) / 32768.0
        # `embed_utterance` returns a normalized 256-dim numpy array.
        embedding = self._model.embed_utterance(float_pcm)
        return np.asarray(embedding, dtype=np.float32)

    def embed_turn(self, turn) -> np.ndarray:
        """Convenience: embed a segmenter.Turn."""
        return self.embed_pcm(turn.pcm, turn.sample_rate)


# Module-level default instance. Refiner uses this so tests can patch
# the underlying model with `monkeypatch.setattr` if needed.
default_encoder = VoiceEncoder()
