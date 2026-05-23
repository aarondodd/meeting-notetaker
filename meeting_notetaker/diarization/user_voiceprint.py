"""Persistent storage for the user's voice embedding ("voiceprint").

Backs the Settings > Speaker Identification enrollment flow. Stored as a
single JSON file at `<app_data>/user_voiceprint.json` rather than as a
row in `speakers.db` so it can't accidentally be matched against another
speaker (the SpeakerStore.match() pass would otherwise treat the user's
own voiceprint as just another candidate, producing wrong attributions
for everyone else in the meeting).

Schema (versioned for forward compat):

    {
      "version": 1,
      "embedding": [256 floats],
      "embedding_dim": 256,
      "sample_count": 1,
      "recorded_at": "2026-05-18T...Z"
    }

The refiner consults `load()` once per session refinement; if the file is
missing, mic-source segments fall back to the legacy "user_name everywhere"
labeling. The Settings dialog and status-bar indicator both call `exists()`
to decide whether to surface the enrollment notice.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np


VERSION = 1
FILENAME = "user_voiceprint.json"


@dataclass(frozen=True)
class UserVoiceprint:
    embedding: np.ndarray
    sample_count: int
    recorded_at: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def voiceprint_path() -> Path:
    """Default location under the app data dir."""
    from ..utils.paths import app_data_dir
    return app_data_dir() / FILENAME


def save(embedding: np.ndarray, *, path: Optional[Path] = None, sample_count: int = 1) -> Path:
    """Persist a voiceprint embedding. Overwrites any prior value.

    Writes via a `.tmp` file + rename so a crash mid-write can't leave a
    half-written voiceprint that fails to parse on next launch.
    """
    target = Path(path) if path is not None else voiceprint_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    emb = np.asarray(embedding, dtype=np.float32).reshape(-1)
    payload = {
        "version": VERSION,
        "embedding": [float(x) for x in emb.tolist()],
        "embedding_dim": int(emb.size),
        "sample_count": int(sample_count),
        "recorded_at": _now_iso(),
    }
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(target)
    return target


def load(*, path: Optional[Path] = None) -> Optional[UserVoiceprint]:
    """Return the stored voiceprint, or None if no enrollment exists.

    Also returns None when the stored embedding's dimension does not
    match what the current encoder produces -- this happens after the
    encoder swap from Resemblyzer (256-dim) to ECAPA-TDNN (192-dim) in
    v0.5. Treating a stale-dim voiceprint as "not enrolled" prompts the
    user to re-record so subsequent matches use comparable vectors.
    """
    target = Path(path) if path is not None else voiceprint_path()
    if not target.exists():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    version = int(raw.get("version", 1))
    if version > VERSION:
        raise ValueError(
            f"user_voiceprint.json version {version} not understood "
            f"(this build supports up to {VERSION})"
        )
    embedding = np.asarray(raw.get("embedding", []), dtype=np.float32).reshape(-1)
    if embedding.size == 0:
        return None
    # Mismatched-dim voiceprints (e.g. stored under Resemblyzer 256-dim,
    # encoder is now ECAPA 192-dim) are treated as not enrolled. The
    # file stays on disk so the user can keep the old enrollment around
    # if they swap encoders back, but the active encoder won't pull it.
    try:
        from .embeddings import EMBEDDING_DIM as _CURRENT_DIM
    except Exception:
        _CURRENT_DIM = None
    if _CURRENT_DIM is not None and embedding.size != _CURRENT_DIM:
        return None
    return UserVoiceprint(
        embedding=embedding,
        sample_count=int(raw.get("sample_count", 1)),
        recorded_at=str(raw.get("recorded_at", "")),
    )


def exists(*, path: Optional[Path] = None) -> bool:
    """Cheap presence check used by Settings UI and the status indicator.

    Returns True if any voiceprint file is present on disk, regardless
    of whether its dimension matches the active encoder. The status-bar
    indicator (the yellow "Voiceprint" pill) is driven by `load()`, which
    is dim-aware, so a stale-dim file correctly surfaces as needing
    re-enrollment in the UI even though the file is present.
    """
    target = Path(path) if path is not None else voiceprint_path()
    return target.exists()


def clear(*, path: Optional[Path] = None) -> bool:
    """Remove the stored voiceprint. Returns True if a file was deleted."""
    target = Path(path) if path is not None else voiceprint_path()
    if not target.exists():
        return False
    target.unlink()
    return True
