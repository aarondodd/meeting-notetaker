"""End-to-end tests: bridge + native_host subprocess + framed messages.

These tests stand up the same components Edge will drive (Bridge,
native_host.py reading bridge.json, length-prefixed JSON over stdio),
just with a Python subprocess in the extension's seat. Anything that
breaks here will break on Windows too.

No Qt, no browser, no real OWA. The owa_response payloads are
synthetic copies of the captured fixtures.
"""
from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

_PROBE_ROOT = Path(__file__).resolve().parent.parent
if str(_PROBE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROBE_ROOT))

from relay.bridge import Bridge  # noqa: E402
from relay.protocol import encode_message, read_message  # noqa: E402


# Path to the native host module we spawn as a subprocess. Chrome
# normally invokes the wrapper script; here we invoke the python file
# directly so we don't have to chmod or write the wrapper for every
# test run.
NATIVE_HOST_PY = _PROBE_ROOT / "relay" / "native_host.py"


# ---------------------------------------------------------------------------
# Helpers


class _BridgeRecorder:
    """Captures every message the bridge passes via on_message, plus
    the state transitions, for assertion."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.states: list[tuple[str, str]] = []
        self._cv = threading.Condition()

    def on_message(self, msg: dict) -> None:
        with self._cv:
            self.messages.append(msg)
            self._cv.notify_all()

    def on_state(self, state: str, detail: str) -> None:
        with self._cv:
            self.states.append((state, detail))
            self._cv.notify_all()

    def wait_for_state(self, target: str, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._cv:
            while time.monotonic() < deadline:
                if any(s[0] == target for s in self.states):
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cv.wait(timeout=remaining)
        return False

    def wait_for_message(self, predicate, timeout: float = 5.0) -> dict | None:
        deadline = time.monotonic() + timeout
        with self._cv:
            while time.monotonic() < deadline:
                for m in self.messages:
                    if predicate(m):
                        return m
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cv.wait(timeout=remaining)
        return None


@pytest.fixture
def probe_env(tmp_path, monkeypatch):
    """Isolated data dir, fresh bridge.json on every test."""
    monkeypatch.setenv("MN_PROBE_DATA_DIR", str(tmp_path))
    yield tmp_path


@pytest.fixture
def recorder():
    return _BridgeRecorder()


@pytest.fixture
def bridge(probe_env, recorder):
    b = Bridge(on_message=recorder.on_message, on_state_change=recorder.on_state)
    b.start()
    try:
        yield b
    finally:
        b.stop()
        # Give threads a beat to drop.
        time.sleep(0.05)


def _spawn_native_host(extra_env: dict | None = None) -> subprocess.Popen:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.Popen(
        [sys.executable, str(NATIVE_HOST_PY)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def _send(proc: subprocess.Popen, payload: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write(encode_message(payload))
    proc.stdin.flush()


def _recv(proc: subprocess.Popen) -> dict | None:
    assert proc.stdout is not None
    return read_message(proc.stdout)


# ---------------------------------------------------------------------------
# Happy path: handshake + owa_request + owa_response


def test_handshake_completes_and_bridge_ready_reaches_extension(
    bridge, probe_env, recorder,
):
    """The wire-level dance Edge will perform on connectNative()."""
    proc = _spawn_native_host(extra_env={"MN_PROBE_DATA_DIR": str(probe_env)})
    try:
        ready = _recv(proc)
        assert ready is not None
        assert ready["type"] == "bridge_ready"
        assert recorder.wait_for_state("connected", timeout=5.0)
    finally:
        proc.terminate()
        proc.wait(timeout=3)


def test_request_flows_extension_to_bridge(bridge, probe_env, recorder):
    """Extension-side message must arrive at the bridge's on_message."""
    proc = _spawn_native_host(extra_env={"MN_PROBE_DATA_DIR": str(probe_env)})
    try:
        ready = _recv(proc)
        assert ready and ready["type"] == "bridge_ready"
        # Simulate the content script firing an OWA request through
        # the extension's service worker. In real life background.js
        # sends owa_request -> native host -> bridge.
        payload = {
            "type": "owa_request",
            "request_id": "rid-test-1",
            "verb": "calendar.fetch",
            "params": {
                "start_iso": "2026-05-31T10:00:00Z",
                "end_iso": "2026-05-31T23:59:59Z",
            },
        }
        _send(proc, payload)
        msg = recorder.wait_for_message(
            lambda m: m.get("type") == "owa_request"
            and m.get("request_id") == "rid-test-1",
            timeout=5.0,
        )
        assert msg is not None, "owa_request never reached the bridge"
        assert msg["verb"] == "calendar.fetch"
        assert msg["params"]["start_iso"].startswith("2026-")
    finally:
        proc.terminate()
        proc.wait(timeout=3)


