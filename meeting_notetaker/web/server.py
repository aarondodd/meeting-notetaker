"""HTTP server loop for the local web UI.

Binds 127.0.0.1 only. Picks the first free port starting at
``--port`` (default 8765) with 20 fallbacks before giving up, so a
second instance does not need the user to pick a number.
"""
from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import handlers


log = logging.getLogger(__name__)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PORT_FALLBACK_RANGE = 20


class _RequestHandler(BaseHTTPRequestHandler):
    """Translates http.server callbacks into handlers.dispatch calls."""

    server_version = "MeetingNotetaker/web"

    # Silence http.server's default stderr noise; we have our own logger.
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        log.info("%s - %s", self.address_string(), format % args)

    def _read_body(self) -> bytes:
        length_str = self.headers.get("Content-Length") or "0"
        try:
            length = int(length_str)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _dispatch(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

        headers = {k.lower(): v for k, v in self.headers.items()}

        raw_body = self._read_body() if self.command in ("POST", "PUT", "PATCH") else b""
        form: dict[str, list[str]] = {}
        json_body: object | None = None
        content_type = (headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if raw_body and content_type == "application/json":
            try:
                json_body = json.loads(raw_body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                json_body = None
        elif raw_body and content_type == "application/x-www-form-urlencoded":
            form = urllib.parse.parse_qs(raw_body.decode("utf-8"), keep_blank_values=True)

        request = handlers.Request(
            method=self.command,
            path=parsed.path,
            query=query,
            form=form,
            json_body=json_body,
            headers=headers,
            path_params={},
        )
        status, resp_headers, body = handlers.dispatch(request)
        self.send_response(status)
        for name, value in resp_headers.items():
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch()


def _port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _pick_port(host: str, start: int, fallback_range: int = PORT_FALLBACK_RANGE) -> int:
    for candidate in range(start, start + fallback_range + 1):
        if _port_is_free(host, candidate):
            return candidate
    raise RuntimeError(
        f"no free port in {start}..{start + fallback_range} on {host}"
    )


def make_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    allow_port_fallback: bool = True,
) -> tuple[ThreadingHTTPServer, int]:
    """Build (but do not start) the server. Returns (server, actual_port).

    ``allow_port_fallback`` is for tests: pass ``False`` and ``port=0`` to
    let the OS pick an ephemeral port and read it back off the socket.
    """
    if port == 0:
        server = ThreadingHTTPServer((host, 0), _RequestHandler)
        return server, server.server_address[1]
    if allow_port_fallback:
        port = _pick_port(host, port)
    server = ThreadingHTTPServer((host, port), _RequestHandler)
    return server, port


def serve(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    open_browser: bool = True,
) -> int:
    server, actual_port = make_server(host, port)
    url = f"http://{host}:{actual_port}/"
    log.info("meeting-notetaker web UI on %s", url)
    sys.stderr.write(f"meeting-notetaker web UI on {url}\n")
    sys.stderr.flush()
    if open_browser:
        try:
            webbrowser.open(url, new=1, autoraise=True)
        except Exception:  # pragma: no cover - best-effort
            log.warning("could not open browser; navigate to %s manually", url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


_BACKGROUND_LOCK = threading.Lock()
_BACKGROUND_URL: str | None = None
_BACKGROUND_SERVER: ThreadingHTTPServer | None = None


def start_background_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> str:
    """Start the web UI on a daemon thread exactly once per process.

    Returns the URL to open. Called from the GUI's Settings page so the
    user does not have to run a second command. A second call returns
    the already-running server's URL rather than binding a new port, so
    repeated clicks on the Settings button are idempotent.
    """
    global _BACKGROUND_URL, _BACKGROUND_SERVER
    with _BACKGROUND_LOCK:
        if _BACKGROUND_URL is not None:
            return _BACKGROUND_URL
        server, actual_port = make_server(host, port)
        url = f"http://{host}:{actual_port}/"
        thread = threading.Thread(
            target=server.serve_forever,
            name="meeting-notetaker-web",
            daemon=True,
        )
        thread.start()
        _BACKGROUND_URL = url
        _BACKGROUND_SERVER = server
        log.info("web UI started in background at %s", url)
        return url


def background_server_url() -> str | None:
    """Return the URL if the background server is up, else None."""
    return _BACKGROUND_URL


def _reset_background_server_for_tests() -> None:
    """Stop and clear the background server. Used by tests only."""
    global _BACKGROUND_URL, _BACKGROUND_SERVER
    with _BACKGROUND_LOCK:
        if _BACKGROUND_SERVER is not None:
            _BACKGROUND_SERVER.shutdown()
            _BACKGROUND_SERVER.server_close()
        _BACKGROUND_URL = None
        _BACKGROUND_SERVER = None


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="meeting-notetaker --web-ui")
    parser.add_argument("--host", default=DEFAULT_HOST, help="bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="preferred port")
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="do not open the default browser automatically",
    )
    parser.add_argument(
        "--mcp-server",
        action="store_true",
        help=argparse.SUPPRESS,  # swallowed up-stack so argparse does not choke
    )
    parser.add_argument(
        "--web-ui",
        action="store_true",
        help=argparse.SUPPRESS,  # consumed by main.py; keep it legal here
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    ns = _parse_args(list(sys.argv[1:] if argv is None else argv))
    return serve(host=ns.host, port=ns.port, open_browser=not ns.no_open)
