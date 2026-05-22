"""Native-messaging host: bridges Chrome stdio <-> the running app's
TCP loopback socket.

Chrome spawns this entry point every time the extension calls
``chrome.runtime.connectNative('com.meeting_notetaker.bridge')``. We
read ``bridge.json`` to find the app's loopback port + auth token,
open a TCP connection, send a handshake, and then ferry length-
prefixed JSON frames between stdin/stdout (the extension hop) and the
TCP socket (the app hop) until either side closes.

Invoke from the frozen .exe with ``--native-host``::

    main.py --native-host

The Chrome-side native-messaging manifest points at the .exe with that
arg so a separate bridge binary doesn't need to ship.
"""
from __future__ import annotations

import json
import logging
import socket
import sys
import threading
from pathlib import Path

from . import messages
from .protocol import ProtocolError, read_message, write_message


log = logging.getLogger(__name__)


HOST_VERSION = "1"

# Time to wait for the app's handshake_ack after sending our handshake.
HANDSHAKE_ACK_TIMEOUT_SEC = 5.0


def run(handshake_file: Path, *, extension_id: str = "") -> int:
    """Entry point invoked from ``main.py`` when ``--native-host`` is
    present in argv. Returns a process exit code."""
    _setup_stdio_binary()
    try:
        info = _read_handshake_file(handshake_file)
    except FileNotFoundError:
        _write_protocol_error("app not running (bridge.json missing)")
        return 2
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        _write_protocol_error(f"bridge.json unreadable: {exc}")
        return 2

    try:
        tcp = socket.create_connection(("127.0.0.1", info["port"]), timeout=5.0)
    except OSError as exc:
        _write_protocol_error(f"cannot reach app on port {info['port']}: {exc}")
        return 3

    tcp.settimeout(None)
    tcp_stream = tcp.makefile("rwb")

    handshake = messages.HandshakeRequest(
        token=info["token"],
        host_version=HOST_VERSION,
        extension_id=extension_id,
    )
    try:
        write_message(tcp_stream, handshake.to_json())  # type: ignore[arg-type]
    except OSError as exc:
        _write_protocol_error(f"handshake write failed: {exc}")
        return 4

    tcp.settimeout(HANDSHAKE_ACK_TIMEOUT_SEC)
    try:
        ack = read_message(tcp_stream)  # type: ignore[arg-type]
    except (OSError, ProtocolError) as exc:
        _write_protocol_error(f"handshake ack read failed: {exc}")
        return 4
    tcp.settimeout(None)

    if not ack or ack.get("type") != "handshake_ack" or not ack.get("accepted"):
        detail = (ack or {}).get("detail", "rejected") if isinstance(ack, dict) else "rejected"
        _write_protocol_error(f"app rejected handshake: {detail}")
        return 5

    # Tell the extension we're alive + which app version we're talking
    # to. The extension can choose to act on app_version (e.g. force a
    # reinstall if the app is older than the extension expects).
    _write_to_extension({
        "type": "bridge_ready",
        "app_version": ack.get("app_version", ""),
    })

    # Two pump threads: stdin (extension) -> TCP (app), TCP -> stdout.
    done = threading.Event()
    t1 = threading.Thread(
        target=_pump_extension_to_app,
        args=(tcp_stream, done),
        name="nh-ext-to-app",
        daemon=True,
    )
    t2 = threading.Thread(
        target=_pump_app_to_extension,
        args=(tcp_stream, done),
        name="nh-app-to-ext",
        daemon=True,
    )
    t1.start()
    t2.start()
    done.wait()
    try:
        tcp.close()
    except OSError:
        pass
    return 0


# ---------------------------------------------------------------------------
# stdio plumbing


def _setup_stdio_binary() -> None:
    """Ensure stdin/stdout are byte streams. On Windows, the default
    sys.stdin/stdout are text mode and would corrupt the length prefix
    by translating CRLF."""
    if hasattr(sys.stdin, "buffer"):
        sys.stdin = sys.stdin.buffer  # type: ignore[assignment]
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = sys.stdout.buffer  # type: ignore[assignment]
    if sys.platform.startswith("win"):
        # Belt-and-suspenders: even with .buffer access, set the
        # underlying file descriptors to binary mode.
        try:
            import msvcrt  # noqa: PLC0415
            import os  # noqa: PLC0415
            msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
            msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
        except (ImportError, OSError):
            pass


def _write_to_extension(payload: dict) -> None:
    try:
        write_message(sys.stdout, payload)  # type: ignore[arg-type]
    except OSError:
        pass


def _write_protocol_error(detail: str) -> None:
    _write_to_extension({
        "type": "error",
        "code": "bridge_unavailable",
        "detail": detail,
    })


# ---------------------------------------------------------------------------
# Handshake file


def _read_handshake_file(path: Path) -> dict:
    with path.open("rb") as f:
        data = json.load(f)
    if "port" not in data or "token" not in data:
        raise KeyError("port + token required in bridge.json")
    return data


# ---------------------------------------------------------------------------
# Pumps


def _pump_extension_to_app(tcp_stream, done: threading.Event) -> None:
    try:
        while not done.is_set():
            msg = read_message(sys.stdin)  # type: ignore[arg-type]
            if msg is None:
                break
            try:
                write_message(tcp_stream, msg)
            except OSError:
                break
    except (ProtocolError, OSError) as exc:
        log.info("ext->app pump ended: %s", exc)
    finally:
        done.set()


def _pump_app_to_extension(tcp_stream, done: threading.Event) -> None:
    try:
        while not done.is_set():
            msg = read_message(tcp_stream)
            if msg is None:
                break
            _write_to_extension(msg)
    except (ProtocolError, OSError) as exc:
        log.info("app->ext pump ended: %s", exc)
    finally:
        done.set()
