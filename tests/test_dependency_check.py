"""Tests for the runtime dependency self-test."""
from __future__ import annotations

import sys

import pytest

from meeting_notetaker.utils import dependency_check
from meeting_notetaker.utils.dependency_check import (
    DependencyResult,
    Status,
    _Check,
    _probe,
    format_report,
    run_checks,
    summary,
)


def test_run_checks_returns_one_entry_per_group():
    grouped = run_checks()
    group_names = [name for name, _ in grouped]
    assert "Core" in group_names
    assert "Transcription" in group_names
    assert "Speaker identification" in group_names
    # Order is stable -- Core first, Synthesis automation last (added
    # in v0.6.3 -- prior to that, the last group was Ad-hoc detection).
    assert group_names[0] == "Core"
    assert group_names[-1] == "Synthesis automation"
    assert "Ad-hoc meeting detection (Windows)" in group_names


def test_every_result_has_status_and_feature_string():
    for _, results in run_checks():
        for r in results:
            assert isinstance(r, DependencyResult)
            assert r.name
            assert r.feature
            assert r.status in {Status.OK, Status.MISSING, Status.SKIP}


def test_windows_only_checks_skip_on_non_windows():
    if sys.platform == "win32":
        pytest.skip("test asserts the non-Windows path")
    skipped_features = set()
    for _, results in run_checks():
        for r in results:
            if r.status is Status.SKIP:
                skipped_features.add(r.name)
    # All Windows-only deps should be skipped on Linux/macOS.
    expected_skips = {
        "PyAudioWPatch",
        "truststore",
        "pywin32 (win32com.client)",
        "pywin32 (win32timezone)",
        "pycaw",
        "psutil",
    }
    assert expected_skips.issubset(skipped_features)


def test_probe_bogus_module_reports_missing():
    bogus = _Check(
        name="not-a-real-package",
        feature="Synthetic test",
        module="meeting_notetaker_nonexistent_module_zzzz",
    )
    result = _probe(bogus)
    assert result.status is Status.MISSING
    assert "meeting_notetaker_nonexistent_module_zzzz" in result.detail


def test_probe_real_module_reports_ok_with_version():
    # numpy is in requirements and definitely available in the dev venv.
    real = _Check(name="numpy", feature="Test", module="numpy")
    result = _probe(real)
    assert result.status is Status.OK
    assert "version" in result.detail or result.detail == "imported"


def test_windows_only_check_skips_on_non_windows_via_probe():
    if sys.platform == "win32":
        pytest.skip("test asserts the non-Windows path")
    win_check = _Check(
        name="pywin32", feature="Test", module="win32com.client", windows_only=True,
    )
    result = _probe(win_check)
    assert result.status is Status.SKIP
    assert "Windows-only" in result.detail


def test_summary_counts_match_individual_results():
    grouped = run_checks()
    counts = summary(grouped)
    total = sum(counts.values())
    individual_total = sum(len(results) for _, results in grouped)
    assert total == individual_total


def test_format_report_includes_each_group_heading_and_summary():
    grouped = run_checks()
    report = format_report(grouped)
    assert "Meeting Notetaker -- Dependency check" in report
    assert "## Core" in report
    assert "## Speaker identification" in report
    assert "Summary:" in report
    # Status tags appear in the right format
    assert "[     OK]" in report or "[MISSING]" in report or "[   SKIP]" in report


def test_speaker_id_group_covers_runtime_yaml_targets():
    """The whole point of the speaker-id group is to surface the leaf
    modules SpeechBrain's yaml hparams resolve at runtime. If those
    aren't in the check list, a frozen .exe with missing hidden imports
    will pass the self-test and then fail mid-meeting."""
    grouped = dict(run_checks())
    names = {r.name for r in grouped["Speaker identification"]}
    required = {
        "speechbrain.inference.speaker",
        "speechbrain.lobes.models.ECAPA_TDNN",
        "speechbrain.processing.features",
        "torch",
        "torchaudio",
    }
    assert required.issubset(names)


def test_outlook_group_covers_lazy_pywin32_submodules():
    """win32timezone is loaded lazily by win32com.client.dynamic when a
    COM property returns a datetime (item.Start / item.End on Outlook
    calendar items). PyInstaller's static analysis can't see that
    import, so it must appear in dependency_check or the build gate
    won't catch the bundling miss. Pins the 2026-05-21 incident fix."""
    grouped = dict(run_checks())
    names = {r.name for r in grouped["Outlook calendar (Windows)"]}
    assert "pywin32 (win32timezone)" in names
