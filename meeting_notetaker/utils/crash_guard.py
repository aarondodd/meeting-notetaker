"""Route unhandled exceptions and Qt diagnostics into the log.

PyQt6 handles an exception that escapes a slot by printing the
traceback to stderr and then calling ``qFatal()``, unless
``sys.excepthook`` has been replaced. In the frozen windowed build
stderr is ``None``, so the traceback goes nowhere, and on Windows Qt 6
aborts through ``__fastfail``, which bypasses the faulthandler hook the
MainLoopWatchdog installs. The result is a process that vanishes with
nothing in the log at all.

``install()`` replaces ``sys.excepthook`` so a slot exception is
logged with its full traceback and the event loop keeps running, and
also covers exceptions in plain ``threading.Thread`` targets and Qt's
own warning / critical / fatal messages (e.g. "QThread: Destroyed while
thread is still running"), which otherwise also go to the null stderr.
"""
from __future__ import annotations

import logging
import sys
import threading
import traceback
from typing import Callable, Optional

log = logging.getLogger("meeting_notetaker.crash_guard")

# Called on the GUI thread's behalf after an unhandled exception has
# been logged, so the app can tell the user something went wrong
# without a modal dialog (a dialog raised from inside a failing slot
# can itself re-enter the failing code). Set by MainApp.
_notifier: Optional[Callable[[str], None]] = None
_installed = False

# Qt repeats some warnings on every paint or layout pass. Each distinct
# message is logged this many times, then once more with a suppression
# note, then dropped. Fatal messages are never suppressed.
QT_REPEAT_LIMIT = 20
_qt_message_counts: dict[str, int] = {}


def set_notifier(fn: Optional[Callable[[str], None]]) -> None:
    global _notifier
    _notifier = fn


def _flush_handlers() -> None:
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:  # noqa: BLE001
            pass


def format_exception(exc_type, exc_value, exc_tb) -> str:
    return "".join(traceback.format_exception(exc_type, exc_value, exc_tb)).rstrip()


def excepthook(exc_type, exc_value, exc_tb) -> None:
    """Replacement for sys.excepthook: log, flush, notify, keep running."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    log.critical(
        "Unhandled exception (app kept running):\n%s",
        format_exception(exc_type, exc_value, exc_tb),
    )
    _flush_handlers()
    notifier = _notifier
    if notifier is not None:
        try:
            notifier(f"{exc_type.__name__}: {exc_value}")
        except Exception:  # noqa: BLE001
            log.exception("crash_guard notifier failed")


def thread_excepthook(args: threading.ExceptHookArgs) -> None:
    if args.exc_type is SystemExit:
        return
    name = args.thread.name if args.thread is not None else "<unknown>"
    log.critical(
        "Unhandled exception in thread %s:\n%s",
        name,
        format_exception(args.exc_type, args.exc_value, args.exc_traceback),
    )
    _flush_handlers()


def qt_message_handler(mode, context, message: str) -> None:
    """Forward Qt's qDebug/qWarning/qCritical/qFatal output to logging.

    qFatal still aborts after this returns; logging and flushing here
    is what gets the reason onto disk first.
    """
    from PyQt6.QtCore import QtMsgType  # noqa: PLC0415

    where = ""
    try:
        if context is not None and context.file:
            where = f" ({context.file}:{context.line})"
    except Exception:  # noqa: BLE001
        pass
    if mode == QtMsgType.QtFatalMsg:
        log.critical("Qt fatal: %s%s", message, where)
        _flush_handlers()
        return
    count = _qt_message_counts.get(message, 0) + 1
    _qt_message_counts[message] = count
    if count > QT_REPEAT_LIMIT + 1:
        return
    if count == QT_REPEAT_LIMIT + 1:
        where += " [repeated; further occurrences suppressed]"
    if mode == QtMsgType.QtCriticalMsg:
        log.error("Qt critical: %s%s", message, where)
    elif mode == QtMsgType.QtWarningMsg:
        log.warning("Qt warning: %s%s", message, where)
    else:
        log.debug("Qt: %s%s", message, where)


def install() -> None:
    """Install all three hooks. Idempotent."""
    global _installed
    if _installed:
        return
    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    try:
        from PyQt6.QtCore import qInstallMessageHandler  # noqa: PLC0415

        qInstallMessageHandler(qt_message_handler)
    except Exception:  # noqa: BLE001
        log.exception("crash_guard: qInstallMessageHandler failed")
    _installed = True
