"""Loopback TCP server the relay app listens on.

One peer at a time. The native host reads bridge.json (host + port +
token), opens a TCP socket, sends a handshake_request, and from then
on the relay just passes framed messages back and forth.

Outgoing requests are pushed via ``send`` from the Qt thread (worker
threads pump RX). Incoming OWA responses fire an on_message callback
that the app routes back to the UI via a Qt signal.
"""
from __future__ import annotations

import json
import logging
import secrets
import socket
import threading
from pathlib import Path
from typing import Callable, Optional

from . import paths
from .protocol import ProtocolError, read_message, write_message


log = logging.getLogger(__name__)


APP_VERSION = "owa-probe-0.0.1"


class Bridge:
    """Loopback TCP server with a single-peer policy."""

    def __init__(
        self,
        *,
        on_message: Callable[[dict], None],
        on_state_change: Callable[[str, str], None],
    ) -> None:
        self._on_message = on_message
        self._on_state = on_state_change  # state, detail
        self._lock = threading.Lock()
        self._peer_sock: Optional[socket.socket] = None
        self._peer_stream = None
        self._listener: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._accept_thread: Optional[threading.Thread] = None
        self._rx_thread: Optional[threading.Thread] = None
        self._token = secrets.token_urlsafe(24)
        self._port = 0

    # ---- lifecycle ------------------------------------------------------

    def start(self) -> int:
        """Bind to a free loopback port, write bridge.json, start
        accept loop. Returns the bound port."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        self._listener = s
        self._port = s.getsockname()[1]
        self._write_handshake_file()
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="probe-bridge-accept", daemon=True
        )
        self._accept_thread.start()
        self._on_state("listening", f"port {self._port}")
        log.info("bridge listening on 127.0.0.1:%d", self._port)
        return self._port

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            if self._peer_sock:
                try:
                    self._peer_sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    self._peer_sock.close()
                except OSError:
                    pass
                self._peer_sock = None
                self._peer_stream = None
        if self._listener:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None
        # Best effort: scrub the handshake file so a stale extension
        # invocation after relay exit can't try to talk to us.
        try:
            paths.handshake_path().unlink(missing_ok=True)
        except OSError:
            pass

    # ---- outbound send --------------------------------------------------

    def send(self, payload: dict) -> bool:
        """Push one message to the connected peer. Returns False if
        no peer is connected -- caller surfaces that to the UI."""
        with self._lock:
            stream = self._peer_stream
        if stream is None:
            return False
        try:
            write_message(stream, payload)
            return True
        except (OSError, ProtocolError) as exc:
            log.warning("bridge send failed: %s", exc)
            self._drop_peer("send_failed")
            return False

    # ---- internals ------------------------------------------------------

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                conn, addr = self._listener.accept()
            except OSError:
                return
            log.info("bridge accept from %s:%d", *addr)
            with self._lock:
                if self._peer_sock is not None:
                    # Single-peer policy: refuse the new one.
                    try:
                        write_message(
                            conn.makefile("rwb"),
                            {
                                "type": "handshake_ack",
                                "accepted": False,
                                "detail": "another peer is connected",
                            },
                        )
                    except OSError:
                        pass
                    conn.close()
                    log.info("bridge rejected duplicate peer")
                    continue
                conn.settimeout(5.0)
                stream = conn.makefile("rwb")
                try:
                    msg = read_message(stream)
                except (OSError, ProtocolError) as exc:
                    log.warning("handshake read failed: %s", exc)
                    conn.close()
                    continue
                if not msg or msg.get("type") != "handshake_request":
                    write_message(stream, {
                        "type": "handshake_ack",
                        "accepted": False,
                        "detail": "first frame must be handshake_request",
                    })
                    conn.close()
                    continue
                if msg.get("token") != self._token:
                    write_message(stream, {
                        "type": "handshake_ack",
                        "accepted": False,
                        "detail": "token mismatch",
                    })
                    conn.close()
                    continue
                write_message(stream, {
                    "type": "handshake_ack",
                    "accepted": True,
                    "app_version": APP_VERSION,
                })
                conn.settimeout(None)
                self._peer_sock = conn
                self._peer_stream = stream
                self._on_state("connected", "")
                self._rx_thread = threading.Thread(
                    target=self._rx_loop, name="probe-bridge-rx", daemon=True
                )
                self._rx_thread.start()

    def _rx_loop(self) -> None:
        with self._lock:
            stream = self._peer_stream
        if stream is None:
            return
        try:
            while not self._stop.is_set():
                msg = read_message(stream)
                if msg is None:
                    break
                try:
                    self._on_message(msg)
                except Exception as exc:  # never let UI handler kill the pump
                    log.exception("on_message handler raised: %s", exc)
        except (OSError, ProtocolError) as exc:
            log.info("bridge rx ended: %s", exc)
        finally:
            self._drop_peer("rx_closed")

    def _drop_peer(self, detail: str) -> None:
        with self._lock:
            sock = self._peer_sock
            self._peer_sock = None
            self._peer_stream = None
        if sock:
            try:
                sock.close()
            except OSError:
                pass
        self._on_state("disconnected", detail)

    def _write_handshake_file(self) -> None:
        bridge_json = {
            "port": self._port,
            "token": self._token,
            "app_version": APP_VERSION,
            "host": "127.0.0.1",
        }
        path = paths.handshake_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(bridge_json, indent=2), encoding="utf-8")
