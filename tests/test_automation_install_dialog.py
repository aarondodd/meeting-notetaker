"""Install wizard: chrome.exe locator + dialog smoke.

The dialog itself needs a QApplication; the locator helper is pure
Python and gets a focused test for the path-resolution logic that
caused Bug 1 (Windows "we can't open this 'chrome' link").
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from meeting_notetaker.ui.automation_install_dialog import _locate_chrome_exe


@pytest.mark.skipif(sys.platform.startswith("win"), reason="dev hosts only")
def test_locator_returns_path_object_or_none():
    """On Linux/macOS we mostly fall through to shutil.which, which
    returns None on most dev hosts. The contract is that the function
    never raises and returns Path or None."""
    result = _locate_chrome_exe()
    assert result is None or isinstance(result, Path)


def test_locator_handles_missing_winreg_gracefully():
    """If winreg import fails (non-Windows / sandboxed Windows) the
    locator falls back to PATH lookup without exploding."""
    with patch.dict(sys.modules, {"winreg": None}):
        # PATH lookup may or may not find chrome; the point is no
        # exception bubbles up.
        result = _locate_chrome_exe()
        assert result is None or isinstance(result, Path)


def test_locator_uses_path_when_available(tmp_path: Path, monkeypatch):
    """If shutil.which finds a chrome binary, return it."""
    import shutil

    fake_chrome = tmp_path / "chrome"
    fake_chrome.write_text("")
    fake_chrome.chmod(0o755)

    def fake_which(name: str) -> str | None:
        if name == "chrome":
            return str(fake_chrome)
        return None

    monkeypatch.setattr(shutil, "which", fake_which)
    # Patch the Windows-only branch off so we always reach the
    # which() path on this Linux test host.
    monkeypatch.setattr(sys, "platform", "linux")
    result = _locate_chrome_exe()
    assert result == fake_chrome
