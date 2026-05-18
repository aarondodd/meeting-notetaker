"""Pure-Python tests for the ad-hoc meeting auto-detect logic.

The pycaw enumeration side is unimportable on Linux; everything below
drives the public pure-logic surface (snapshot filtering, run tracking,
debounce, cooldown) directly.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta

import pytest

from meeting_notetaker.integrations.audio_session_monitor import (
    DEFAULT_APP_ALLOWLIST,
    MeetingAudioInfo,
    _advance_runs,
    _CooldownStore,
    _filter_allowlisted,
    _RunState,
    _runs_ready_to_fire,
    _SessionSnapshot,
    display_name_for,
    is_available,
)


# ---- allowlist filter -----------------------------------------------------


def test_filter_allowlisted_matches_case_insensitively():
    snaps = [
        _SessionSnapshot("Teams.exe", peak=0.4),
        _SessionSnapshot("TEAMS.exe", peak=0.5),
        _SessionSnapshot("teams.EXE", peak=0.6),
        _SessionSnapshot("chrome.exe", peak=0.9),
    ]
    out = _filter_allowlisted(snaps, ["teams.exe"], silence_floor=0.0)
    assert len(out) == 3
    assert all(s.process_name.lower() == "teams.exe" for s in out)


def test_filter_allowlisted_drops_below_silence_floor():
    snaps = [
        _SessionSnapshot("Zoom.exe", peak=0.001),
        _SessionSnapshot("Zoom.exe", peak=0.5),
    ]
    out = _filter_allowlisted(snaps, ["Zoom.exe"], silence_floor=0.01)
    assert len(out) == 1
    assert out[0].peak == 0.5


def test_filter_allowlisted_skips_non_allowlisted_apps():
    snaps = [
        _SessionSnapshot("spotify.exe", peak=0.9),
        _SessionSnapshot("Teams.exe", peak=0.5),
    ]
    out = _filter_allowlisted(snaps, ["Teams.exe"], silence_floor=0.0)
    assert [s.process_name for s in out] == ["Teams.exe"]


# ---- run state advancement ------------------------------------------------


def test_advance_runs_starts_run_for_new_active_session():
    runs: dict[str, _RunState] = {}
    now = datetime(2026, 5, 18, 10, 0, 0)
    _advance_runs(runs, [_SessionSnapshot("Teams.exe", 0.5)], now)
    assert "Teams.exe" in runs
    assert runs["Teams.exe"].started_at == now
    assert runs["Teams.exe"].fired is False


def test_advance_runs_keeps_existing_run_alive_and_updates_last_active():
    now = datetime(2026, 5, 18, 10, 0, 0)
    later = now + timedelta(seconds=10)
    runs = {
        "Teams.exe": _RunState(
            process_name="Teams.exe",
            started_at=now,
            last_active_at=now,
        )
    }
    _advance_runs(runs, [_SessionSnapshot("Teams.exe", 0.5)], later)
    assert runs["Teams.exe"].started_at == now
    assert runs["Teams.exe"].last_active_at == later


def test_advance_runs_drops_run_when_session_disappears():
    now = datetime(2026, 5, 18, 10, 0, 0)
    runs = {
        "Teams.exe": _RunState(
            process_name="Teams.exe",
            started_at=now,
            last_active_at=now,
        )
    }
    expired = _advance_runs(runs, [], now + timedelta(seconds=5))
    assert expired == ["Teams.exe"]
    assert runs == {}


# ---- ready-to-fire selection ---------------------------------------------


def test_runs_ready_to_fire_emits_only_after_min_duration():
    now = datetime(2026, 5, 18, 10, 0, 0)
    runs = {
        "Teams.exe": _RunState(
            "Teams.exe", started_at=now, last_active_at=now + timedelta(seconds=10),
        ),
    }
    assert _runs_ready_to_fire(runs, {}, min_duration_sec=25, now=now + timedelta(seconds=10)) == []
    runs["Teams.exe"].last_active_at = now + timedelta(seconds=30)
    ready = _runs_ready_to_fire(
        runs, {}, min_duration_sec=25, now=now + timedelta(seconds=30)
    )
    assert len(ready) == 1
    assert ready[0].process_name == "Teams.exe"


def test_runs_ready_to_fire_skips_already_fired_runs():
    now = datetime(2026, 5, 18, 10, 0, 0)
    run = _RunState("Teams.exe", started_at=now, last_active_at=now + timedelta(seconds=60))
    run.fired = True
    assert _runs_ready_to_fire({"Teams.exe": run}, {}, 25, now + timedelta(seconds=60)) == []


def test_runs_ready_to_fire_respects_cooldown():
    now = datetime(2026, 5, 18, 10, 0, 0)
    runs = {
        "Teams.exe": _RunState(
            "Teams.exe", started_at=now, last_active_at=now + timedelta(seconds=60),
        ),
    }
    cooldowns = {"Teams.exe": now + timedelta(minutes=5)}
    assert _runs_ready_to_fire(runs, cooldowns, 25, now + timedelta(seconds=60)) == []
    # Past the cooldown window: eligible again (assuming run wasn't fired).
    assert _runs_ready_to_fire(runs, cooldowns, 25, now + timedelta(minutes=6)) != []


# ---- cooldown persistence -------------------------------------------------


def test_cooldown_store_roundtrip(tmp_path):
    path = tmp_path / "cooldowns.json"
    store = _CooldownStore(path)
    expiry = datetime(2099, 1, 1, 0, 0, 0)
    store.set("Teams.exe", expiry)
    active = store.active()
    assert active["Teams.exe"] == expiry


def test_cooldown_store_prunes_expired_entries_on_read(tmp_path):
    path = tmp_path / "cooldowns.json"
    # Hand-write a payload with one already-expired entry + one live one.
    now = datetime(2026, 5, 18, 10, 0, 0)
    path.write_text(
        json.dumps({
            "Teams.exe": (now - timedelta(hours=1)).isoformat(),
            "Zoom.exe": (now + timedelta(hours=1)).isoformat(),
        }),
        encoding="utf-8",
    )
    store = _CooldownStore(path, now=lambda: now)
    active = store.active()
    assert "Teams.exe" not in active
    assert "Zoom.exe" in active
    # The prune is persisted: a fresh load reads the cleaned-up file.
    reloaded = json.loads(path.read_text(encoding="utf-8"))
    assert "Teams.exe" not in reloaded


def test_cooldown_store_clear_removes_entry(tmp_path):
    path = tmp_path / "cooldowns.json"
    store = _CooldownStore(path)
    store.set("Teams.exe", datetime(2099, 1, 1))
    store.clear("Teams.exe")
    assert "Teams.exe" not in store.active()


def test_cooldown_store_handles_missing_and_corrupt_files(tmp_path):
    path = tmp_path / "nope.json"
    assert _CooldownStore(path).active() == {}
    path.write_text("not json {{{", encoding="utf-8")
    assert _CooldownStore(path).active() == {}


# ---- display name + availability ------------------------------------------


def test_display_name_known_apps():
    assert display_name_for("Teams.exe") == "Microsoft Teams"
    assert display_name_for("ms-teams.exe") == "Microsoft Teams"
    assert display_name_for("Zoom.exe") == "Zoom"
    assert display_name_for("slack.exe") == "Slack"


def test_display_name_unknown_strips_suffix():
    assert display_name_for("foo.exe") == "foo"
    assert display_name_for("Foo.Bar.exe") == "Foo.Bar"


def test_default_allowlist_has_teams_and_zoom():
    norm = {a.lower() for a in DEFAULT_APP_ALLOWLIST}
    assert "teams.exe" in norm
    assert "zoom.exe" in norm


def test_is_available_returns_false_on_linux_test_env():
    # The pure-Python test env has neither pycaw nor psutil installed; even
    # if they were, sys.platform won't say win32 here. Guarding the check
    # makes sure is_available() never crashes from a missing import.
    assert is_available() is False


# ---- info shape -----------------------------------------------------------


def test_meeting_audio_info_fields():
    info = MeetingAudioInfo(
        process_name="Teams.exe",
        app_label="Microsoft Teams",
        first_detected_at=datetime(2026, 5, 18, 10, 30, 0),
        sustained_seconds=42.0,
    )
    assert info.app_label == "Microsoft Teams"
    assert info.sustained_seconds == 42.0


# ---- end-to-end QObject state machine (offscreen Qt) ---------------------


pytest.importorskip("PyQt6.QtCore")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qt_app():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def test_monitor_emits_after_sustained_run(qt_app, tmp_path):
    from meeting_notetaker.integrations.audio_session_monitor import (
        AudioSessionMonitor,
        _SessionSnapshot,
    )
    if AudioSessionMonitor is None:
        pytest.skip("PyQt6 not available")
    state_path = tmp_path / "cooldowns.json"
    monitor = AudioSessionMonitor(
        state_path,
        allowlist=["Teams.exe"],
        min_duration_sec=20,
        cooldown_minutes=5,
    )
    received: list[MeetingAudioInfo] = []
    monitor.meeting_audio_detected.connect(lambda info: received.append(info))

    start = datetime(2026, 5, 18, 10, 0, 0)
    # Tick 1: Teams just started; no fire yet.
    monitor._process_snapshots([_SessionSnapshot("Teams.exe", 0.5)], start)
    assert received == []
    # Tick 2 (15s in): still under min_duration.
    monitor._process_snapshots(
        [_SessionSnapshot("Teams.exe", 0.5)], start + timedelta(seconds=15)
    )
    assert received == []
    # Tick 3 (25s in): crosses threshold -> emit once.
    monitor._process_snapshots(
        [_SessionSnapshot("Teams.exe", 0.5)], start + timedelta(seconds=25)
    )
    assert len(received) == 1
    assert received[0].app_label == "Microsoft Teams"
    # Tick 4 (40s in): same run; should NOT re-emit.
    monitor._process_snapshots(
        [_SessionSnapshot("Teams.exe", 0.5)], start + timedelta(seconds=40)
    )
    assert len(received) == 1


def test_monitor_suppresses_when_recording_callback_true(qt_app, tmp_path):
    from meeting_notetaker.integrations.audio_session_monitor import (
        AudioSessionMonitor,
    )
    if AudioSessionMonitor is None:
        pytest.skip("PyQt6 not available")
    state_path = tmp_path / "cooldowns.json"
    monitor = AudioSessionMonitor(
        state_path,
        allowlist=["Teams.exe"],
        min_duration_sec=20,
        cooldown_minutes=5,
        is_recording=lambda: True,
    )
    received: list[MeetingAudioInfo] = []
    monitor.meeting_audio_detected.connect(lambda info: received.append(info))
    # _tick() must early-return; calling it directly is the cheapest assertion.
    monitor._tick()
    assert received == []


def test_monitor_cooldown_blocks_second_run(qt_app, tmp_path):
    from meeting_notetaker.integrations.audio_session_monitor import (
        AudioSessionMonitor,
        _SessionSnapshot,
    )
    if AudioSessionMonitor is None:
        pytest.skip("PyQt6 not available")
    state_path = tmp_path / "cooldowns.json"
    monitor = AudioSessionMonitor(
        state_path,
        allowlist=["Teams.exe"],
        min_duration_sec=10,
        cooldown_minutes=5,
    )
    received: list[MeetingAudioInfo] = []
    monitor.meeting_audio_detected.connect(lambda info: received.append(info))

    t0 = datetime(2026, 5, 18, 10, 0, 0)
    # First run: detect and emit.
    monitor._process_snapshots([_SessionSnapshot("Teams.exe", 0.5)], t0)
    monitor._process_snapshots(
        [_SessionSnapshot("Teams.exe", 0.5)], t0 + timedelta(seconds=12)
    )
    assert len(received) == 1
    # Audio stops, then re-starts inside the cooldown window: must NOT fire.
    monitor._process_snapshots([], t0 + timedelta(seconds=20))
    monitor._process_snapshots(
        [_SessionSnapshot("Teams.exe", 0.5)], t0 + timedelta(seconds=30)
    )
    monitor._process_snapshots(
        [_SessionSnapshot("Teams.exe", 0.5)], t0 + timedelta(seconds=45)
    )
    assert len(received) == 1
