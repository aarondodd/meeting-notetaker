"""Chrome process detection + launch helpers.

is_chrome_running is the canonical "is the LLM intermediary alive?"
check the synthesis automation uses to decide whether to launch
Chrome on Send, gate the Send button, and pause the keep-alive ping
loop. Mocking psutil keeps these tests deterministic.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from meeting_notetaker.utils.chrome_process import (
    _CHROME_PROCESS_NAMES,
    is_chrome_running,
    launch_chrome,
    locate_chrome_exe,
)


psutil = pytest.importorskip(
    "psutil",
    reason="psutil is Windows-only in requirements; tests run on dev "
    "boxes without it (the runtime path returns False in that case "
    "which is the safe fallback).",
)


def _fake_proc(name: str) -> MagicMock:
    p = MagicMock()
    p.info = {"name": name}
    return p


def test_is_chrome_running_true_when_psutil_finds_chrome():
    with patch("psutil.process_iter") as iter_mock:
        iter_mock.return_value = iter([
            _fake_proc("explorer.exe"),
            _fake_proc("chrome.exe"),
            _fake_proc("notepad.exe"),
        ])
        assert is_chrome_running() is True


def test_is_chrome_running_false_when_no_chrome_process():
    with patch("psutil.process_iter") as iter_mock:
        iter_mock.return_value = iter([
            _fake_proc("explorer.exe"),
            _fake_proc("notepad.exe"),
        ])
        assert is_chrome_running() is False


def test_is_chrome_running_handles_psutil_error():
    """If psutil throws (sandboxed env, permission denied, etc) we
    treat the answer as "not running" so the launch path triggers
    on Send. Launching when Chrome is already up just opens a new
    tab, so a false negative is safe."""
    with patch("psutil.process_iter", side_effect=psutil.Error("boom")):
        assert is_chrome_running() is False


def test_is_chrome_running_skips_dead_processes():
    """psutil can throw NoSuchProcess for processes that exit
    mid-iteration -- we skip them rather than aborting the scan."""
    dead_proc = MagicMock()
    type(dead_proc).info = property(
        lambda self: (_ for _ in ()).throw(psutil.NoSuchProcess(pid=999))
    )
    chrome_proc = _fake_proc("chrome.exe")
    with patch("psutil.process_iter") as iter_mock:
        iter_mock.return_value = iter([dead_proc, chrome_proc])
        assert is_chrome_running() is True


def test_is_chrome_running_no_psutil_returns_false(monkeypatch):
    """If psutil is unavailable in the running env (it's Windows-only
    in our requirements), the function returns False rather than
    raising. The caller treats False as "launch Chrome on Send",
    which is the safe path even if Chrome is actually up."""
    import meeting_notetaker.utils.chrome_process as cp

    # Temporarily make `import psutil` fail inside the function.
    monkeypatch.setitem(sys.modules, "psutil", None)
    assert cp.is_chrome_running() is False


def test_chrome_process_names_constant_is_complete():
    """If someone ever adds e.g. "msedge.exe" to the constant by
    accident, the synthesis automation would mis-trigger on Edge.
    Pin the expected names so the constant can't drift silently."""
    assert _CHROME_PROCESS_NAMES == frozenset({
        "chrome.exe",
        "chrome",
        "Google Chrome",
    })


def test_launch_chrome_returns_false_when_no_executable(monkeypatch):
    monkeypatch.setattr(
        "meeting_notetaker.utils.chrome_process.locate_chrome_exe",
        lambda: None,
    )
    assert launch_chrome("https://claude.ai/new") is False


def test_launch_chrome_invokes_subprocess(tmp_path, monkeypatch):
    fake_chrome = tmp_path / "chrome"
    fake_chrome.write_text("")
    monkeypatch.setattr(
        "meeting_notetaker.utils.chrome_process.locate_chrome_exe",
        lambda: fake_chrome,
    )
    calls: list[list[str]] = []

    def fake_popen(args, **kwargs):
        calls.append(args)
        # Return a stub Popen-shaped object; the launcher doesn't
        # interact with it beyond construction.
        return MagicMock()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    assert launch_chrome("https://claude.ai/new") is True
    assert calls == [[str(fake_chrome), "https://claude.ai/new"]]


def test_launch_chrome_without_url(tmp_path, monkeypatch):
    fake_chrome = tmp_path / "chrome"
    fake_chrome.write_text("")
    monkeypatch.setattr(
        "meeting_notetaker.utils.chrome_process.locate_chrome_exe",
        lambda: fake_chrome,
    )
    captured: list[list[str]] = []
    monkeypatch.setattr(
        "subprocess.Popen",
        lambda args, **kw: captured.append(args) or MagicMock(),
    )
    launch_chrome()
    assert captured == [[str(fake_chrome)]]


def test_launch_chrome_handles_oserror(tmp_path, monkeypatch):
    fake_chrome = tmp_path / "chrome"
    fake_chrome.write_text("")
    monkeypatch.setattr(
        "meeting_notetaker.utils.chrome_process.locate_chrome_exe",
        lambda: fake_chrome,
    )
    monkeypatch.setattr(
        "subprocess.Popen",
        lambda *a, **k: (_ for _ in ()).throw(OSError("permission denied")),
    )
    assert launch_chrome("https://claude.ai/new") is False


@pytest.mark.skipif(sys.platform.startswith("win"), reason="dev hosts only")
def test_locator_returns_path_or_none():
    """Smoke: contract is Path or None, never raises."""
    result = locate_chrome_exe()
    assert result is None or isinstance(result, Path)


# SynthesisConnectionState ------------------------------------------------

def test_state_derive_combinations():
    from meeting_notetaker.utils.chrome_process import SynthesisConnectionState as S

    # No Chrome -> NOT_RUNNING regardless of bridge state (which can't
    # be connected without Chrome anyway, but defensively).
    assert S.derive(chrome_running=False, bridge_connected=False) is S.NOT_RUNNING
    assert S.derive(chrome_running=False, bridge_connected=True) is S.NOT_RUNNING
    # Chrome + bridge connected -> RUNNING_CONNECTED.
    assert S.derive(chrome_running=True, bridge_connected=True) is S.RUNNING_CONNECTED
    # Chrome but no bridge peer -> RUNNING_DISCONNECTED.
    assert S.derive(chrome_running=True, bridge_connected=False) is S.RUNNING_DISCONNECTED


def test_state_send_button_gating():
    """Send is enabled in NOT_RUNNING (launches Chrome) and
    RUNNING_CONNECTED (normal flow); disabled only in
    RUNNING_DISCONNECTED (extension is broken)."""
    from meeting_notetaker.utils.chrome_process import SynthesisConnectionState as S

    assert S.NOT_RUNNING.send_button_enabled() is True
    assert S.RUNNING_CONNECTED.send_button_enabled() is True
    assert S.RUNNING_DISCONNECTED.send_button_enabled() is False


def test_state_status_labels_match_aarons_spec():
    """Aaron's exact strings: Chrome not running / Chrome running,
    connected / Chrome running, disconnected."""
    from meeting_notetaker.utils.chrome_process import SynthesisConnectionState as S

    assert "Chrome not running" in S.NOT_RUNNING.status_label()
    assert "Chrome running, connected" in S.RUNNING_CONNECTED.status_label()
    assert "Chrome running, disconnected" in S.RUNNING_DISCONNECTED.status_label()
