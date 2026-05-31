"""Length-prefixed JSON framing for Chrome native messaging.

Mirrors the production app's protocol.py byte-for-byte so the same
framing works on both the stdio (extension hop) and TCP (relay hop)
sides. Vendored rather than imported so the experiment has no
runtime dependency on the prod package.
"""
from __future__ import annotations

import json
import struct
from typing import Any, BinaryIO


# Chrome native-messaging cap: 1 MB inbound, 64 MB outbound. We honor
# the tighter ceiling everywhere so a corrupt length prefix can't
# trigger a multi-GB allocation.
MAX_MESSAGE_BYTES = 1024 * 1024


class ProtocolError(Exception):
    """Framing violation -- short read, oversize body, non-JSON."""


def encode_message(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_MESSAGE_BYTES:
        raise ProtocolError(
            f"message too large: {len(body)} bytes (limit {MAX_MESSAGE_BYTES})"
        )
    return struct.pack("<I", len(body)) + body


def write_message(stream: BinaryIO, payload: dict[str, Any]) -> None:
    stream.write(encode_message(payload))
    stream.flush()


def read_message(stream: BinaryIO) -> dict[str, Any] | None:
    header = _read_exact(stream, 4)
    if header is None:
        return None
    (length,) = struct.unpack("<I", header)
    if length == 0:
        return {}
    if length > MAX_MESSAGE_BYTES:
        raise ProtocolError(
            f"declared message length {length} exceeds limit {MAX_MESSAGE_BYTES}"
        )
    body = _read_exact(stream, length)
    if body is None:
        raise ProtocolError(
            f"short read: expected {length} bytes, peer closed mid-message"
        )
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError(f"message body is not UTF-8: {exc}") from exc
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"message body is not JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ProtocolError(
            f"message body must be a JSON object, got {type(parsed).__name__}"
        )
    return parsed


def _read_exact(stream: BinaryIO, n: int) -> bytes | None:
    chunks: list[bytes] = []
    remaining = n
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            if not chunks:
                return None
            raise ProtocolError(
                f"short read: expected {n} bytes, got {n - remaining}"
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