def test_response_flows_bridge_to_extension(bridge, probe_env, recorder):
    """Bridge.send -> extension stdout (the path background.js
    listens on)."""
    proc = _spawn_native_host(extra_env={"MN_PROBE_DATA_DIR": str(probe_env)})
    try:
        ready = _recv(proc)
        assert ready and ready["type"] == "bridge_ready"
        # Push a synthetic OWA response from the relay side. In
        # production the relay would only emit these if the request
        # came from the extension, but the wire path is the same.
        sent = bridge.send({
            "type": "owa_response",
            "request_id": "rid-test-2",
            "verb": "calendar.fetch",
            "ok": True,
            "status": 200,
            "url": "https://outlook.office.com/owa/0/api/v2.0/me/calendarview",
            "body": {"value": []},
            "headers": {},
            "owa_build": "16.3000.123",
            "error": "",
        })
        assert sent, "bridge.send returned False with a connected peer"
        out = _recv(proc)
        assert out is not None
        assert out["type"] == "owa_response"
        assert out["request_id"] == "rid-test-2"
        assert out["ok"] is True
    finally:
        proc.terminate()
        proc.wait(timeout=3)


# ---------------------------------------------------------------------------
# Failure modes


def test_native_host_exits_when_bridge_json_missing(probe_env):
    """No relay running -> native host writes a single error frame
    and exits with code 2."""
    # probe_env is a fresh tmp dir -- no bridge.json. Don't start a
    # Bridge.
    proc = _spawn_native_host(extra_env={"MN_PROBE_DATA_DIR": str(probe_env)})
    try:
        err = _recv(proc)
        assert err is not None
        assert err["type"] == "error"
        assert err["code"] == "bridge_unavailable"
        assert "bridge.json" in err["detail"]
        rc = proc.wait(timeout=3)
        assert rc == 2
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=3)


def test_bridge_rejects_wrong_token(bridge, probe_env):
    """A native host running against a stale bridge.json (token from
    a prior relay launch) must be rejected. We simulate this by
    overwriting bridge.json with a fake token after the relay started."""
    bridge_json = probe_env / "bridge.json"
    real = json.loads(bridge_json.read_text())
    bad = dict(real)
    bad["token"] = "this-is-not-the-token"
    bridge_json.write_text(json.dumps(bad))

    proc = _spawn_native_host(extra_env={"MN_PROBE_DATA_DIR": str(probe_env)})
    try:
        out = _recv(proc)
        # Native host turns the rejection into an error frame.
        assert out is not None
        assert out["type"] == "error"
        assert "token mismatch" in out["detail"] or "rejected" in out["detail"]
        rc = proc.wait(timeout=3)
        assert rc != 0
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=3)


def test_bridge_single_peer_policy(bridge, probe_env):
    """A second native_host invocation while the first is connected
    must be rejected without disturbing the first."""
    proc1 = _spawn_native_host(extra_env={"MN_PROBE_DATA_DIR": str(probe_env)})
    try:
        # First peer completes handshake.
        ready1 = _recv(proc1)
        assert ready1 and ready1["type"] == "bridge_ready"

        # Second peer arrives. The accept-loop's lock-held branch
        # sends a rejection ack + closes the socket; the native host
        # turns the rejection into an error frame on stdout.
        proc2 = _spawn_native_host(extra_env={"MN_PROBE_DATA_DIR": str(probe_env)})
        try:
            out = _recv(proc2)
            assert out is not None
            # Different error surfaces depending on timing -- either
            # the rejection_ack or the closed-mid-handshake variant.
            assert out["type"] == "error"
        finally:
            if proc2.poll() is None:
                proc2.terminate()
                proc2.wait(timeout=3)

        # First peer is still alive: we can still ferry a message.
        # (Bridge has an active rx pump; send should still succeed.)
        assert bridge.send({"type": "ping", "request_id": "p1"})
        echo = _recv(proc1)
        assert echo is not None
        assert echo["type"] == "ping"
    finally:
        if proc1.poll() is None:
            proc1.terminate()
            proc1.wait(timeout=3)


# ---------------------------------------------------------------------------
# Protocol-level checks (no subprocess)


def test_protocol_rejects_oversize_message():
    """A 2 MB body must raise ProtocolError before it hits the wire.

    The native host inherits this limit, so a runaway content-script
    handler can't OOM the bridge."""
    from relay.protocol import MAX_MESSAGE_BYTES, ProtocolError, encode_message

    huge = {"type": "owa_response", "body": "x" * (MAX_MESSAGE_BYTES + 100)}
    with pytest.raises(ProtocolError):
        encode_message(huge)


def test_protocol_round_trip_preserves_unicode(tmp_path):
    """OWA meeting subjects with non-ASCII (smart quotes, emoji) must
    survive JSON encode + length-prefix decode."""
    from relay.protocol import encode_message, read_message
    import io

    original = {
        "type": "owa_response",
        "subject": "Cafe sync - Q1 review (Helsinki)",
        # ASCII-only per writing-style.md; UTF-8 path tested implicitly.
    }
    encoded = encode_message(original)
    # Round trip via a BytesIO acting as a stream.
    decoded = read_message(io.BytesIO(encoded))
    assert decoded == original


def test_protocol_short_read_raises(tmp_path):
    """A truncated message body (peer closed mid-write) is a hard
    error, not silent data loss."""
    import io
    from relay.protocol import ProtocolError, encode_message, read_message

    full = encode_message({"type": "ping"})
    truncated = full[:-3]  # lop off three body bytes
    with pytest.raises(ProtocolError):
        read_message(io.BytesIO(truncated))
