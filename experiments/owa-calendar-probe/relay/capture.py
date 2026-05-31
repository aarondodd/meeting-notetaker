"""Capture every OWA response + bridge event to disk.

Default behavior is paranoid: email local-parts get redacted to
``***@domain.com`` before write so a quick `git status` after a
session doesn't surface a workspace full of attendee addresses. The
config toggle (KEEP_EMAILS env or constructor arg) is there for the
case where the probe author needs the real addresses to debug
attendee enrichment.

Bridge-log lines stay light: timestamp, direction, verb, request_id,
size. Full payload bodies live in the per-request capture file so
``bridge.log`` itself stays grep-friendly.
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from . import paths


# Captures land at data/<UTC-ISO>-<verb>.json. Filename-safe ISO: drop
# colons, milliseconds.
_FNAME_TS_FMT = "%Y%m%dT%H%M%SZ"

# Email pattern intentionally lax -- OWA mixes the formal display
# "Sample User <user@example.com>" with raw addresses. We scrub
# wherever the @ is.
_EMAIL_RE = re.compile(
    r"(?P<local>[A-Za-z0-9._%+\-]+)@(?P<domain>[A-Za-z0-9.\-]+\.[A-Za-z]{2,})"
)


def _redact_email(match: re.Match[str]) -> str:
    return "***@" + match.group("domain")


def redact_emails(value: Any) -> Any:
    """Walk an OWA-shaped JSON tree, replacing email local parts.

    String values get the regex pass. Lists + dicts recurse. Everything
    else passes through. Returns a deep copy so the caller's original
    payload is unchanged."""
    if isinstance(value, str):
        return _EMAIL_RE.sub(_redact_email, value)
    if isinstance(value, list):
        return [redact_emails(v) for v in value]
    if isinstance(value, dict):
        return {k: redact_emails(v) for k, v in value.items()}
    return value


class CaptureLog:
    """Writes per-request capture files + a bridge.log tail.

    Constructed once at relay startup; safe to call concurrently from
    the TCP server thread + the Qt main thread."""

    def __init__(self, *, redact: bool | None = None) -> None:
        # Default to redacting. KEEP_EMAILS=1 disables it.
        if redact is None:
            redact = os.environ.get("MN_PROBE_KEEP_EMAILS", "") != "1"
        self.redact = redact
        self._lock = threading.Lock()
        self._bridge_log = paths.bridge_log_path()
        self._bridge_log.parent.mkdir(parents=True, exist_ok=True)

    # ---- per-request capture (the JSON dump goes here) ------------------

    def record_owa_response(
        self,
        *,
        verb: str,
        request_id: str,
        payload: dict[str, Any],
    ) -> Path:
        """Write the OWA response body + envelope to a timestamped file."""
        body = payload.get("body")
        url = payload.get("url", "")
        captured = {
            "captured_at": _utc_iso_now(),
            "request_id": request_id,
            "verb": verb,
            "ok": bool(payload.get("ok", False)),
            "status": int(payload.get("status", 0)),
            "url": url,
            "owa_build": payload.get("owa_build", ""),
            "redacted": self.redact,
            "headers": payload.get("headers", {}),
            "error": payload.get("error", ""),
            "body": copy.deepcopy(body),
        }
        if self.redact:
            captured["body"] = redact_emails(captured["body"])
            captured["url"] = redact_emails(captured["url"])

        ts = datetime.utcnow().strftime(_FNAME_TS_FMT)
        safe_verb = verb.replace(".", "-").replace("/", "-")
        out = paths.data_dir() / f"{ts}-{safe_verb}-{request_id[-6:] or 'na'}.json"
        out.write_text(json.dumps(captured, indent=2, sort_keys=False), encoding="utf-8")
        return out

    def record_owa_request(
        self,
        *,
        verb: str,
        request_id: str,
        params: dict[str, Any],
    ) -> None:
        """Append a bridge.log line for the outbound request. The
        full request body isn't captured here -- params are small."""
        # Redact emails *inside* params (people.lookup uses email),
        # never the keys themselves.
        safe_params = redact_emails(params) if self.redact else params
        self._append_bridge_log(
            f"out {verb} rid={request_id} params={json.dumps(safe_params, sort_keys=True)}",
        )

    # ---- bridge.log (the running tail) ----------------------------------

    def info(self, msg: str) -> None:
        self._append_bridge_log("info " + msg)

    def warn(self, msg: str) -> None:
        self._append_bridge_log("warn " + msg)

    def bridge_event(
        self,
        *,
        direction: str,
        verb: str,
        request_id: str,
        size: int,
    ) -> None:
        self._append_bridge_log(
            f"{direction} verb={verb} rid={request_id} size={size}"
        )

    # ---- internals ------------------------------------------------------

    def _append_bridge_log(self, line: str) -> None:
        stamped = f"{_utc_iso_now()} {line}\n"
        with self._lock:
            with self._bridge_log.open("a", encoding="utf-8") as f:
                f.write(stamped)


def _utc_iso_now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
