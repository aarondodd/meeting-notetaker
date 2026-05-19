"""SpeechBrain ECAPA-TDNN voice-embedding wrapper.

We isolate the encoder behind a thin facade so the rest of the diarization
pipeline can be unit-tested with a mock embedder. Real imports happen
inside `VoiceEncoder._lazy_import` so that test environments without
SpeechBrain installed can still exercise the segmenter, clusterer, and
store.

ECAPA-TDNN (Emphasized Channel Attention, Propagation and Aggregation
Time-Delay Neural Network) is a stronger speaker-embedding model than
the LSTM-based encoder this module previously used: roughly seven times
lower equal-error-rate on VoxCeleb1-E. Same-gender separation
specifically gets meaningfully better, which is the failure mode that
shows up as "two real speakers getting merged into one cluster."

The pretrained model is `speechbrain/spkrec-ecapa-voxceleb`. License is
Apache-2.0; no Hugging Face token or user license-accept required. The
first call downloads ~22 MB into `app_data_dir() / "models" / "ecapa"`
and caches it there for offline runs.

SpeechBrain's `EncoderClassifier.encode_batch` accepts a float32 torch
tensor sampled at 16 kHz, returns a 192-dim L2-normalizable embedding.
The wrapper handles the int16 -> float32 conversion + sample rate
resampling using our existing audio.resample helpers (no librosa just
for that step).

A short minimum-duration guard avoids feeding tiny snippets to the
encoder where the embedding would be unreliable.
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np

from ..audio.resample import resample_linear_int16, to_mono_int16
from ..utils.paths import app_data_dir


log = logging.getLogger(__name__)


EMBEDDING_DIM = 192
TARGET_SAMPLE_RATE = 16000
MIN_DURATION_SEC = 0.5

# Hugging Face Hub id of the public, Apache-2.0 ECAPA-TDNN checkpoint.
# Public download; no token required.
_ECAPA_SOURCE = "speechbrain/spkrec-ecapa-voxceleb"


class EmbedderUnavailable(RuntimeError):
    """Raised when SpeechBrain can't be imported (missing dep) or fails to load."""


def _model_cache_dir():
    """Where to cache the ECAPA checkpoint. Sits next to our other models
    rather than under the HF default ~/.cache/huggingface so end users
    see all app artifacts in one place."""
    cache = app_data_dir() / "models" / "ecapa"
    cache.mkdir(parents=True, exist_ok=True)
    return cache


class VoiceEncoder:
    """Thin wrapper around speechbrain.inference.speaker.EncoderClassifier.

    The underlying model loads on first `embed_*` call so that simply
    constructing a refiner doesn't drag torch + speechbrain into memory.
    Subsequent calls reuse the loaded model.
    """

    def __init__(self) -> None:
        self._classifier = None
        self._torch = None
        self._load_error: Optional[Exception] = None

    @property
    def is_loaded(self) -> bool:
        return self._classifier is not None

    @property
    def is_available(self) -> bool:
        """True if SpeechBrain + torch can be imported. Does not load
        the network or download model weights."""
        try:
            self._lazy_import()
            return True
        except EmbedderUnavailable:
            return False

    def _lazy_import(self):
        try:
            import torch  # noqa: F401
        except ImportError as exc:
            self._load_error = exc
            raise EmbedderUnavailable(
                f"torch is not installed: {exc}. Install the project "
                "requirements (`pip install -r requirements.txt`) before "
                "running speaker identification."
            ) from exc
        try:
            from speechbrain.inference.speaker import EncoderClassifier
            return EncoderClassifier
        except ImportError as exc:
            self._load_error = exc
            raise EmbedderUnavailable(
                f"SpeechBrain is not installed: {exc}. Install with "
                "`pip install speechbrain` (already listed in "
                "requirements.txt)."
            ) from exc
        except Exception as exc:  # pragma: no cover - defensive
            self._load_error = exc
            raise EmbedderUnavailable(
                f"SpeechBrain failed to import: {exc}"
            ) from exc

    def _load(self) -> None:
        if self._classifier is not None:
            return
        encoder_cls = self._lazy_import()
        try:
            import torch
            self._torch = torch
            savedir = str(_model_cache_dir())
            # `from_hparams` pulls the checkpoint from HF Hub on first call
            # and caches under `savedir`. Subsequent loads are offline.
            #
            # `local_strategy=COPY` matters on Windows: SpeechBrain's
            # default is SYMLINK, which downloads to ~/.cache/huggingface
            # and then symlinks the files into savedir. Creating symlinks
            # on Windows requires admin or Developer Mode (WinError 1314
            # otherwise), neither of which a corporate user typically has.
            # COPY uses `shutil.copy` instead -- ~22 MB of duplication on
            # disk in exchange for working on every Windows config. The
            # parameter has been part of SpeechBrain since 1.0; for older
            # builds we silently fall back to whatever the default is.
            kwargs = {
                "source": _ECAPA_SOURCE,
                "savedir": savedir,
                "run_opts": {"device": "cpu"},
            }
            try:
                from speechbrain.utils.fetching import LocalStrategy
                kwargs["local_strategy"] = LocalStrategy.COPY
            except ImportError:
                pass
            self._classifier = encoder_cls.from_hparams(**kwargs)
        except Exception as exc:
            self._load_error = exc
            raise EmbedderUnavailable(
                f"ECAPA-TDNN model failed to load: {exc}. The first run "
                "downloads ~22 MB from huggingface.co; check network "
                "reachability if this is a fresh install."
            ) from exc
        log.info(
            "SpeechBrain ECAPA-TDNN encoder loaded (dim=%d, source=%s)",
            EMBEDDING_DIM, _ECAPA_SOURCE,
        )

    # ---- public API ----

    def embed_pcm(
        self,
        pcm: np.ndarray,
        sample_rate: int,
        *,
        channels: int = 1,
    ) -> np.ndarray:
        """Return a 192-dim embedding for the given PCM clip.

        `pcm` is int16 or float32 (interleaved if `channels > 1`).
        Silently downmixes to mono + resamples to 16 kHz; returns a
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
        torch = self._torch
        # ECAPA expects float32 in [-1, 1] at 16 kHz as a (batch, samples)
        # tensor on the encoder's device.
        float_pcm = resampled.astype(np.float32) / 32768.0
        tensor = torch.from_numpy(float_pcm).unsqueeze(0)
        with torch.inference_mode():
            embedding = self._classifier.encode_batch(tensor)
        # encode_batch returns shape (batch, 1, dim) -- squeeze to (dim,).
        # L2-normalize so cosine similarity reduces to a dot product.
        vec = embedding.squeeze().cpu().numpy().astype(np.float32)
        norm = float(np.linalg.norm(vec))
        if norm > 1e-8:
            vec = vec / norm
        return vec

    def embed_turn(self, turn) -> np.ndarray:
        """Convenience: embed a segmenter.Turn."""
        return self.embed_pcm(turn.pcm, turn.sample_rate)


# Module-level default instance. Refiner uses this so tests can patch
# the underlying model with `monkeypatch.setattr` if needed.
default_encoder = VoiceEncoder()
