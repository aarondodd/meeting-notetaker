"""Entry-point dispatch tests for main.py.

Covers only the pure-Python surface: the --web-ui path now routes
through the GUI entry (with the FORCE_WEB_UI_THIS_RUN sentinel set and
the flag stripped from argv) rather than launching a separate HTTP
server exe. The GUI itself is stubbed so these tests do not need Qt or
a display.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_main_module():
    """Load main.py as a module without going through runpy's __main__
    path (which triggers the actual dispatch at import).
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "meeting_notetaker_main_under_test", REPO_ROOT / "main.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_run_web_ui_sets_force_flag_and_calls_gui(monkeypatch):
    """`_run_web_ui` sets FORCE_WEB_UI_THIS_RUN then invokes the GUI."""
    main_mod = _load_main_module()

    stub = types.ModuleType("meeting_notetaker.app")
    stub.FORCE_WEB_UI_THIS_RUN = False
    calls: list[bool] = []

    def fake_main() -> int:
        calls.append(stub.FORCE_WEB_UI_THIS_RUN)
        return 0

    stub.main = fake_main
    monkeypatch.setitem(sys.modules, "meeting_notetaker.app", stub)

    assert main_mod._run_web_ui() == 0
    assert calls == [True], "GUI entry must see FORCE_WEB_UI_THIS_RUN set"


def test_main_strips_web_ui_from_argv_before_dispatch(monkeypatch):
    """Qt chokes on unknown dash flags; `--web-ui` must be gone from argv."""
    import runpy

    recorded_argv: list[list[str]] = []

    stub = types.ModuleType("meeting_notetaker.app")
    stub.FORCE_WEB_UI_THIS_RUN = False

    def fake_main() -> int:
        recorded_argv.append(list(sys.argv))
        return 0

    stub.main = fake_main
    monkeypatch.setitem(sys.modules, "meeting_notetaker.app", stub)

    monkeypatch.setattr(sys, "argv", ["main.py", "--web-ui", "--port", "9999"])
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(str(REPO_ROOT / "main.py"), run_name="__main__")
    assert excinfo.value.code == 0
    assert recorded_argv, "GUI entry was not reached"
    assert "--web-ui" not in recorded_argv[0]
    # Other args stay so a user asking for a specific port can still
    # pass it through once we wire overrides into the GUI path.
    assert "--port" in recorded_argv[0]
    assert "9999" in recorded_argv[0]
