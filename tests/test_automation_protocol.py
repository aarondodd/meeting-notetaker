"""Chrome native-messaging length-prefixed framing.

The framing rules come from Chrome's native-messaging spec and we
re-use them on both hops (extension <-> host <-> app). Getting them
wrong silently corrupts every message, so this file exercises the
edge cases: empty payload, multi-message stream, oversize, partial
read, non-UTF-8 body, non-JSON body, non-dict body.
"""
from __future__ import annotations

import io
import json
import struct

import pytest

from meeting_notetaker.automation.protocol import (
    MAX_MESSAGE_BYTES,
    ProtocolError,
    encode_message,
    read_message,
    write_message,
)


def _frame(body: bytes) -> bytes:
    return struct.pack("<I", len(body)) + body


def test_encode_round_trip_simple_dict():
    payload = {"type": "ping", "request_id": "abc"}
    frame = encode_message(payload)
    # 4-byte LE length + UTF-8 body.
    assert len(frame) == 4 + len(json.dumps(payload).encode("utf-8"))
    parsed = read_message(io.BytesIO(frame))
    assert parsed == payload


def test_read_two_messages_in_one_stream():
    a = encode_message({"type": "ping", "request_id": "1"})
    b = encode_message({"type": "ping", "request_id": "2"})
    stream = io.BytesIO(a + b)
    assert read_message(stream) == {"type": "ping", "request_id": "1"}
    assert read_message(stream) == {"type": "ping", "request_id": "2"}
    assert read_message(stream) is None  # clean EOF


def test_empty_stream_returns_none():
    assert read_message(io.BytesIO(b"")) is None


def test_zero_length_body_returns_empty_dict():
    """A 4-byte header of 0 followed by no body parses as {}."""
    frame = struct.pack("<I", 0)
    assert read_message(io.BytesIO(frame)) == {}


def test_unicode_payload_round_trips_through_utf8():
    payload = {"detail": "naïve resume: don’t do that"}
    frame = encode_message(payload)
    parsed = read_message(io.BytesIO(frame))
    assert parsed == payload


def test_oversize_outbound_raises():
    huge = {"x": "a" * (MAX_MESSAGE_BYTES + 1)}
    with pytest.raises(ProtocolError, match="too large"):
        encode_message(huge)


def test_oversize_declared_length_raises_on_read():
    # Header claims a body larger than the cap. We must not allocate
    # a 4 GB buffer trying to honor it.
    header = struct.pack("<I", MAX_MESSAGE_BYTES + 1)
    with pytest.raises(ProtocolError, match="exceeds limit"):
        read_message(io.BytesIO(header + b"x"))


def test_partial_body_raises():
    """Header says 10 bytes; peer closed after 4. Must surface as a
    protocol error, not a silent truncation."""
    frame = struct.pack("<I", 10) + b"abcd"
    with pytest.raises(ProtocolError, match="short read"):
        read_message(io.BytesIO(frame))


def test_partial_header_raises():
    """3 bytes where 4 are expected -> short read in header."""
    with pytest.raises(ProtocolError, match="short read"):
        read_message(io.BytesIO(b"abc"))


def test_non_utf8_body_raises():
    bad = _frame(b"\xff\xfe\xfd")
    with pytest.raises(ProtocolError, match="UTF-8"):
        read_message(io.BytesIO(bad))


def test_non_json_body_raises():
    bad = _frame(b"not json at all")
    with pytest.raises(ProtocolError, match="JSON"):
        read_message(io.BytesIO(bad))


def test_non_object_body_raises():
    """The spec says messages are objects. A bare array or string is
    invalid -- if we accepted them callers would have to type-check
    every read, defeating the schema."""
    bad = _frame(b'["not", "a", "dict"]')
    with pytest.raises(ProtocolError, match="JSON object"):
        read_message(io.BytesIO(bad))


def test_write_message_flushes():
    """write_message must flush after each frame so a buffered stream
    delivers the message to the peer immediately (extensions wait on
    individual frames, not buffered chunks)."""

    class CountingStream(io.BytesIO):
        flushes = 0

        def flush(self):  # type: ignore[override]
            self.flushes += 1
            super().flush()

    stream = CountingStream()
    write_message(stream, {"type": "ping"})
    assert stream.flushes == 1
