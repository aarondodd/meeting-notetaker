"""Per-session capture-only override.

NewSessionDialog returns None when the user didn't deviate from the
global default, True/False when they did. start_session() honors the
override.

We don't exercise Qt event loops here -- NewSessionResult is a plain
dataclass and SessionController.start_session can be exercised against
fakes for the audio + worker dependencies. The point of these tests is
to pin the override contract.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from meeting_notetaker.ui.new_session_dialog import NewSessionResult


def test_result_dataclass_carries_override():
    r = NewSessionResult(title="x", retain_audio=False, capture_only_override=True)
    assert r.capture_only_override is True

    r2 = NewSessionResult(title="x", retain_audio=False, capture_only_override=False)
    assert r2.capture_only_override is False

    # None means "use the global default".
    r3 = NewSessionResult(title="x", retain_audio=False)
    assert r3.capture_only_override is None


def test_resolved_cpu_threads_uses_num_workers_split():
    """End-to-end smoke that the Config helper still does the auto math.
    Mirrors the helper test but expressed against the path the controller
    actually calls (config.transcription.resolved_cpu_threads())."""
    from meeting_notetaker.utils.config import Config
    cfg = Config()
    cfg.transcription.cpu_threads = 0
    cfg.transcription.num_workers = 2
    # 12-core target lands at 6.
    assert cfg.transcription.resolved_cpu_threads(cpu_count=12) == 6
