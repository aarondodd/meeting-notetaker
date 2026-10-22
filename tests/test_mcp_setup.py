"""Settings > Synthesis via MCP helpers.

These are pure-Python so they can be tested without Qt. The frozen
code path depends on ``sys.frozen``; the test monkeypatches it so both
shapes are exercised.
"""
from __future__ import annotations

import json
import sys

import pytest

from meeting_notetaker.utils import mcp_setup


def test_launcher_argv_dev_shape(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
    argv = mcp_setup.mcp_launcher_argv()
    assert argv[0] == "/usr/bin/python3"
    assert argv[-1] == "--mcp-server"
    assert argv[1].endswith("main.py")


def test_launcher_argv_frozen_shape(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "C:/Program Files/MeetingNotetaker/meeting-notetaker.exe")
    argv = mcp_setup.mcp_launcher_argv()
    assert argv == [
        "C:/Program Files/MeetingNotetaker/meeting-notetaker.exe",
        "--mcp-server",
    ]


def test_launcher_command_is_shell_quoted(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/opt/my app/meeting.exe")
    cmd = mcp_setup.mcp_launcher_command()
    # The space in the path must be quoted so a dumb shell paste works.
    assert "my app" in cmd
    # shlex.quote wraps in single quotes on posix shells.
    assert "'" in cmd or '"' in cmd


def test_client_config_snippet_is_valid_json(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/opt/mn.exe")
    snippet = mcp_setup.mcp_client_config_snippet()
    data = json.loads(snippet)
    assert "mcpServers" in data
    server = data["mcpServers"][mcp_setup.MCP_SERVER_KEY]
    assert server["command"] == "/opt/mn.exe"
    assert server["args"] == ["--mcp-server"]
