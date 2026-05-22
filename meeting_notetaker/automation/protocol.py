"""Wire framing for both hops.

Chrome native messaging uses a length-prefixed JSON protocol over the
host's stdio: each message is a 4-byte little-endian unsigned int
followed by that many UTF-8 bytes of JSON. We reuse the exact same
framing on the TCP loopback hop between the native host and the
running app so the host is a near-trivial bytes copier.

Spec: https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging#native-messaging-host-protocol
"""
from __future__ import annotations

import json
import struct
from typing import Any, BinaryIO

# Native messaging caps payloads at 1 MB inbound and 64 MB outbound. A
# synthesis prompt + response easily fits in 1 MB but we keep the
# tighter ceiling explicit so a malformed length field doesn't make us
# try to allocate 4 GB.
MAX_MESSAGE_BYTES = 1024 * 1024


class ProtocolError(Exception):
    """Raised on framing violations: short reads, oversize messages,
    or non-UTF-8 / non-JSON payloads."""


def encode_message(payload: dict[str, Any]) -> bytes:
    """Serialize a JSON dict into the length-prefixed wire form."""
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
    """Read one framed message. Returns None on clean EOF (peer closed
    between messages). Raises ProtocolError on framing violations."""
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
    """Read exactly ``n`` bytes. Returns None if the stream is at EOF
    before any data has been read; raises ProtocolError on partial
    read (peer closed mid-message)."""
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
