"""Encoder pre-warm cache-detection + Qt signal flow.

The cache-detection helper is testable without Qt or SpeechBrain; the
Qt-side state machine is exercised under the offscreen platform with
the encoder swapped for a fake to keep the test fast and offline.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from meeting_notetaker.diarization.encoder_prewarm import (
    expected_cache_files_present,
)


# ---- cache-detection helper (no Qt, no SpeechBrain) ----------------------


def test_expected_cache_files_present_returns_false_for_missing_dir(tmp_path):
    assert expected_cache_files_present(tmp_path / "absent") is False


def test_expected_cache_files_present_returns_false_when_any_file_is_missing(tmp_path):
    cache = tmp_path / "ecapa"
    cache.mkdir()
    # Drop in only some of the expected filenames; helper must return False.
    (cache / "hyperparams.yaml").write_text("k: v")
    (cache / "embedding_model.ckpt").write_bytes(b"\x00")
    assert expected_cache_files_present(cache) is False


def test_expected_cache_files_present_returns_true_when_all_present(tmp_path):
    cache = tmp_path / "ecapa"
    cache.mkdir()
    for name in (
        "hyperparams.yaml",
        "embedding_model.ckpt",
        "mean_var_norm_emb.ckpt",
        "classifier.ckpt",
        "label_encoder.txt",
    ):
        (cache / name).write_bytes(b"\x00")
    assert expected_cache_files_present(cache) is True


# ---- Qt state-machine end-to-end (offscreen) -----------------------------


pytest.importorskip("PyQt6.QtCore")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qt_app():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


class _StubEncoder:
    """Stand-in for default_encoder. Driven entirely by the test."""

    def __init__(
        self,
        *,
        is_loaded: bool = False,
        is_available: bool = True,
        load_raises: BaseException | None = None,
    ) -> None:
        self.is_loaded = is_loaded
        self.is_available = is_available
        self._load_calls = 0
        self._load_raises = load_raises

    def _load(self):  # noqa: D401 - mimic real API
        self._load_calls += 1
        if self._load_raises is not None:
            raise self._load_raises


def _drain_signals(qt_app) -> None:
    """Pump the Qt event loop briefly so queued signals dispatched
    from a background thread arrive at their main-thread slots before
    we make assertions."""
    from PyQt6.QtCore import QCoreApplication
    for _ in range(20):
        QCoreApplication.processEvents()


def test_prewarm_emits_finished_ok_when_already_loaded(qt_app, monkeypatch, isolated_data_dir):
    """If the encoder is already loaded (cached + prior _load()), the
    pre-warm thread must skip the download path and emit finished_ok
    directly so the app's status bar stays quiet on warm boots."""
    from meeting_notetaker.diarization import embeddings
    from meeting_notetaker.diarization.encoder_prewarm import EncoderPrewarmThread

    if EncoderPrewarmThread is None:
        pytest.skip("PyQt6 not available")

    stub = _StubEncoder(is_loaded=True, is_available=True)
    monkeypatch.setattr(embeddings, "default_encoder", stub)

    received: dict[str, int] = {"download": 0, "ok": 0, "failed": 0}
    thread = EncoderPrewarmThread()
    thread.download_started.connect(lambda: received.__setitem__("download", received["download"] + 1))
    thread.finished_ok.connect(lambda: received.__setitem__("ok", received["ok"] + 1))
    thread.failed.connect(lambda _msg: received.__setitem__("failed", received["failed"] + 1))
    thread.start()
    thread.wait(2000)
    _drain_signals(qt_app)

    assert received == {"download": 0, "ok": 1, "failed": 0}
    assert stub._load_calls == 0


def test_prewarm_emits_failed_when_dep_unavailable(qt_app, monkeypatch):
    """If SpeechBrain isn't importable, fail cleanly rather than crash
    or hang. The app surfaces this as a non-fatal status message."""
    from meeting_notetaker.diarization import embeddings
    from meeting_notetaker.diarization.encoder_prewarm import EncoderPrewarmThread

    if EncoderPrewarmThread is None:
        pytest.skip("PyQt6 not available")

    stub = _StubEncoder(is_loaded=False, is_available=False)
    monkeypatch.setattr(embeddings, "default_encoder", stub)

    received_failed: list[str] = []
    received_ok: list[bool] = []
    thread = EncoderPrewarmThread()
    thread.failed.connect(lambda msg: received_failed.append(msg))
    thread.finished_ok.connect(lambda: received_ok.append(True))
    thread.start()
    thread.wait(2000)
    _drain_signals(qt_app)

    assert received_ok == []
    assert len(received_failed) == 1
    assert "speechbrain" in received_failed[0].lower() or "torch" in received_failed[0].lower()
    assert stub._load_calls == 0


def test_prewarm_surfaces_download_started_when_cache_is_empty(qt_app, monkeypatch, isolated_data_dir):
    """Cold first launch: model isn't cached, pre-warm must emit
    download_started before _load() so the UI shows the "downloading"
    status message."""
    from meeting_notetaker.diarization import embeddings, encoder_prewarm

    if encoder_prewarm.EncoderPrewarmThread is None:
        pytest.skip("PyQt6 not available")

    stub = _StubEncoder(is_loaded=False, is_available=True)
    monkeypatch.setattr(embeddings, "default_encoder", stub)
    # Force the cache-check helper to report "empty" so the download path runs.
    monkeypatch.setattr(
        encoder_prewarm, "expected_cache_files_present", lambda _p: False
    )

    download_seen = []
    ok_seen = []
    thread = encoder_prewarm.EncoderPrewarmThread()
    thread.download_started.connect(lambda: download_seen.append(True))
    thread.finished_ok.connect(lambda: ok_seen.append(True))
    thread.start()
    thread.wait(2000)
    _drain_signals(qt_app)

    assert download_seen == [True]
    assert ok_seen == [True]
    assert stub._load_calls == 1


def test_prewarm_skips_download_message_when_cache_is_warm(qt_app, monkeypatch, isolated_data_dir):
    """Warm relaunch: model is already on disk, the load is fast and
    silent. No "downloading..." flash in the status bar."""
    from meeting_notetaker.diarization import embeddings, encoder_prewarm

    if encoder_prewarm.EncoderPrewarmThread is None:
        pytest.skip("PyQt6 not available")

    stub = _StubEncoder(is_loaded=False, is_available=True)
    monkeypatch.setattr(embeddings, "default_encoder", stub)
    monkeypatch.setattr(
        encoder_prewarm, "expected_cache_files_present", lambda _p: True
    )

    download_seen = []
    ok_seen = []
    thread = encoder_prewarm.EncoderPrewarmThread()
    thread.download_started.connect(lambda: download_seen.append(True))
    thread.finished_ok.connect(lambda: ok_seen.append(True))
    thread.start()
    thread.wait(2000)
    _drain_signals(qt_app)

    assert download_seen == []
    assert ok_seen == [True]
    assert stub._load_calls == 1
