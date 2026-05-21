"""faster-whisper model loader + per-size cache.

The first call to get_model(size) loads the model into memory (downloading
weights to the configured download_root if absent). Subsequent calls with
the same size return the cached instance. A different size triggers a load
and the prior instance is dropped.

Designed to be called from a worker thread, but the underlying
WhisperModel.transcribe() method is thread-safe per upstream docs, so two
worker threads (one per source) can share the same model instance.

Offline / pre-staged models
---------------------------
Users behind a corporate proxy that blocks huggingface.co can pre-stage a
model in either of two ways:

1. Drop the model files into <models_dir>/<size>/  (a flat directory with
   model.bin, config.json, tokenizer files, vocabulary.txt). We detect that
   directory and pass its absolute path to WhisperModel, which bypasses
   huggingface_hub entirely.

2. Set HF_HUB_OFFLINE=1 in the environment AND place the standard HF
   snapshot under <models_dir>/models--Systran--faster-whisper-<size>/
   (the layout faster-whisper itself creates on a successful first run on
   a clean network). With HF_HUB_OFFLINE=1, huggingface_hub never calls the
   API and just resolves from the local cache.

The first option is simpler for end users; the second mirrors the layout
faster-whisper produces naturally.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Callable, Optional

from ..utils.paths import models_dir


log = logging.getLogger(__name__)


_lock = threading.Lock()
_current_model = None
_current_size: Optional[str] = None
_current_cpu_threads: int = 0
_current_num_workers: int = 1


# Filenames a faster-whisper CTranslate2 snapshot must contain to be usable
# without re-downloading. Tokenizer is the most variant -- newer faster-
# whisper expects tokenizer.json, older fell back to vocabulary.txt.
_REQUIRED_FILES_ANY_OF: tuple[tuple[str, ...], ...] = (
    ("model.bin",),
    ("config.json",),
    ("tokenizer.json", "vocabulary.txt"),
)


def _flat_local_path(root: Path, size: str) -> Optional[Path]:
    """Return <root>/<size>/ if it looks like a complete flat snapshot, else None."""
    candidate = root / size
    if not candidate.is_dir():
        return None
    for group in _REQUIRED_FILES_ANY_OF:
        if not any((candidate / name).exists() for name in group):
            return None
    return candidate


def get_model(
    size: str,
    *,
    device: str = "cpu",
    compute_type: str = "int8",
    cpu_threads: int = 0,
    num_workers: int = 1,
    download_root: Optional[Path] = None,
    progress: Optional[Callable[[str], None]] = None,
):
    """Return a faster_whisper.WhisperModel instance for `size`.

    `cpu_threads` and `num_workers` map directly onto CTranslate2's
    same-named knobs. Defaults of 0 / 1 reproduce CT2's own defaults
    (cpu_threads = min(cores, 4), single inference worker). On a 12-core
    CPU recording two sources, callers should pass cpu_threads=6,
    num_workers=2 so each transcribe() can saturate half the cores and
    parallel mic + sys batch passes run truly in parallel inside one
    model instance. Total OS threads = cpu_threads * num_workers; keep
    that <= physical core count to avoid L3 thrash.

    A change in cpu_threads or num_workers forces a model rebuild even
    when the size matches, because CT2 fixes those at construct time.

    Lazily imports faster_whisper so that test environments without it can
    still import this module.
    """
    global _current_model, _current_size, _current_cpu_threads, _current_num_workers
    with _lock:
        if (
            _current_model is not None
            and _current_size == size
            and _current_cpu_threads == cpu_threads
            and _current_num_workers == num_workers
        ):
            return _current_model
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]

        root = Path(download_root or models_dir())
        root.mkdir(parents=True, exist_ok=True)

        # Pre-staged flat snapshot wins: pass the directory path directly
        # so huggingface_hub is never contacted.
        flat = _flat_local_path(root, size)
        if flat is not None:
            log.info("Using pre-staged faster-whisper snapshot at %s", flat)
            if progress:
                progress(f"Loading {size} model from local snapshot...")
            model_arg: str = str(flat)
            local_only = True
        else:
            # Standard path: faster-whisper resolves through huggingface_hub
            # against <root>/models--<repo>--<size>/. If HF_HUB_OFFLINE=1 is
            # set, the API call is skipped and only the local cache is used.
            model_arg = size
            local_only = os.environ.get("HF_HUB_OFFLINE", "0") not in ("", "0", "false", "False")
            if progress:
                progress(f"Loading {size} model (compute_type={compute_type})...")

        log.info(
            "Loading faster-whisper size=%s device=%s compute_type=%s "
            "cpu_threads=%s num_workers=%s local_only=%s",
            size, device, compute_type, cpu_threads, num_workers, local_only,
        )
        kwargs: dict = dict(
            model_size_or_path=model_arg,
            device=device,
            compute_type=compute_type,
            download_root=str(root),
            local_files_only=local_only,
        )
        # Only forward CT2 knobs when explicitly set; 0 means "let CT2
        # pick its own default" so we don't override on tiny dev VMs.
        if cpu_threads and cpu_threads > 0:
            kwargs["cpu_threads"] = cpu_threads
        if num_workers and num_workers > 1:
            kwargs["num_workers"] = num_workers
        try:
            model = WhisperModel(**kwargs)
        except Exception as exc:
            msg = _friendly_error(exc, size, root)
            log.error("model load failed: %s", msg)
            raise RuntimeError(msg) from exc

        _current_model = model
        _current_size = size
        _current_cpu_threads = cpu_threads
        _current_num_workers = num_workers
        if progress:
            progress("Model ready.")
        return model


def current_size() -> Optional[str]:
    """The size of the currently loaded model, or None if nothing is loaded."""
    with _lock:
        return _current_size


def unload() -> None:
    """Drop the cached model instance. Useful for tests and 'free memory' menus."""
    global _current_model, _current_size, _current_cpu_threads, _current_num_workers
    with _lock:
        _current_model = None
        _current_size = None
        _current_cpu_threads = 0
        _current_num_workers = 1


def _friendly_error(exc: Exception, size: str, root: Path) -> str:
    """Translate huggingface_hub / TLS errors into actionable guidance."""
    text = str(exc)
    is_ssl = "CERTIFICATE_VERIFY_FAILED" in text or "self-signed certificate" in text or "self signed certificate" in text
    is_net = "ConnectError" in text or "LocalEntryNotFoundError" in text or "Failed to resolve" in text
    if is_ssl:
        return (
            "Could not download the Whisper model: TLS verification failed against "
            "huggingface.co. This usually means a corporate proxy (Netskope, Zscaler) "
            "is intercepting the connection. Two fixes:\n"
            "  1) Make sure the 'truststore' package is installed (it is in "
            "requirements.txt; re-run 'pip install -r requirements.txt'). It uses "
            "the Windows certificate store so the proxy CA is trusted.\n"
            f"  2) Pre-stage the model: download '{size}' (model.bin, config.json, "
            f"tokenizer.json) on a network that allows huggingface.co, drop the files "
            f"into {root / size}, then start the app again.\n"
            f"Underlying error: {text}"
        )
    if is_net:
        return (
            "Could not download the Whisper model from huggingface.co. Check your "
            "internet connection, or pre-stage the model: download '"
            f"{size}' (model.bin, config.json, tokenizer.json) and drop the files "
            f"into {root / size}.\n"
            f"Underlying error: {text}"
        )
    return text
