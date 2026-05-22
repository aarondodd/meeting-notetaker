"""Typed messages exchanged between the app, the native host bridge,
and the Chrome extension.

Wire format is JSON. The same dict shape flows over both the TCP
loopback hop (app <-> native host) and the Chrome native-messaging hop
(native host <-> extension). Keeping one schema avoids a translation
layer in the host.

Direction conventions:
  * ``APP_*``  -- app produces, extension consumes.
  * ``EXT_*``  -- extension produces, app consumes.

Every message carries a ``type`` discriminator and (for request /
response pairs) a ``request_id`` so the app can match results to the
synthesize call that produced them.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


# ---------------------------------------------------------------------------
# App -> extension
# ---------------------------------------------------------------------------


@dataclass
class SynthesizeRequest:
    """App asks the extension to drive a synthesis pass.

    ``new_chat=True`` always for v0.6.3 (see Conversation mode decision
    in PR #?? -- always start fresh). The field exists so future
    Conversation-mode setting changes don't require a protocol bump.
    """

    type: Literal["synthesize"] = "synthesize"
    request_id: str = ""
    target: str = "claude"  # see VALID_LLM_TARGETS in utils.config
    prompt: str = ""
    new_chat: bool = True

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PingRequest:
    """App probes whether the extension is reachable. The Verify button
    in the install wizard sends this and waits for an ``ext_pong``."""

    type: Literal["ping"] = "ping"
    request_id: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CancelRequest:
    """User pressed Cancel on an in-flight synthesis. The extension
    closes any tab it opened and stops scraping."""

    type: Literal["cancel"] = "cancel"
    request_id: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Extension -> app
# ---------------------------------------------------------------------------


# Status event names. Kept as a closed enum so the SessionView state-label
# code can switch on them without surprises.
STATUS_OPENING_TAB = "opening_tab"
STATUS_AWAITING_LOGIN = "awaiting_login"
STATUS_PROXY_ACK_NEEDED = "proxy_ack_needed"  # toast-only in v0.6.3 per Aaron's preference
STATUS_PROXY_ACK_CLEARED = "proxy_ack_cleared"
STATUS_PASTING = "pasting"
STATUS_AWAITING_RESPONSE = "awaiting_response"
STATUS_RESPONSE_STREAMING = "response_streaming"
STATUS_DONE = "done"


VALID_STATUS_EVENTS = frozenset({
    STATUS_OPENING_TAB,
    STATUS_AWAITING_LOGIN,
    STATUS_PROXY_ACK_NEEDED,
    STATUS_PROXY_ACK_CLEARED,
    STATUS_PASTING,
    STATUS_AWAITING_RESPONSE,
    STATUS_RESPONSE_STREAMING,
    STATUS_DONE,
})


# Error codes. The extension picks one when surfacing an issue; the app
# can map each to a user-facing message + fallback action.
ERR_NO_TAB = "no_tab"
ERR_NOT_LOGGED_IN = "not_logged_in"
ERR_PASTE_FAILED = "paste_failed"
ERR_TIMEOUT = "timeout"
ERR_INTERSTITIAL_TIMEOUT = "interstitial_timeout"
ERR_UNKNOWN = "unknown"


VALID_ERROR_CODES = frozenset({
    ERR_NO_TAB,
    ERR_NOT_LOGGED_IN,
    ERR_PASTE_FAILED,
    ERR_TIMEOUT,
    ERR_INTERSTITIAL_TIMEOUT,
    ERR_UNKNOWN,
})


@dataclass
class PongResponse:
    type: Literal["pong"] = "pong"
    request_id: str = ""
    extension_version: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StatusEvent:
    type: Literal["status"] = "status"
    request_id: str = ""
    event: str = ""  # one of STATUS_* above
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SynthesizeResult:
    """The synthesized markdown, streamed back when the LLM finishes."""

    type: Literal["result"] = "result"
    request_id: str = ""
    markdown: str = ""
    target: str = "claude"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ErrorEvent:
    type: Literal["error"] = "error"
    request_id: str = ""
    code: str = ERR_UNKNOWN
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Native-host handshake (TCP hop only, never reaches the extension)
# ---------------------------------------------------------------------------


@dataclass
class HandshakeRequest:
    """Native-host process tells the app who it is + its auth token.

    The token is rotated at app startup and written to ``bridge.json``;
    only a process able to read that file can talk to the app, which
    keeps random localhost scanners out. The host's first wire message
    must be a HandshakeRequest; everything else gets dropped until the
    handshake succeeds.
    """

    type: Literal["handshake"] = "handshake"
    token: str = ""
    host_version: str = ""
    extension_id: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HandshakeAck:
    type: Literal["handshake_ack"] = "handshake_ack"
    accepted: bool = False
    detail: str = ""
    app_version: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def encode(obj: Any) -> dict[str, Any]:
    """Best-effort JSON-dict form. Accepts a dataclass or a raw dict;
    the bridge gets both in practice (typed for outbound, raw for
    pass-through forwarding)."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_json"):
        return obj.to_json()
    return asdict(obj)
