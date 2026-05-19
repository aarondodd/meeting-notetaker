"""Background pre-warm of the speaker-embedding encoder.

First-launch loading of the SpeechBrain ECAPA-TDNN model downloads
~22 MB from Hugging Face Hub. Pulling that onto a background thread at
app startup keeps the UI responsive and means the model is ready by
the time the user opens the voice-enrollment dialog or records a
meeting.

Detects whether the model is already cached on disk so an already-warm
launch doesn't surface a misleading "downloading" message -- the cache
check is the only way to distinguish a fast offline load from a slow
first-time download since SpeechBrain's `from_hparams` doesn't report
download progress out of the box.

QObject + signal layer is in EncoderPrewarmThread at the bottom. The
state-determination helper (`expected_cache_files_present`) is testable
without Qt.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional


log = logging.getLogger(__name__)


# Filenames SpeechBrain copies into our savedir on a successful
# `from_hparams`. Presence of all of these means the model is cached
# and the next load is offline; absence of any means a download is
# pending. This is the surface we use to distinguish "downloading"
# from "loading from disk" for the status-bar message.
_EXPECTED_CACHE_FILES = (
    "hyperparams.yaml",
    "embedding_model.ckpt",
    "mean_var_norm_emb.ckpt",
    "classifier.ckpt",
    "label_encoder.txt",
)


def expected_cache_files_present(cache_dir: Path) -> bool:
    """True if all expected ECAPA model files are already on disk.

    Used to decide whether the pre-warm needs to surface a
    "downloading" message vs a silent fast load.
    """
    if not cache_dir.exists():
        return False
    for name in _EXPECTED_CACHE_FILES:
        if not (cache_dir / name).exists():
            return False
    return True


try:
    from PyQt6.QtCore import QThread, pyqtSignal

    class EncoderPrewarmThread(QThread):
        """Pre-loads the SpeechBrain ECAPA-TDNN encoder in the background.

        Emits one of:
        - `download_started` when the model isn't cached and a fetch is
          about to run. UI surfaces this as a status-bar message.
        - `finished_ok` when the encoder is loaded and ready (whether
          download happened or not).
        - `failed(message)` if loading fails (no network, unavailable
          dep, etc.). The app logs but does not crash; speaker ID
          simply remains unavailable until the user retries.
        """

        download_started = pyqtSignal()
        finished_ok = pyqtSignal()
        failed = pyqtSignal(str)

        def __init__(self, parent=None) -> None:
            super().__init__(parent)

        def run(self) -> None:
            # Imports are kept local so the prewarm module is itself
            # cheap to import in test environments without SpeechBrain.
            try:
                from . import embeddings as _embeddings
                from .embeddings import (
                    EmbedderUnavailable,
                    _model_cache_dir,
                    default_encoder,
                )
            except Exception as exc:  # pragma: no cover - defensive
                self.failed.emit(str(exc))
                return

            if default_encoder.is_loaded:
                self.finished_ok.emit()
                return
            if not default_encoder.is_available:
                self.failed.emit(
                    "SpeechBrain or torch is not importable; speaker "
                    "identification is disabled until the dependencies "
                    "are installed."
                )
                return

            cache_dir = _model_cache_dir()
            if not expected_cache_files_present(cache_dir):
                self.download_started.emit()
                log.info(
                    "encoder pre-warm: model not cached, fetching from HF Hub"
                )

            try:
                default_encoder._load()  # noqa: SLF001 -- intentional warm
            except EmbedderUnavailable as exc:
                log.warning("encoder pre-warm failed: %s", exc)
                self.failed.emit(str(exc))
                return
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("encoder pre-warm raised unexpectedly")
                self.failed.emit(str(exc))
                return
            self.finished_ok.emit()
except ImportError:  # pragma: no cover - pure-python tests without PyQt6
    EncoderPrewarmThread = None  # type: ignore[assignment,misc]
