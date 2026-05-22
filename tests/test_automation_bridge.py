"""End-to-end exercise of the TCP loopback hop.

The bridge runs an actual socket server on 127.0.0.1; the tests open
a real socket against it, send framed messages, and assert the
callback fires. This is the most plumbing-heavy code in v0.6.3 -- the
test suite catches handshake regressions before they make it into a
Chrome roundtrip where they're a 30-second cycle to debug.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest

from meeting_notetaker.automation import messages
from meeting_notetaker.automation.bridge import Bridge, HandshakeState
from meeting_notetaker.automation.protocol import (
    encode_message,
    read_message,
    write_message,
)


@pytest.fixture
def handshake_file(tmp_path: Path) -> Path:
    return tmp_path / "bridge.json"


def _connect_and_handshake(
    bridge: Bridge, *, token: str | None = None, extension_id: str = "x"
) -> tuple[socket.socket, "io.BufferedRWPair"]:
    """Open a socket against the bridge's listening port, send a
    handshake, return (sock, stream) on accept. ``token=None`` means
    use the bridge's real token; pass a string to force a mismatch."""
    sock = socket.create_connection(("127.0.0.1", bridge.port), timeout=2.0)
    stream = sock.makefile("rwb")
    payload = messages.HandshakeRequest(
        token=token if token is not None else bridge.token,
        host_version="test",
        extension_id=extension_id,
    ).to_json()
    write_message(stream, payload)  # type: ignore[arg-type]
    return sock, stream


def _wait(predicate, timeout: float = 2.0, interval: float = 0.01) -> bool:
    """Poll for an event with a deadline. Threaded socket plumbing
    can't be observed synchronously; this keeps tests deterministic
    without arbitrary time.sleep() padding."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_start_writes_handshake_file(handshake_file: Path):
    bridge = Bridge(handshake_file, on_message=lambda _m: None)
    bridge.start()
    try:
        assert handshake_file.exists()
        data = json.loads(handshake_file.read_text())
        assert data["port"] == bridge.port
        assert data["token"] == bridge.token
        assert "pid" in data
    finally:
        bridge.stop()
    assert not handshake_file.exists(), "stop() should remove the handshake file"


def test_handshake_accepted_with_correct_token(handshake_file: Path):
    received: list[HandshakeState] = []
    bridge = Bridge(
        handshake_file,
        on_message=lambda _m: None,
        on_connect=received.append,
    )
    bridge.start()
    try:
        _sock, stream = _connect_and_handshake(bridge)
        ack = read_message(stream)
        assert ack["type"] == "handshake_ack"
        assert ack["accepted"] is True
        assert _wait(lambda: bridge.is_connected)
        assert len(received) == 1
        assert received[0].accepted is True
        assert received[0].extension_id == "x"
    finally:
        bridge.stop()


def test_handshake_rejected_with_bad_token(handshake_file: Path):
    bridge = Bridge(handshake_file, on_message=lambda _m: None)
    bridge.start()
    try:
        sock, stream = _connect_and_handshake(bridge, token="wrong-token")
        ack = read_message(stream)
        assert ack["type"] == "handshake_ack"
        assert ack["accepted"] is False
        assert "invalid token" in ack["detail"]
        # Socket gets closed on rejection; further reads return None
        # immediately.
        assert read_message(stream) is None
        sock.close()
        assert not bridge.is_connected
    finally:
        bridge.stop()


def test_inbound_messages_invoke_callback(handshake_file: Path):
    received: list[dict] = []
    bridge = Bridge(handshake_file, on_message=received.append)
    bridge.start()
    try:
        _sock, stream = _connect_and_handshake(bridge)
        read_message(stream)  # consume ack
        assert _wait(lambda: bridge.is_connected)
        write_message(stream, {"type": "status", "event": "pasting"})  # type: ignore[arg-type]
        write_message(stream, {"type": "status", "event": "done"})  # type: ignore[arg-type]
        assert _wait(lambda: len(received) == 2)
        assert received[0]["event"] == "pasting"
        assert received[1]["event"] == "done"
    finally:
        bridge.stop()


def test_outbound_send_reaches_peer(handshake_file: Path):
    bridge = Bridge(handshake_file, on_message=lambda _m: None)
    bridge.start()
    try:
        _sock, stream = _connect_and_handshake(bridge)
        read_message(stream)  # consume ack
        assert _wait(lambda: bridge.is_connected)
        req = messages.SynthesizeRequest(
            request_id="r1", target="claude", prompt="hello"
        )
        assert bridge.send(req) is True
        msg = read_message(stream)
        assert msg["type"] == "synthesize"
        assert msg["request_id"] == "r1"
        assert msg["prompt"] == "hello"
    finally:
        bridge.stop()


def test_send_without_peer_returns_false(handshake_file: Path):
    bridge = Bridge(handshake_file, on_message=lambda _m: None)
    bridge.start()
    try:
        assert bridge.send({"type": "ping"}) is False
    finally:
        bridge.stop()


def test_second_concurrent_peer_is_rejected(handshake_file: Path):
    bridge = Bridge(handshake_file, on_message=lambda _m: None)
    bridge.start()
    try:
        sock1, stream1 = _connect_and_handshake(bridge)
        read_message(stream1)  # ack
        assert _wait(lambda: bridge.is_connected)
        # Second socket gets accepted at the TCP layer then immediately
        # closed by the bridge. A read returns None (clean EOF).
        sock2 = socket.create_connection(("127.0.0.1", bridge.port), timeout=2.0)
        stream2 = sock2.makefile("rwb")
        assert read_message(stream2) is None
        sock2.close()
        # Original peer still works.
        bridge.send({"type": "ping"})
        relay = read_message(stream1)
        assert relay == {"type": "ping"}
    finally:
        bridge.stop()


def test_disconnect_callback_fires_when_peer_closes(handshake_file: Path):
    disconnects: list[None] = []
    bridge = Bridge(
        handshake_file,
        on_message=lambda _m: None,
        on_disconnect=lambda: disconnects.append(None),
    )
    bridge.start()
    try:
        sock, stream = _connect_and_handshake(bridge)
        read_message(stream)  # ack
        assert _wait(lambda: bridge.is_connected)
        # Both the socket and its makefile keep refcounts on the underlying
        # fd. shutdown() forces the FIN regardless of which references
        # are still open.
        sock.shutdown(socket.SHUT_RDWR)
        sock.close()
        assert _wait(lambda: len(disconnects) == 1)
        assert bridge.is_connected is False
    finally:
        bridge.stop()


def test_stop_is_idempotent(handshake_file: Path):
    bridge = Bridge(handshake_file, on_message=lambda _m: None)
    bridge.start()
    bridge.stop()
    bridge.stop()  # must not raise


def test_token_rotates_per_start(handshake_file: Path):
    bridge1 = Bridge(handshake_file, on_message=lambda _m: None)
    bridge1.start()
    token1 = bridge1.token
    bridge1.stop()

    bridge2 = Bridge(handshake_file, on_message=lambda _m: None)
    bridge2.start()
    try:
        assert bridge2.token != token1, (
            "fresh bridge must get a new token so a stale handshake "
            "file from a crashed prior run can't authenticate."
        )
    finally:
        bridge2.stop()
