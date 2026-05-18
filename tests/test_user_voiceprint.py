"""Voiceprint storage round-trip + clear behavior."""
from __future__ import annotations

import json

import numpy as np
import pytest

from meeting_notetaker.diarization import user_voiceprint


def test_save_then_load_round_trip(tmp_path):
    target = tmp_path / "user_voiceprint.json"
    embedding = np.linspace(-1, 1, 256, dtype=np.float32)
    user_voiceprint.save(embedding, path=target, sample_count=1)
    assert user_voiceprint.exists(path=target)
    loaded = user_voiceprint.load(path=target)
    assert loaded is not None
    assert loaded.embedding.shape == (256,)
    assert loaded.sample_count == 1
    assert loaded.recorded_at  # ISO timestamp populated
    np.testing.assert_allclose(loaded.embedding, embedding, atol=1e-6)


def test_load_returns_none_when_missing(tmp_path):
    assert user_voiceprint.load(path=tmp_path / "nope.json") is None
    assert not user_voiceprint.exists(path=tmp_path / "nope.json")


def test_clear_removes_file(tmp_path):
    target = tmp_path / "user_voiceprint.json"
    user_voiceprint.save(np.ones(256, dtype=np.float32), path=target)
    assert user_voiceprint.clear(path=target) is True
    assert not target.exists()
    # Idempotent: clearing a missing file just returns False.
    assert user_voiceprint.clear(path=target) is False


def test_save_uses_atomic_rename(tmp_path):
    """Half-written .tmp shouldn't be loadable as the real file."""
    target = tmp_path / "user_voiceprint.json"
    user_voiceprint.save(np.zeros(256, dtype=np.float32), path=target)
    # Mid-write tmp file should not exist after save() completes.
    assert not target.with_suffix(target.suffix + ".tmp").exists()


def test_load_rejects_future_version(tmp_path):
    target = tmp_path / "user_voiceprint.json"
    target.write_text(json.dumps({
        "version": 999,
        "embedding": [0.0] * 256,
        "embedding_dim": 256,
        "sample_count": 1,
        "recorded_at": "2026-01-01T00:00:00Z",
    }))
    with pytest.raises(ValueError):
        user_voiceprint.load(path=target)


def test_load_handles_corrupt_file(tmp_path):
    target = tmp_path / "user_voiceprint.json"
    target.write_text("not json")
    assert user_voiceprint.load(path=target) is None
