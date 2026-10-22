"""Helpers that compose the Settings > Synthesis via MCP page.

The two public callers (``settings_dialog.py`` and ``docs/mcp-setup.md``)
need the exact launcher invocation for whatever shape of install the
user has, plus a copy-pastable JSON snippet their MCP client speaks.
Both are derived from one source so a frozen build, a dev checkout,
and the docs page never disagree about the quoting rules.
"""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path


MCP_SERVER_KEY = "meeting-notetaker"


def mcp_launcher_argv() -> list[str]:
    """Argv the MCP client has to spawn to run the server.

    Frozen (pyinstaller --onedir) builds call the app's own ``.exe``
    with ``--mcp-server``; a source / portable checkout calls the
    Python interpreter plus ``main.py``.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--mcp-server"]
    main_py = Path(sys.argv[0]).resolve()
    if main_py.name != "main.py":
        main_py = Path(__file__).resolve().parent.parent.parent / "main.py"
    return [sys.executable, str(main_py), "--mcp-server"]


def mcp_launcher_command() -> str:
    """Shell-quoted single-line form of ``mcp_launcher_argv``.

    Suitable for pasting into a Settings read-only field so the user
    can confirm what the client will run before wiring up MCP.
    """
    argv = mcp_launcher_argv()
    return " ".join(shlex.quote(part) for part in argv)


def mcp_client_config_snippet() -> str:
    """Multi-line JSON snippet the user pastes into their MCP client.

    Shape matches Claude Desktop's ``claude_desktop_config.json``,
    which Claude Code and the Chrome Claude extension both consume
    with the same ``mcpServers`` schema. The one key is the server
    name (``meeting-notetaker``) and the command plus args the client
    spawns.
    """
    argv = mcp_launcher_argv()
    body = {
        "mcpServers": {
            MCP_SERVER_KEY: {
                "command": argv[0],
                "args": argv[1:],
            }
        }
    }
    return json.dumps(body, indent=2)
