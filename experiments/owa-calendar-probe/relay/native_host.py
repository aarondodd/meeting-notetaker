"""Chrome-side stdio<->TCP bridge for the OWA probe.

Chrome spawns this process every time the probe extension calls
``chrome.runtime.connectNative('com.meeting_notetaker.probe')``.

Behavior mirrors the production app's native_host.py but talks to the
probe's bridge.json (located under experiments/owa-calendar-probe/data/)
and the probe's port + token. Vendored rather than imported so the
experiment has no runtime dependency on the production package.

Invoke as: python -m relay.native_host

Stdin/stdout MUST be binary on Windows or the length prefix gets
mangled by CRLF translation.
"""
from __future__ import annotations

import json
import socket
import sys
import threading
from pathlib import Path

# Allow `python <path>/native_host.py` to find sibling modules even
# without -m. Chrome's manifest points at a wrapper script that calls
# this file directly, so package import isn't guaranteed.
_THIS = Path(__file__).resolve()
if str(_THIS.parent.parent) not in sys.path:
    sys.path.insert(0, str(_THIS.parent.parent))

from relay import paths  # noqa: E402
from relay.protocol import ProtocolError, read_message, write_message  # noqa: E402


HOST_VERSION = "1"
HANDSHAKE_ACK_TIMEOUT_SEC = 5.0


def main() -> int:
    _setup_stdio_binary()
    handshake_file = paths.handshake_path()

    try:
        info = _read_handshake_file(handshake_file)
    except FileNotFoundError:
        _write_protocol_error("relay not running (bridge.json missing)")
        return 2
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        _write_protocol_error(f"bridge.json unreadable: {exc}")
        return 2

    try:
        tcp = socket.create_connection(("127.0.0.1", info["port"]), timeout=5.0)
    except OSError as exc:
        _write_protocol_error(
            f"cannot reach relay on port {info['port']}: {exc}"
        )
        return 3

    tcp.settimeout(None)
    tcp_stream = tcp.makefile("rwb")

    handshake = {
        "type": "handshake_request",
        "token": info["token"],
        "host_version": HOST_VERSION,
    }
    try:
        write_message(tcp_stream, handshake)
    except OSError as exc:
        _write_protocol_error(f"handshake write failed: {exc}")
        return 4

    tcp.settimeout(HANDSHAKE_ACK_TIMEOUT_SEC)
    try:
        ack = read_message(tcp_stream)
    except (OSError, ProtocolError) as exc:
        _write_protocol_error(f"handshake ack read failed: {exc}")
        return 4
    tcp.settimeout(None)

    if not ack or ack.get("type") != "handshake_ack" or not ack.get("accepted"):
        detail = (ack or {}).get("detail", "rejected") if isinstance(ack, dict) else "rejected"
        _write_protocol_error(f"relay rejected handshake: {detail}")
        return 5

    _write_to_extension({
        "type": "bridge_ready",
        "app_version": ack.get("app_version", ""),
    })

    done = threading.Event()
    t1 = threading.Thread(
        target=_pump_extension_to_app, args=(tcp_stream, done),
        name="probe-nh-ext-to-app", daemon=True,
    )
    t2 = threading.Thread(
        target=_pump_app_to_extension, args=(tcp_stream, done),
        name="probe-nh-app-to-ext", daemon=True,
    )
    t1.start()
    t2.start()
    done.wait()
    try:
        tcp.close()
    except OSError:
        pass
    return 0


def _setup_stdio_binary() -> None:
    if hasattr(sys.stdin, "buffer"):
        sys.stdin = sys.stdin.buffer  # type: ignore[assignment]
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = sys.stdout.buffer  # type: ignore[assignment]
    if sys.platform.startswith("win"):
        try:
            import msvcrt
            import os
            msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
            msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
        except (ImportError, OSError):
            pass


def _write_to_extension(payload: dict) -> None:
    try:
        write_message(sys.stdout, payload)
    except OSError:
        pass


def _write_protocol_error(detail: str) -> None:
    _write_to_extension({
        "type": "error",
        "code": "bridge_unavailable",
        "detail": detail,
    })


def _read_handshake_file(path: Path) -> dict:
    with path.open("rb") as f:
        data = json.load(f)
    if "port" not in data or "token" not in data:
        raise KeyError("port + token required in bridge.json")
    return data


def _pump_extension_to_app(tcp_stream, done: threading.Event) -> None:
    try:
        while not done.is_set():
            msg = read_message(sys.stdin)
            if msg is None:
                break
            try:
                write_message(tcp_stream, msg)
            except OSError:
                break
    except (ProtocolError, OSError):
        pass
    finally:
        done.set()


def _pump_app_to_extension(tcp_stream, done: threading.Event) -> None:
    try:
        while not done.is_set():
            msg = read_message(tcp_stream)
            if msg is None:
                break
            _write_to_extension(msg)
    except (ProtocolError, OSError):
        pass
    finally:
        done.set()


if __name__ == "__main__":
    sys.exit(main())
