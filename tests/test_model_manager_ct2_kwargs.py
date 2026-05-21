"""Verify cpu_threads + num_workers flow into WhisperModel construction.

faster_whisper isn't required in every dev env (the test path needs to
work without it installed), so we inject a fake faster_whisper module
into sys.modules before exercising model_manager.get_model. The fake
records every kwarg passed to WhisperModel(...), which is what we
actually want to pin down here.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace, ModuleType
from typing import Any

import pytest


def _install_fake_faster_whisper(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace `faster_whisper` in sys.modules with a recorder. Returns the
    dict of captured kwargs from the most recent WhisperModel(...) call."""
    captured: dict[str, Any] = {}

    class FakeWhisperModel:
        def __init__(self, **kwargs: Any) -> None:
            captured.clear()
            captured.update(kwargs)

    module = ModuleType("faster_whisper")
    module.WhisperModel = FakeWhisperModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    return captured


def test_explicit_cpu_threads_and_num_workers_forwarded(monkeypatch, tmp_path, isolated_data_dir):
    captured = _install_fake_faster_whisper(monkeypatch)
    # Force a flat-snapshot directory to skip the HF cache path entirely.
    snapshot = tmp_path / "small.en"
    snapshot.mkdir()
    for name in ("model.bin", "config.json", "tokenizer.json"):
        (snapshot / name).write_bytes(b"")

    from meeting_notetaker.transcription import model_manager
    model_manager.unload()

    model_manager.get_model(
        "small.en",
        cpu_threads=6,
        num_workers=2,
        download_root=tmp_path,
    )

    assert captured.get("cpu_threads") == 6
    assert captured.get("num_workers") == 2


def test_default_zero_threads_omitted_from_call(monkeypatch, tmp_path, isolated_data_dir):
    """cpu_threads=0 means 'let CT2 pick': we must NOT forward the kwarg
    in that case, since passing 0 would force CT2 onto a single thread."""
    captured = _install_fake_faster_whisper(monkeypatch)
    snapshot = tmp_path / "small.en"
    snapshot.mkdir()
    for name in ("model.bin", "config.json", "tokenizer.json"):
        (snapshot / name).write_bytes(b"")

    from meeting_notetaker.transcription import model_manager
    model_manager.unload()

    model_manager.get_model(
        "small.en",
        cpu_threads=0,
        num_workers=1,
        download_root=tmp_path,
    )

    assert "cpu_threads" not in captured
    # num_workers=1 is also the CT2 default; omitted.
    assert "num_workers" not in captured


def test_changed_kwargs_force_model_reload(monkeypatch, tmp_path, isolated_data_dir):
    """The per-size cache must invalidate when CT2 knobs change, since
    cpu_threads / num_workers are fixed at WhisperModel construct time."""
    captured = _install_fake_faster_whisper(monkeypatch)
    snapshot = tmp_path / "small.en"
    snapshot.mkdir()
    for name in ("model.bin", "config.json", "tokenizer.json"):
        (snapshot / name).write_bytes(b"")

    from meeting_notetaker.transcription import model_manager
    model_manager.unload()

    model_manager.get_model("small.en", cpu_threads=4, num_workers=2, download_root=tmp_path)
    first = dict(captured)
    # Same size, different knob -> must rebuild (captured updates).
    model_manager.get_model("small.en", cpu_threads=8, num_workers=2, download_root=tmp_path)
    assert captured != first
    assert captured.get("cpu_threads") == 8

    # Same args again -> cache hit, captured stays as the last build.
    last = dict(captured)
    model_manager.get_model("small.en", cpu_threads=8, num_workers=2, download_root=tmp_path)
    assert captured == last
