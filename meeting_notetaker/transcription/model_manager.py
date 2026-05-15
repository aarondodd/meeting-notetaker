"""faster-whisper model loader + per-size cache.

The first call to get_model(size) loads the model into memory (downloading
weights to the configured download_root if absent). Subsequent calls with
the same size return the cached instance. A different size triggers a load
and the prior instance is dropped.

Designed to be called from a worker thread, but the underlying
WhisperModel.transcribe() method is thread-safe per upstream docs, so two
worker threads (one per source) can share the same model instance.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable, Optional

from ..utils.paths import models_dir


log = logging.getLogger(__name__)


_lock = threading.Lock()
_current_model = None
_current_size: Optional[str] = None


def get_model(
    size: str,
    *,
    device: str = "cpu",
    compute_type: str = "int8",
    download_root: Optional[Path] = None,
    progress: Optional[Callable[[str], None]] = None,
):
    """Return a faster_whisper.WhisperModel instance for `size`.

    Lazily imports faster_whisper so that test environments without it can
    still import this module.
    """
    global _current_model, _current_size
    with _lock:
        if _current_model is not None and _current_size == size:
            return _current_model
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]

        root = str(download_root or models_dir())
        if progress:
            progress(f"Loading {size} model (compute_type={compute_type})...")
        log.info("Loading faster-whisper model size=%s device=%s compute_type=%s", size, device, compute_type)
        model = WhisperModel(
            model_size_or_path=size,
            device=device,
            compute_type=compute_type,
            download_root=root,
            local_files_only=False,
        )
        _current_model = model
        _current_size = size
        if progress:
            progress("Model ready.")
        return model


def current_size() -> Optional[str]:
    """The size of the currently loaded model, or None if nothing is loaded."""
    with _lock:
        return _current_size


def unload() -> None:
    """Drop the cached model instance. Useful for tests and 'free memory' menus."""
    global _current_model, _current_size
    with _lock:
        _current_model = None
        _current_size = None
