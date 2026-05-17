"""Outlook diagnostic structured result (pure-Python paths)."""
from __future__ import annotations

import sys

from meeting_notetaker.integrations import outlook_calendar
from meeting_notetaker.integrations.outlook_calendar import (
    DiagnosticResult,
    diagnose,
)


def test_overall_ok_requires_all_steps():
    r = DiagnosticResult(
        platform_ok=True, pywin32_ok=True, dispatch_ok=True, namespace_ok=True
    )
    assert r.overall_ok is True


def test_overall_ok_false_when_any_step_fails():
    for missing in ("platform_ok", "pywin32_ok", "dispatch_ok", "namespace_ok"):
        kwargs = dict(
            platform_ok=True, pywin32_ok=True, dispatch_ok=True, namespace_ok=True
        )
        kwargs[missing] = False
        r = DiagnosticResult(**kwargs)
        assert r.overall_ok is False, f"expected False when {missing} is False"


def test_summary_targets_first_failure():
    r = DiagnosticResult(platform_ok=False, pywin32_ok=False)
    assert "Windows-only" in r.summary()

    r = DiagnosticResult(
        platform_ok=True,
        pywin32_ok=False,
        pywin32_error="ImportError: No module named 'win32com'",
    )
    msg = r.summary()
    assert "pywin32" in msg
    assert "pip install pywin32" in msg
    assert "No module named" in msg

    r = DiagnosticResult(
        platform_ok=True,
        pywin32_ok=True,
        dispatch_ok=False,
        dispatch_error="CoInitialize has not been called",
    )
    msg = r.summary()
    assert "Dispatch" in msg
    assert "CoInitialize" in msg

    r = DiagnosticResult(
        platform_ok=True,
        pywin32_ok=True,
        dispatch_ok=True,
        namespace_ok=False,
        namespace_error="MAPI not ready",
    )
    msg = r.summary()
    assert "MAPI" in msg
    assert "transient" in msg


def test_diagnose_returns_platform_false_on_linux():
    """Sanity check the real diagnose() function on a non-Windows host."""
    if sys.platform.startswith("win"):
        return  # smoke-only on Linux/macOS CI; skip on Windows
    r = diagnose()
    assert r.platform_ok is False
    assert r.pywin32_ok is False
    assert r.overall_ok is False
    assert "Windows-only" in r.summary()
