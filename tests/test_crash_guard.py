"""crash_guard: an exception escaping a Qt slot must be logged, not fatal.

PyQt6 calls qFatal() on an unhandled slot exception unless
sys.excepthook has been replaced. In the windowed Windows build that
aborts the process with nothing in the log, which is how clicks in the
session list were killing the app. The subprocess tests pin both sides:
without the guard the process dies, with it the process survives and
the traceback reaches the log.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from meeting_notetaker.utils import crash_guard

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

_SLOT_SCRIPT = textwrap.dedent(
    """
    import logging, os, sys
    sys.path.insert(0, {root!r})
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    logging.basicConfig(filename={log!r}, level=logging.INFO)
    from meeting_notetaker.utils import crash_guard
    if {guard}:
        crash_guard.install()
    from PyQt6.QtCore import QObject, QTimer, pyqtSignal
    from PyQt6.QtWidgets import QApplication
    app = QApplication([])
    class Emitter(QObject):
        fired = pyqtSignal()
    e = Emitter()
    def bad_slot():
        raise KeyError("session-list-click")
    e.fired.connect(bad_slot)
    QTimer.singleShot(10, e.fired.emit)
    QTimer.singleShot(300, app.quit)
    app.exec()
    logging.info("event loop survived")
    """
)


def _run_slot_script(tmp_path: Path, *, guard: bool) -> tuple[int, str]:
    pytest.importorskip("PyQt6.QtWidgets")
    log_file = tmp_path / f"guard_{guard}.log"
    script = _SLOT_SCRIPT.format(root=str(_PROJECT_ROOT), log=str(log_file), guard=guard)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, timeout=60,
    )
    text = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
    return proc.returncode, text


def test_unguarded_slot_exception_kills_process(tmp_path):
    # Control: this is the failure mode the guard exists for.
    rc, log_text = _run_slot_script(tmp_path, guard=False)
    assert rc != 0
    assert "event loop survived" not in log_text


def test_guarded_slot_exception_is_logged_and_survived(tmp_path):
    rc, log_text = _run_slot_script(tmp_path, guard=True)
    assert rc == 0
    assert "event loop survived" in log_text
    assert "Unhandled exception" in log_text
    assert "KeyError: 'session-list-click'" in log_text
    assert "bad_slot" in log_text


def test_excepthook_logs_and_notifies(caplog):
    seen = []
    crash_guard.set_notifier(seen.append)
    try:
        try:
            raise ValueError("boom")
        except ValueError:
            with caplog.at_level(logging.CRITICAL, logger="meeting_notetaker.crash_guard"):
                crash_guard.excepthook(*sys.exc_info())
    finally:
        crash_guard.set_notifier(None)
    assert "ValueError: boom" in caplog.text
    assert seen == ["ValueError: boom"]


def test_excepthook_survives_a_failing_notifier(caplog):
    def broken(_msg):
        raise RuntimeError("notifier broke")

    crash_guard.set_notifier(broken)
    try:
        try:
            raise ValueError("first")
        except ValueError:
            crash_guard.excepthook(*sys.exc_info())
    finally:
        crash_guard.set_notifier(None)
    assert "ValueError: first" in caplog.text


def test_thread_excepthook_logs_thread_name(caplog):
    def target():
        raise OSError("disk gone")

    t = threading.Thread(target=target, name="WorkerX")
    old = threading.excepthook
    threading.excepthook = crash_guard.thread_excepthook
    try:
        with caplog.at_level(logging.CRITICAL, logger="meeting_notetaker.crash_guard"):
            t.start()
            t.join()
    finally:
        threading.excepthook = old
    assert "WorkerX" in caplog.text
    assert "OSError: disk gone" in caplog.text


def test_qt_warning_repeats_are_capped(caplog, monkeypatch):
    QtCore = pytest.importorskip("PyQt6.QtCore")
    monkeypatch.setattr(crash_guard, "_qt_message_counts", {})
    monkeypatch.setattr(crash_guard, "QT_REPEAT_LIMIT", 3)
    with caplog.at_level(logging.WARNING, logger="meeting_notetaker.crash_guard"):
        for _ in range(10):
            crash_guard.qt_message_handler(QtCore.QtMsgType.QtWarningMsg, None, "noisy")
    lines = [r for r in caplog.records if "noisy" in r.getMessage()]
    assert len(lines) == 4
    assert "suppressed" in lines[-1].getMessage()
