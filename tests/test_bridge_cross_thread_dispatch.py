"""Regression test for the cross-thread dispatch bug.

The bridge reader thread emits a pyqtSignal to bounce inbound
messages to the Qt main thread. The original implementation used
``QTimer.singleShot(0, lambda: ...)`` instead, which silently failed
on Windows because the timer was created on a thread with no Qt
event loop. Aaron's 2026-05-22 repro had the bridge accept messages
but the result never reached the app -- this test pins the
signal-based dispatch path so the regression can't recur.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

# Force offscreen Qt so the test runs headless on dev hosts.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEventLoop, QObject, pyqtSignal  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def test_pyqtsignal_emit_from_worker_thread_routes_to_main(qt_app):
    """An emit from a non-Qt thread must invoke the slot on the
    main thread when auto-connection is used. This is the exact
    pattern the bridge uses to deliver inbound messages."""

    class Receiver(QObject):
        got_message = pyqtSignal(dict)

        def __init__(self):
            super().__init__()
            self.received: list[dict] = []
            self.thread_ids: list[int] = []
            self.got_message.connect(self._on_msg)

        def _on_msg(self, msg: dict) -> None:
            self.received.append(msg)
            self.thread_ids.append(threading.get_ident())

    receiver = Receiver()
    main_tid = threading.get_ident()

    def emit_from_worker():
        # Different thread -> Qt promotes connection to QueuedConnection.
        receiver.got_message.emit({"type": "result", "request_id": "r1"})

    worker = threading.Thread(target=emit_from_worker, daemon=True)
    worker.start()
    worker.join(timeout=2.0)

    # Pump the event loop briefly so the queued emit gets delivered.
    loop = QEventLoop()
    from PyQt6.QtCore import QTimer

    QTimer.singleShot(200, loop.quit)
    loop.exec()

    assert len(receiver.received) == 1, (
        "queued signal didn't reach the slot -- the cross-thread bounce "
        "is broken and the v0.6.3 result-not-returning bug is present"
    )
    assert receiver.received[0]["request_id"] == "r1"
    # The slot must run on the main thread, not the worker.
    assert receiver.thread_ids[0] == main_tid


def test_bridge_emits_message_via_signal(qt_app, tmp_path):
    """End-to-end exercise of the bridge -> MainApp pyqtSignal path.

    We don't instantiate the full MainApp (too heavy); instead we
    construct a minimal QObject with the same signal, set it up
    as the bridge's on_message callback (just like MainApp does),
    and verify a message sent over TCP reaches the slot.
    """
    import socket
    from meeting_notetaker.automation import messages as automation_messages
    from meeting_notetaker.automation.bridge import Bridge
    from meeting_notetaker.automation.protocol import write_message

    class FakeApp(QObject):
        bridge_message_received = pyqtSignal(dict)

        def __init__(self):
            super().__init__()
            self.received: list[dict] = []
            self.bridge_message_received.connect(self._on)

        def _on(self, msg: dict) -> None:
            self.received.append(msg)

        # The bridge calls this on its worker thread.
        def on_bridge_message(self, msg: dict) -> None:
            self.bridge_message_received.emit(msg)

    app_obj = FakeApp()
    bridge = Bridge(
        handshake_file=tmp_path / "bridge.json",
        on_message=app_obj.on_bridge_message,
    )
    bridge.start()
    try:
        # Connect + handshake as the native host would.
        sock = socket.create_connection(("127.0.0.1", bridge.port), timeout=2.0)
        stream = sock.makefile("rwb")
        hs = automation_messages.HandshakeRequest(token=bridge.token, host_version="t").to_json()
        write_message(stream, hs)
        from meeting_notetaker.automation.protocol import read_message
        ack = read_message(stream)
        assert ack["accepted"] is True

        # Wait for bridge to mark connected.
        deadline = time.time() + 2.0
        while time.time() < deadline and not bridge.is_connected:
            time.sleep(0.01)
        assert bridge.is_connected

        # Send a RESULT-shaped message.
        write_message(stream, {
            "type": "result",
            "request_id": "test-rid",
            "markdown": "hello from the test",
            "target": "claude",
        })

        # Pump the Qt event loop until the signal lands or we time out.
        loop = QEventLoop()
        from PyQt6.QtCore import QTimer
        timer = QTimer()
        timer.setInterval(50)
        timer.timeout.connect(lambda: app_obj.received and loop.quit())
        deadline_timer = QTimer()
        deadline_timer.setSingleShot(True)
        deadline_timer.setInterval(2000)
        deadline_timer.timeout.connect(loop.quit)
        timer.start()
        deadline_timer.start()
        loop.exec()
        timer.stop()
        deadline_timer.stop()

        assert len(app_obj.received) == 1, (
            "result sent over the bridge didn't reach the slot via the "
            "pyqtSignal -- the result-not-returning regression is back."
        )
        assert app_obj.received[0]["request_id"] == "test-rid"
        assert app_obj.received[0]["markdown"] == "hello from the test"
    finally:
        try:
            sock.shutdown(socket.SHUT_RDWR)
            sock.close()
        except OSError:
            pass
        bridge.stop()
