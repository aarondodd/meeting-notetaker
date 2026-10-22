"""Stdio MCP server loop.

Implements the subset of the Model Context Protocol that Chrome
Claude, Claude Desktop, and Claude Code actually call: ``initialize``,
``notifications/initialized``, ``tools/list``, ``tools/call``. One
JSON-RPC message per line on stdin/stdout, UTF-8. See
https://modelcontextprotocol.io/ for the wire format.

The loop is self-contained so a test can drive it with
``io.StringIO`` instead of real pipes.
"""
from __future__ import annotations

import json
import logging
import sys
import traceback
from typing import IO

from ..version import __version__
from .tools import TOOLS, tool_by_name


log = logging.getLogger(__name__)


PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "meeting-notetaker"


# JSON-RPC error codes defined by the spec.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _write(out: IO[str], message: dict) -> None:
    """Write one JSON message followed by a newline, flushing immediately.

    The MCP stdio transport frames messages by newline, so each payload
    must be a single line of JSON. json.dumps with the default
    ``separators`` already strips newlines inside strings via escaping.
    """
    out.write(json.dumps(message, separators=(",", ":")))
    out.write("\n")
    out.flush()


def _error(req_id, code: int, message: str, data=None) -> dict:
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _result(req_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _handle_initialize(_params: dict) -> dict:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {
            "name": SERVER_NAME,
            "version": __version__,
        },
    }


def _handle_tools_list(_params: dict) -> dict:
    return {
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.input_schema,
            }
            for t in TOOLS
        ]
    }


def _handle_tools_call(params: dict) -> dict:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    tool = tool_by_name(name)
    if tool is None:
        raise ValueError(f"unknown tool '{name}'")
    try:
        payload = tool.handler(arguments)
    except Exception as exc:
        log.exception("tool %s raised", name)
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"error: {exc}"}],
        }
    # MCP tools return a "content" array of blocks. We serialize the
    # tool's structured result as JSON text so clients can show it or
    # parse it. isError is set when the handler itself returned
    # {"error": ...}.
    is_error = isinstance(payload, dict) and "error" in payload
    text = json.dumps(payload, indent=2)
    return {
        "isError": is_error,
        "content": [{"type": "text", "text": text}],
    }


def dispatch(message: dict) -> dict | None:
    """Route a single parsed JSON-RPC message. Returns a response dict,
    or None when the message is a notification (no reply)."""
    req_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}

    # Notifications have no id and expect no reply.
    is_notification = "id" not in message

    if method == "initialize":
        return _result(req_id, _handle_initialize(params))
    if method == "notifications/initialized":
        return None
    if method in ("ping", "notifications/ping"):
        if is_notification:
            return None
        return _result(req_id, {})
    if method == "tools/list":
        return _result(req_id, _handle_tools_list(params))
    if method == "tools/call":
        try:
            return _result(req_id, _handle_tools_call(params))
        except ValueError as exc:
            return _error(req_id, INVALID_PARAMS, str(exc))
    if method == "shutdown":
        return _result(req_id, {})
    if is_notification:
        # Unknown notification: ignore per spec.
        return None
    return _error(req_id, METHOD_NOT_FOUND, f"method not found: {method}")


def serve(in_stream: IO[str] | None = None, out_stream: IO[str] | None = None) -> int:
    """Run the stdio loop. Returns 0 on clean EOF, 1 on fatal error."""
    stdin = in_stream if in_stream is not None else sys.stdin
    stdout = out_stream if out_stream is not None else sys.stdout
    try:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                _write(stdout, _error(None, PARSE_ERROR, f"parse error: {exc}"))
                continue
            try:
                response = dispatch(message)
            except Exception as exc:
                log.exception("dispatch failed")
                _write(
                    stdout,
                    _error(
                        message.get("id"),
                        INTERNAL_ERROR,
                        f"internal error: {exc}",
                        data=traceback.format_exc(),
                    ),
                )
                continue
            if response is not None:
                _write(stdout, response)
    except KeyboardInterrupt:
        return 0
    return 0


def main() -> int:
    """Entry point used by ``main.py --mcp-server``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    return serve()
