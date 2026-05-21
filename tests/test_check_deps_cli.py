"""--check-deps CLI flag behavior.

Runs main.py as a subprocess so we exercise the exact path the build
script invokes on the frozen .exe.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAIN_PY = PROJECT_ROOT / "main.py"


def _run(extra_args: list[str], *, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    import os
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    # Don't let the running test's QT_QPA_PLATFORM=offscreen leak into a
    # subprocess that might import PyQt6 (we don't want to require Qt for
    # the check-deps subprocess to work).
    env.pop("QT_QPA_PLATFORM", None)
    return subprocess.run(
        [sys.executable, str(MAIN_PY), *extra_args],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        env=env,
        timeout=120,
    )


def _has_all_required_for_clean_run() -> bool:
    """Skip the all-OK assertion when the dev venv doesn't have every
    optional dep installed (Linux dev hosts often lack pyaudio etc.)."""
    try:
        import faster_whisper  # noqa: F401
        import pyaudio  # noqa: F401
        import sounddevice  # noqa: F401
        import speechbrain  # noqa: F401
        import webrtcvad  # noqa: F401
        return True
    except ImportError:
        return False


def test_check_deps_flag_prints_report():
    result = _run(["--check-deps"])
    assert "Meeting Notetaker -- Dependency check" in result.stdout
    assert "## Core" in result.stdout
    assert "## Speaker identification" in result.stdout
    assert "Summary:" in result.stdout


def test_check_deps_exits_nonzero_when_any_module_missing():
    """On a dev venv that doesn't have every dep installed, the flag
    must exit non-zero. This is the actual build-gate failure mode --
    PyInstaller produced a binary with a missing import."""
    if _has_all_required_for_clean_run():
        pytest.skip("dev venv has every dep -- can't exercise the failure path")
    result = _run(["--check-deps"])
    assert result.returncode != 0
    assert "MISSING" in result.stdout


def test_check_deps_does_not_launch_ui():
    """The flag must short-circuit before importing meeting_notetaker.app.
    A failed UI import (PyQt6 missing) would otherwise mask the dependency
    report -- the whole point is to surface the report cleanly even when
    something is broken."""
    result = _run(["--check-deps"])
    # Successful or failed return code, the report must reach stdout.
    assert "Summary:" in result.stdout
    # If the UI had loaded, we'd see Qt platform plugin warnings on stderr.
    # Don't assert empty stderr (subprocess can emit noise from torch CUDA
    # probes, etc.); just confirm we got the structured report.


def test_check_deps_in_clean_env_still_reports_dev_venv_deps():
    """The build gate must run --check-deps in an environment that does
    NOT see the dev venv's site-packages, so the report reflects what
    the frozen .exe will actually find at runtime on a user machine.

    Reproduces the 2026-05-22 incident: sounddevice was missing from the
    PyInstaller bundle (dynamic importlib lookup is invisible to static
    analysis), but the build.ps1 gate said OK because the activated venv
    was leaking through. This test confirms we can run --check-deps with
    a sanitized env and still get an honest report.
    """
    if not _has_all_required_for_clean_run():
        pytest.skip(
            "dev venv is incomplete; the env-isolation contract can't "
            "be exercised without a baseline OK run"
        )
    # Run with VIRTUAL_ENV/PYTHONHOME/PYTHONPATH cleared. The subprocess
    # inherits sys.executable's site-packages (since we use the same
    # python binary), so this is a smoke that the flag works under env
    # sanitization rather than a true frozen-bundle replay. Catches
    # accidental reliance on env vars in the check itself.
    result = _run(
        ["--check-deps"],
        env_extra={"VIRTUAL_ENV": "", "PYTHONHOME": "", "PYTHONPATH": ""},
    )
    assert "Summary:" in result.stdout
    # Either OK or MISSING is acceptable here (it depends on whether the
    # dev venv site-packages still resolves under the cleared env). The
    # assertion is just that the report ran to completion without env-
    # sanitization breaking it.


def test_no_flag_does_not_run_check_deps():
    """Sanity: running main.py with an unrelated arg doesn't trigger the
    check (i.e. we won't accidentally gate a user-facing launch)."""
    # We can't actually launch the UI from a test, but we can confirm
    # the check-deps output doesn't appear when the flag isn't passed.
    # Send a no-op arg that the app won't recognize but won't crash on
    # either. We expect the process to attempt to start the UI and
    # likely error out (offscreen Qt would crash without a display) --
    # what we assert is the absence of the dependency-check banner.
    result = _run(["--this-is-not-a-real-arg"], env_extra={"QT_QPA_PLATFORM": "offscreen"})
    assert "Meeting Notetaker -- Dependency check" not in result.stdout
