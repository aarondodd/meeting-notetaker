"""App-side TCP server the Chrome native-messaging host connects to.

Why a TCP loopback hop and not just have the host be the app?
Native-messaging hosts are spawned by Chrome per ``connectNative()``
call -- they live as long as the extension keeps the connection open.
Our app is a long-running PyQt6 GUI that owns the synthesis state
(transcript, prompt, session_id). The native host therefore runs as a
tiny bridge process that ferries length-prefixed JSON between Chrome's
stdio and the app's loopback socket.

Threading model:
  * The TCP listener and the per-connection reader live in a worker
    thread owned by ``Bridge``.
  * Inbound messages dispatch through a user-supplied callback. The
    callback runs on the bridge thread; the caller is expected to
    re-marshal onto the Qt main thread (controller.py does this via
    QMetaObject.invokeMethod / pyqtSignal emission).
  * Only one peer at a time is supported. A second connection while
    one is live gets a closed socket.

Auth:
  * On every app start ``Bridge.start()`` writes ``bridge.json`` with
    ``{port, token, pid}``. The native host reads that file, opens a
    TCP socket, and sends ``handshake{token}`` as its first frame.
  * The token rotates per app launch (``secrets.token_hex(16)``); a
    stale ``bridge.json`` from a crashed prior run gets overwritten.
  * Mismatched token -> bridge closes the socket immediately.

The Bridge object is pure Python (no Qt imports) so the same code is
exercised by the test suite without a Qt runtime.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import messages
from .protocol import ProtocolError, encode_message, read_message, write_message


log = logging.getLogger(__name__)


# How long to wait for the handshake before closing a freshly accepted
# connection. The host should send the handshake immediately on
# connect; anything slower than this is almost certainly a port scan
# or an unrelated localhost client.
HANDSHAKE_TIMEOUT_SEC = 5.0


@dataclass
class HandshakeState:
    accepted: bool
    extension_id: str = ""
    host_version: str = ""
    detail: str = ""


class Bridge:
    """One-peer TCP loopback server for the native-messaging host.

    Lifecycle::

        bridge = Bridge(handshake_file=path, on_message=callback)
        bridge.start()        # binds port, writes handshake file
        # ... runtime: on_message fires for inbound frames
        bridge.send(payload)  # outbound to the connected host (if any)
        bridge.stop()         # closes everything, removes handshake file
    """

    def __init__(
        self,
        handshake_file: Path,
        on_message: Callable[[dict[str, Any]], None],
        *,
        on_connect: Callable[[HandshakeState], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
        app_version: str = "",
    ) -> None:
        self._handshake_file = handshake_file
        self._on_message = on_message
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._app_version = app_version

        self._token: str = ""
        self._port: int = 0
        self._server_sock: socket.socket | None = None
        self._peer_sock: socket.socket | None = None
        self._peer_lock = threading.Lock()
        self._accept_thread: threading.Thread | None = None
        self._reader_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._connected = threading.Event()

    # ------------------------------------------------------------------
    # Lifecycle

    def start(self) -> None:
        """Bind a loopback port, write the handshake file, spawn the
        accept thread. Idempotent: a second call while running is a
        no-op."""
        if self._server_sock is not None:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        self._server_sock = sock
        self._port = sock.getsockname()[1]
        self._token = secrets.token_hex(16)
        self._write_handshake_file()
        self._stop_event.clear()
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="bridge-accept", daemon=True
        )
        self._accept_thread.start()
        log.info("bridge listening on 127.0.0.1:%d", self._port)

    def stop(self) -> None:
        """Close all sockets, remove the handshake file, join threads.

        ``accept()`` doesn't unblock when the listening socket is just
        closed on most platforms, so we open a throwaway connection to
        ourselves to wake it. The accept loop checks ``_stop_event``
        before promoting the new conn and bails out without acking."""
        if self._server_sock is None:
            # Already stopped.
            return
        self._stop_event.set()
        with self._peer_lock:
            peer = self._peer_sock
            self._peer_sock = None
        if peer is not None:
            try:
                peer.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            peer.close()
        # Wake the accept thread.
        port = self._port
        try:
            wakeup = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            wakeup.close()
        except OSError:
            pass
        try:
            self._server_sock.close()
        except OSError:
            pass
        self._server_sock = None
        if self._accept_thread is not None and self._accept_thread.is_alive():
            self._accept_thread.join(timeout=2.0)
        if self._reader_thread is not None and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2.0)
        self._connected.clear()
        try:
            self._handshake_file.unlink()
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------------
    # Properties

    @property
    def port(self) -> int:
        return self._port

    @property
    def token(self) -> str:
        return self._token

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    # ------------------------------------------------------------------
    # Send

    def send(self, payload: Any) -> bool:
        """Push one message to the connected host. Returns False if no
        peer is connected. Caller is expected to surface "not connected"
        as a user-facing error (Send button disabled, etc.)."""
        msg = messages.encode(payload)
        with self._peer_lock:
            peer = self._peer_sock
        if peer is None:
            return False
        try:
            peer.sendall(encode_message(msg))
            return True
        except OSError as exc:
            log.warning("send failed, dropping peer: %s", exc)
            self._drop_peer()
            return False

    # ------------------------------------------------------------------
    # Accept / read loops

    def _accept_loop(self) -> None:
        assert self._server_sock is not None
        while not self._stop_event.is_set():
            try:
                conn, _addr = self._server_sock.accept()
            except OSError:
                # Server socket closed during stop().
                return
            if self._stop_event.is_set():
                conn.close()
                return
            # Reject if we already have a peer.
            with self._peer_lock:
                already_have_peer = self._peer_sock is not None
            if already_have_peer:
                log.warning("second native-host connection rejected")
                conn.close()
                continue
            self._handle_new_peer(conn)

    def _handle_new_peer(self, conn: socket.socket) -> None:
        conn.settimeout(HANDSHAKE_TIMEOUT_SEC)
        try:
            stream = conn.makefile("rwb")
            first = read_message(stream)  # type: ignore[arg-type]
        except (ProtocolError, OSError) as exc:
            log.warning("handshake read failed: %s", exc)
            conn.close()
            return
        if not first or first.get("type") != "handshake":
            log.warning("expected handshake, got %r", first)
            conn.close()
            return
        token = first.get("token", "")
        if not isinstance(token, str) or not _consteq(token, self._token):
            ack = messages.HandshakeAck(
                accepted=False,
                detail="invalid token",
                app_version=self._app_version,
            )
            try:
                write_message(stream, ack.to_json())  # type: ignore[arg-type]
            except OSError:
                pass
            conn.close()
            log.warning("handshake rejected: bad token")
            return
        # Accept.
        state = HandshakeState(
            accepted=True,
            extension_id=str(first.get("extension_id", "")),
            host_version=str(first.get("host_version", "")),
        )
        ack = messages.HandshakeAck(
            accepted=True,
            detail="ok",
            app_version=self._app_version,
        )
        try:
            write_message(stream, ack.to_json())  # type: ignore[arg-type]
        except OSError as exc:
            log.warning("handshake ack write failed: %s", exc)
            conn.close()
            return
        # Promote to peer + start reader.
        conn.settimeout(None)
        with self._peer_lock:
            self._peer_sock = conn
        self._connected.set()
        if self._on_connect is not None:
            try:
                self._on_connect(state)
            except Exception:  # noqa: BLE001 -- defensive; don't kill bridge
                log.exception("on_connect callback raised")
        self._reader_thread = threading.Thread(
            target=self._read_loop, args=(stream,), name="bridge-reader", daemon=True
        )
        self._reader_thread.start()

    def _read_loop(self, stream: Any) -> None:
        while not self._stop_event.is_set():
            try:
                msg = read_message(stream)
            except (ProtocolError, OSError) as exc:
                log.info("peer read ended: %s", exc)
                break
            if msg is None:
                log.info("peer closed cleanly")
                break
            try:
                self._on_message(msg)
            except Exception:  # noqa: BLE001 -- callback is user code
                log.exception("on_message callback raised")
        self._drop_peer()

    def _drop_peer(self) -> None:
        with self._peer_lock:
            peer = self._peer_sock
            self._peer_sock = None
        if peer is not None:
            try:
                peer.close()
            except OSError:
                pass
        if self._connected.is_set():
            self._connected.clear()
            if self._on_disconnect is not None:
                try:
                    self._on_disconnect()
                except Exception:  # noqa: BLE001 -- defensive
                    log.exception("on_disconnect callback raised")

    # ------------------------------------------------------------------
    # Handshake file

    def _write_handshake_file(self) -> None:
        payload = {
            "port": self._port,
            "token": self._token,
            "pid": os.getpid(),
        }
        # Atomic write so a host reading mid-rotate never sees a half
        # file. Tempfile in the same dir -> rename.
        tmp = self._handshake_file.with_suffix(self._handshake_file.suffix + ".tmp")
        self._handshake_file.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, self._handshake_file)


def _consteq(a: str, b: str) -> bool:
    """Constant-time string compare for the token check."""
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a.encode("utf-8"), b.encode("utf-8")):
        result |= x ^ y
    return result == 0
