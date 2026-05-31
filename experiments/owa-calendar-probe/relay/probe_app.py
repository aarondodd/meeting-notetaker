"""Minimal PyQt6 GUI relay for the OWA Calendar Probe.

What this exists to prove:
  * A Chrome content script running on outlook.office.com can pull the
    user's calendar via OWA's internal API with no Entra registration.
  * Attendee enrichment (title / company / department) returns useful
    fields for tenant-resolved people.
  * Attachments can be fetched as bytes and persisted locally.
  * The whole flow is loggable enough that breakage surfaces as a
    diffable capture file rather than a vague "it didn't work".

What this intentionally does NOT do:
  * Touch the production meeting_notetaker package or its data dir.
  * Persist any session beyond the current launch (bridge.json + log
    files are the only state).
  * Render bodies / attendees richly -- the table is a thin debug view.

Run::

    python relay/probe_app.py             # default GUI
    python relay/probe_app.py --no-redact # KEEP_EMAILS=1 equivalent
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

# Allow direct invocation (`python relay/probe_app.py`).
_THIS = Path(__file__).resolve()
if str(_THIS.parent.parent) not in sys.path:
    sys.path.insert(0, str(_THIS.parent.parent))

from PyQt6.QtCore import Qt, QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from relay import paths  # noqa: E402
from relay.bridge import Bridge  # noqa: E402
from relay.capture import CaptureLog  # noqa: E402


log = logging.getLogger("probe_app")


# ---------------------------------------------------------------------------
# Bridge <-> Qt glue


class BridgeSignals(QObject):
    """Marshals bridge callbacks onto the Qt main thread.

    The bridge's RX pump runs in a worker thread; Qt widgets must not
    be touched from there. Emitting a signal hops over the event loop."""

    message_received = pyqtSignal(dict)
    state_changed = pyqtSignal(str, str)


# ---------------------------------------------------------------------------
# Main window


class ProbeWindow(QMainWindow):
    REQUEST_TIMEOUT_SEC = 30.0

    def __init__(self, *, redact: bool) -> None:
        super().__init__()
        self.setWindowTitle("MN OWA Calendar Probe")
        self.resize(1100, 720)

        self._capture = CaptureLog(redact=redact)
        # _port is read by the "listening" state callback, which Bridge
        # invokes synchronously from start() before start() returns.
        # Initialize it first so the early callback doesn't AttributeError.
        self._port = 0
        self._signals = BridgeSignals()
        self._signals.message_received.connect(self._on_bridge_message)
        self._signals.state_changed.connect(self._on_bridge_state)
        self._bridge = Bridge(
            on_message=lambda m: self._signals.message_received.emit(m),
            on_state_change=lambda s, d: self._signals.state_changed.emit(s, d),
        )
        # request_id -> dict describing the pending request (used to
        # match owa_response back to a UI row + decide whether to write
        # an attachment to disk).
        self._inflight: dict[str, dict] = {}
        # Sequential request id assignment so capture filenames sort
        # by issue order.
        self._req_seq = 0
        # Captured events from the latest calendar.fetch, keyed by
        # event_id. Used by the Resolve / Pull buttons.
        self._events: dict[str, dict] = {}

        self._build_ui()
        self._port = self._bridge.start()
        self.statusBar().showMessage(
            f"Bridge listening on 127.0.0.1:{self._port} -- waiting for extension"
        )
        # Live log tail.
        self._tail_pos = 0
        self._tail_timer = QTimer(self)
        self._tail_timer.setInterval(750)
        self._tail_timer.timeout.connect(self._poll_log_tail)
        self._tail_timer.start()

    # ---- UI -------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)

        top = QHBoxLayout()
        self.btn_today = QPushButton("Fetch today")
        self.btn_today.clicked.connect(lambda: self._fetch_calendar(days_ahead=0))
        self.btn_week = QPushButton("Fetch next 7 days")
        self.btn_week.clicked.connect(lambda: self._fetch_calendar(days_ahead=7))
        self.btn_resolve = QPushButton("Resolve attendees for selected")
        self.btn_resolve.clicked.connect(self._resolve_selected)
        self.btn_pull = QPushButton("Pull attachments for selected")
        self.btn_pull.clicked.connect(self._pull_attachments_selected)
        self.btn_bundle = QPushButton("Copy log bundle...")
        self.btn_bundle.clicked.connect(self._copy_log_bundle)
        for b in (
            self.btn_today, self.btn_week, self.btn_resolve,
            self.btn_pull, self.btn_bundle,
        ):
            top.addWidget(b)
        top.addStretch(1)
        outer.addLayout(top)

        splitter = QSplitter(Qt.Orientation.Vertical, self)
        outer.addWidget(splitter, 1)

        self.table = QTableWidget(0, 6, self)
        self.table.setHorizontalHeaderLabels(
            ["Start (local)", "End (local)", "Subject", "Organizer",
             "Attendees", "Attachments?"]
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        splitter.addWidget(self.table)

        self.tail = QPlainTextEdit(self)
        self.tail.setReadOnly(True)
        self.tail.setFont(QFont("Menlo, Consolas, monospace"))
        self.tail.setMaximumBlockCount(5000)
        splitter.addWidget(self.tail)
        splitter.setSizes([460, 220])

        self.setStatusBar(QStatusBar(self))

    # ---- bridge callbacks ----------------------------------------------

    def _on_bridge_state(self, state: str, detail: str) -> None:
        if state == "connected":
            self.statusBar().showMessage("Extension connected")
        elif state == "disconnected":
            self.statusBar().showMessage(
                f"Extension disconnected ({detail or 'no detail'}); "
                "click an extension popup button or reload the OWA tab"
            )
        elif state == "listening":
            self.statusBar().showMessage(
                f"Bridge listening on 127.0.0.1:{self._port}"
            )

    def _on_bridge_message(self, msg: dict) -> None:
        msg_type = msg.get("type", "")
        if msg_type == "owa_response":
            self._handle_owa_response(msg)
        elif msg_type == "owa_error":
            self._handle_owa_error(msg)
        elif msg_type == "pong":
            self._capture.info(
                f"pong from ext v{msg.get('extension_version', '?')}"
            )
        elif msg_type == "bridge_ready":
            # Sent by our own bridge to the extension; should not arrive
            # back. Ignore defensively.
            pass
        else:
            self._capture.warn(f"unhandled bridge msg: {msg_type}")

    # ---- request issue --------------------------------------------------

    def _next_request_id(self, verb: str) -> str:
        self._req_seq += 1
        return f"{verb.replace('.', '-')}-{self._req_seq:04d}"

    def _send_request(self, verb: str, params: dict[str, Any]) -> Optional[str]:
        request_id = self._next_request_id(verb)
        payload = {
            "type": "owa_request",
            "request_id": request_id,
            "verb": verb,
            "params": params,
        }
        if not self._bridge.send(payload):
            QMessageBox.warning(
                self,
                "No connection",
                "The probe extension hasn't connected yet. "
                "Open chrome://extensions, make sure the probe is "
                "loaded, then open outlook.office.com.",
            )
            return None
        self._inflight[request_id] = {
            "verb": verb,
            "params": params,
            "started_at": time.monotonic(),
        }
        self._capture.record_owa_request(
            verb=verb, request_id=request_id, params=params,
        )
        self._capture.bridge_event(
            direction="out",
            verb=verb,
            request_id=request_id,
            size=len(json.dumps(payload)),
        )
        # Fire a timeout watcher; the bridge doesn't enforce one.
        QTimer.singleShot(
            int(self.REQUEST_TIMEOUT_SEC * 1000),
            lambda: self._timeout_check(request_id),
        )
        return request_id

    def _timeout_check(self, request_id: str) -> None:
        if request_id in self._inflight:
            self._capture.warn(
                f"request {request_id} timed out after {self.REQUEST_TIMEOUT_SEC:.0f}s"
            )
            self._inflight.pop(request_id, None)

    # ---- calendar fetch -------------------------------------------------

    def _fetch_calendar(self, *, days_ahead: int) -> None:
        # OWA expects UTC; we anchor to local midnight today, ending at
        # 23:59 days_ahead later. start <= event.start <= end. Wider
        # windows return more events; we trust the $top=100 cap.
        now_local = datetime.now()
        start_local = datetime(now_local.year, now_local.month, now_local.day)
        end_local = start_local + timedelta(days=max(1, days_ahead + 1))
        start_utc = start_local.astimezone(timezone.utc)
        end_utc = (end_local - timedelta(seconds=1)).astimezone(timezone.utc)
        self._events.clear()
        self.table.setRowCount(0)
        self._send_request(
            "calendar.fetch",
            {
                "start_iso": start_utc.isoformat().replace("+00:00", "Z"),
                "end_iso": end_utc.isoformat().replace("+00:00", "Z"),
            },
        )

    def _handle_owa_response(self, msg: dict) -> None:
        request_id = msg.get("request_id", "")
        verb = msg.get("verb", "")
        entry = self._inflight.pop(request_id, None)
        self._capture.bridge_event(
            direction="in",
            verb=verb,
            request_id=request_id,
            size=len(json.dumps(msg)),
        )
        capture_path = self._capture.record_owa_response(
            verb=verb, request_id=request_id, payload=msg,
        )
        self._capture.info(f"captured {verb} -> {capture_path.name}")

        if not msg.get("ok", False):
            self._capture.warn(
                f"{verb} returned status={msg.get('status')} err={msg.get('error')}"
            )
            return

        if verb == "calendar.fetch":
            self._render_calendar_response(msg.get("body") or {})
        elif verb == "people.lookup":
            self._capture.info(
                f"people.lookup ({entry or {}}) -> see capture"
            )
        elif verb == "attachments.list":
            event_id = (entry or {}).get("params", {}).get("event_id", "")
            self._auto_pull_attachments(msg, event_id)
        elif verb == "attachments.fetch":
            self._persist_attachment(msg, entry or {})

    def _handle_owa_error(self, msg: dict) -> None:
        self._capture.warn(
            f"owa_error code={msg.get('code')} detail={msg.get('detail')}"
        )
        self._inflight.pop(msg.get("request_id", ""), None)

    def _render_calendar_response(self, body: dict) -> None:
        events = body.get("value") or []
        self.table.setRowCount(len(events))
        for row, ev in enumerate(events):
            event_id = ev.get("id", "")
            self._events[event_id] = ev
            subject = ev.get("subject") or "(no subject)"
            organizer = (ev.get("organizer") or {}).get("emailAddress", {})
            organizer_name = organizer.get("name") or organizer.get("address") or ""
            attendees = ev.get("attendees") or []
            has_att = ev.get("hasAttachments")
            self.table.setItem(row, 0, _cell(_fmt_owa_time(ev.get("start"))))
            self.table.setItem(row, 1, _cell(_fmt_owa_time(ev.get("end"))))
            self.table.setItem(row, 2, _cell(subject))
            self.table.setItem(row, 3, _cell(organizer_name))
            self.table.setItem(row, 4, _cell(str(len(attendees))))
            self.table.setItem(row, 5, _cell("yes" if has_att else "no"))
            # Stash event_id on the row so resolve / pull can find it.
            self.table.item(row, 0).setData(
                Qt.ItemDataRole.UserRole, event_id
            )
        self._capture.info(f"rendered {len(events)} events")

    # ---- attendee resolve ----------------------------------------------

    def _resolve_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        item = self.table.item(row, 0)
        if not item:
            return
        event_id = item.data(Qt.ItemDataRole.UserRole) or ""
        ev = self._events.get(event_id)
        if not ev:
            return
        attendees = ev.get("attendees") or []
        emails: list[str] = []
        for a in attendees:
            ea = (a or {}).get("emailAddress") or {}
            addr = ea.get("address")
            if addr:
                emails.append(addr)
        # Also resolve the organizer; useful even for solo organizer
        # meetings where attendees is empty.
        org_addr = (ev.get("organizer") or {}).get("emailAddress", {}).get("address")
        if org_addr and org_addr not in emails:
            emails.insert(0, org_addr)
        if not emails:
            self._capture.info("no attendee emails to resolve")
            return
        self._capture.info(
            f"resolving {len(emails)} emails for event {event_id[:12]}..."
        )
        for addr in emails:
            self._send_request("people.lookup", {"email": addr})

    # ---- attachments ----------------------------------------------------

    def _pull_attachments_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        item = self.table.item(row, 0)
        if not item:
            return
        event_id = item.data(Qt.ItemDataRole.UserRole) or ""
        ev = self._events.get(event_id)
        if not ev:
            return
        if not ev.get("hasAttachments"):
            self._capture.info("selected event reports hasAttachments=false")
            return
        # Two-step: list metadata, then user-driven fetch via the next
        # response handler. Listing is cheap; binary $value is not.
        self._capture.info(
            f"requesting attachments list for event {event_id[:12]}..."
        )
        self._send_request("attachments.list", {"event_id": event_id})
        # The subsequent attachments.fetch calls are issued once
        # attachments.list comes back -- see _handle_owa_response +
        # _auto_pull_attachments below.

    def _auto_pull_attachments(self, list_response: dict, event_id: str) -> None:
        """When attachments.list returns, issue an attachments.fetch
        per item up to a generous cap. The probe author can grep the
        capture later to decide whether the metadata-only path is
        enough for their feature."""
        body = list_response.get("body") or {}
        items = body.get("value") or []
        for att in items[:5]:
            self._send_request(
                "attachments.fetch",
                {
                    "event_id": event_id,
                    "attachment_id": att.get("id", ""),
                    "_friendly_name": att.get("name", ""),
                },
            )

    def _persist_attachment(self, msg: dict, entry: dict) -> None:
        """Write the base64-decoded body to data/attachments/<name>."""
        body = msg.get("body") or {}
        b64 = body.get("_b64", "") if isinstance(body, dict) else ""
        if not b64:
            self._capture.warn("attachments.fetch returned no _b64 body")
            return
        import base64
        try:
            raw = base64.b64decode(b64)
        except (ValueError, TypeError) as exc:
            self._capture.warn(f"attachments.fetch base64 decode failed: {exc}")
            return
        target_dir = paths.data_dir() / "attachments"
        target_dir.mkdir(parents=True, exist_ok=True)
        name = (entry.get("params") or {}).get("_friendly_name") or msg.get("request_id", "att")
        safe_name = "".join(c for c in name if c.isalnum() or c in "._- ")[:100] or "att"
        target = target_dir / f"{int(time.time())}-{safe_name}"
        target.write_bytes(raw)
        self._capture.info(
            f"attachment saved {len(raw)} bytes -> {target.name}"
        )

    # ---- log tail + bundle ---------------------------------------------

    def _poll_log_tail(self) -> None:
        log_path = paths.bridge_log_path()
        if not log_path.exists():
            return
        try:
            with log_path.open("r", encoding="utf-8") as f:
                f.seek(self._tail_pos)
                chunk = f.read()
                self._tail_pos = f.tell()
        except OSError:
            return
        if chunk:
            self.tail.appendPlainText(chunk.rstrip("\n"))

    def _copy_log_bundle(self) -> None:
        # Pull the last <= 50 capture JSONs + the bridge.log + the
        # extension version stamp. Drop them into a single zip the
        # user can attach to a bug report.
        captures = sorted(paths.data_dir().glob("*.json"))[-50:]
        log_path = paths.bridge_log_path()
        if not captures and not log_path.exists():
            QMessageBox.information(
                self, "Nothing to bundle",
                "No captures yet. Click Fetch today first.",
            )
            return

        suggested = (
            paths.data_dir().parent
            / f"probe-bundle-{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.zip"
        )
        target_str, _ = QFileDialog.getSaveFileName(
            self, "Save log bundle", str(suggested), "Zip files (*.zip)",
        )
        if not target_str:
            return
        target = Path(target_str)
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            for cap in captures:
                zf.write(cap, arcname=f"captures/{cap.name}")
            if log_path.exists():
                zf.write(log_path, arcname="bridge.log")
            # Include manifest.json from the extension so the recipient
            # can see what extension ID + version produced the bundle.
            manifest_src = paths.EXTENSION_DIR / "manifest.json"
            if manifest_src.exists():
                zf.write(manifest_src, arcname="extension-manifest.json")
        QMessageBox.information(
            self, "Bundle saved", f"Wrote {target}\nIncluded {len(captures)} captures.",
        )

    # ---- shutdown -------------------------------------------------------

    def closeEvent(self, event):  # noqa: N802 (Qt naming)
        try:
            self._bridge.stop()
        finally:
            super().closeEvent(event)


def _cell(text: str) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return item


def _fmt_owa_time(raw: Any) -> str:
    """OWA returns {"dateTime": "...", "timeZone": "..."}.

    The string is wall-clock local time in `timeZone`. For the probe
    view we render it in the user's *current* local timezone via a
    naive parse + tz-aware conversion."""
    if not isinstance(raw, dict):
        return ""
    dt_str = raw.get("dateTime") or ""
    tz_name = raw.get("timeZone") or "UTC"
    if not dt_str:
        return ""
    # OWA emits "2026-05-31T17:00:00.0000000" -- strip the fractional
    # micros, take the bare ISO.
    cleaned = dt_str.split(".")[0]
    try:
        naive = datetime.fromisoformat(cleaned)
    except ValueError:
        return cleaned
    # Best-effort tz attachment. zoneinfo is stdlib on Python 3.9+.
    try:
        from zoneinfo import ZoneInfo
        aware = naive.replace(tzinfo=ZoneInfo(tz_name))
    except Exception:  # pragma: no cover - tz fallbacks
        aware = naive.replace(tzinfo=timezone.utc)
    return aware.astimezone().strftime("%Y-%m-%d %H:%M")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-redact",
        action="store_true",
        help="Disable email local-part redaction in captures + bridge log.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    app = QApplication(sys.argv)
    win = ProbeWindow(redact=not args.no_redact)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
