"""End-to-end exercise of the native-host bridge process.

Spawns the host via ``python -m meeting_notetaker.automation.native_host_cli``
(a tiny CLI shim that exists only for tests; in production the host
runs as ``main.py --native-host``). Pumps Chrome-style length-prefixed
frames over its stdin/stdout while a real Bridge is listening on the
other side. Validates the full hop: extension -> host stdin -> TCP ->
bridge callback -> bridge send -> TCP -> host stdout -> extension.
"""
from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

from meeting_notetaker.automation.bridge import Bridge
from meeting_notetaker.automation.protocol import encode_message, read_message


HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent


def _spawn_host(handshake_file: Path, extension_id: str = "abcdef") -> subprocess.Popen:
    env = os.environ.copy()
    env["MEETING_NOTETAKER_DATA_DIR"] = str(handshake_file.parent.parent)
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "meeting_notetaker.automation.native_host_cli",
            "--handshake-file",
            str(handshake_file),
            "--extension-id",
            extension_id,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(PROJECT_ROOT),
        env=env,
    )


def _send_to_host(proc: subprocess.Popen, payload: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write(encode_message(payload))
    proc.stdin.flush()


def _recv_from_host(proc: subprocess.Popen, timeout: float = 2.0) -> dict | None:
    assert proc.stdout is not None
    # subprocess.Popen.stdout is a binary file with no per-call timeout;
    # for the deterministic stop-after-N-bytes tests we run, blocking
    # reads are fine because the host writes immediately.
    return read_message(proc.stdout)


def test_host_bridges_extension_to_app(tmp_path: Path):
    received_by_app: list[dict] = []
    bridge = Bridge(tmp_path / "bridge.json", on_message=received_by_app.append)
    bridge.start()
    host = _spawn_host(tmp_path / "bridge.json", extension_id="ext1")
    try:
        # Host writes "bridge_ready" once it gets handshake_ack.
        ready = _recv_from_host(host)
        assert ready is not None
        assert ready["type"] == "bridge_ready"

        # Extension -> host stdin -> bridge.
        _send_to_host(host, {"type": "synthesize", "request_id": "r1", "prompt": "hi"})
        deadline = time.time() + 2.0
        while time.time() < deadline and not received_by_app:
            time.sleep(0.01)
        assert len(received_by_app) == 1
        assert received_by_app[0]["request_id"] == "r1"
        assert received_by_app[0]["prompt"] == "hi"

        # Bridge -> TCP -> host stdout -> extension.
        bridge.send({"type": "status", "request_id": "r1", "event": "done"})
        echoed = _recv_from_host(host)
        assert echoed["type"] == "status"
        assert echoed["event"] == "done"
    finally:
        if host.poll() is None:
            host.terminate()
            host.wait(timeout=2.0)
        bridge.stop()


def test_host_reports_app_unreachable_when_no_bridge(tmp_path: Path):
    """No bridge.json -> host writes a bridge_unavailable error to
    stdout so the extension can show a useful message instead of
    silently hanging."""
    host = _spawn_host(tmp_path / "bridge.json")
    try:
        err = _recv_from_host(host)
        assert err["type"] == "error"
        assert err["code"] == "bridge_unavailable"
        assert "missing" in err["detail"].lower()
        host.wait(timeout=2.0)
        assert host.returncode != 0
    finally:
        if host.poll() is None:
            host.terminate()
            host.wait(timeout=2.0)


def test_host_rejects_when_token_is_stale(tmp_path: Path):
    """A bridge.json from a crashed prior app run has a token that
    the live bridge won't accept. Host must surface the rejection
    rather than retrying silently."""
    # Write a stale bridge.json pointing at a bound but wrong-token
    # bridge.
    bridge = Bridge(tmp_path / "bridge.json", on_message=lambda _m: None)
    bridge.start()
    real_path = tmp_path / "bridge.json"
    stale_data = json.loads(real_path.read_text())
    stale_data["token"] = "ffffffffffffffffffffffffffffffff"
    real_path.write_text(json.dumps(stale_data))

    host = _spawn_host(real_path)
    try:
        err = _recv_from_host(host)
        assert err["type"] == "error"
        assert err["code"] == "bridge_unavailable"
        host.wait(timeout=2.0)
        assert host.returncode != 0
    finally:
        if host.poll() is None:
            host.terminate()
            host.wait(timeout=2.0)
        bridge.stop()
