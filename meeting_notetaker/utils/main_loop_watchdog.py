"""Detect + capture main-event-loop stalls.

When Windows shows the "Not Responding" overlay (issue #31), the
log gives us no information about which call is blocking the main
thread. This watchdog closes that gap: it timestamps a QTimer tick
every ~100 ms, runs a background thread that compares the latest
timestamp against wall-clock, and when the gap crosses a threshold
(default 750 ms) dumps every Python thread's stack via faulthandler.
The dump goes to the same file the rest of the app logs to, so the
next freeze leaves a fingerprint we can read post-hoc.

Cost on a healthy run is negligible: one daemon thread sleeping
250 ms at a time, one QTimer doing a single monotonic() call. The
watchdog does NOT raise -- it logs and continues. After a dump, it
suppresses further dumps until the main loop ticks again, so a
multi-second freeze produces exactly one stack dump rather than
flooding the log.
"""
from __future__ import annotations

import faulthandler
import io
import logging
import threading
import time
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, QTimer


log = logging.getLogger(__name__)


class MainLoopWatchdog(QObject):
    """QObject-owning wrapper for the tick QTimer + watchdog thread."""

    def __init__(
        self,
        *,
        stall_threshold_ms: int = 750,
        tick_interval_ms: int = 100,
        check_interval_ms: int = 250,
        log_file_path: Optional[Path] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._stall_threshold = stall_threshold_ms / 1000.0
        self._check_interval = check_interval_ms / 1000.0
        # Wall-clock of the most recent main-thread tick. The
        # watchdog thread reads this without a lock (single 8-byte
        # float load is atomic in CPython) and compares against now.
        self._last_tick: float = time.monotonic()
        self._tick = QTimer(self)
        self._tick.setInterval(tick_interval_ms)
        self._tick.timeout.connect(self._on_tick)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Tracks whether we've already dumped for the current stall;
        # reset when the main thread ticks again. Without this, a
        # 5-second freeze logs ~20 dumps -- one per check interval.
        self._dumped_for_current_stall = False
        # The file faulthandler writes to. We want the dumps to land
        # in meeting_notetaker.log alongside everything else, so we
        # open it in append mode and let faulthandler write directly.
        # If unavailable, fall back to stderr.
        self._dump_target: Optional[io.IOBase] = None
        if log_file_path is not None:
            try:
                self._dump_target = open(  # noqa: SIM115
                    str(log_file_path), "a", encoding="utf-8",
                )
            except OSError:
                log.exception(
                    "MainLoopWatchdog: could not open %s for stack dumps",
                    log_file_path,
                )

    def start(self) -> None:
        if self._thread is not None:
            return
        try:
            faulthandler.enable(file=self._dump_target or None, all_threads=True)
        except Exception:
            log.exception("MainLoopWatchdog: faulthandler.enable failed")
        self._tick.start()
        self._thread = threading.Thread(
            target=self._watchdog_loop,
            name="MainLoopWatchdog",
            daemon=True,
        )
        self._thread.start()
        log.info(
            "MainLoopWatchdog started (threshold=%.0fms, check=%.0fms)",
            self._stall_threshold * 1000.0,
            self._check_interval * 1000.0,
        )

    def stop(self) -> None:
        self._stop_event.set()
        self._tick.stop()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._dump_target is not None:
            try:
                self._dump_target.close()
            except OSError:
                pass
            self._dump_target = None

    def _on_tick(self) -> None:
        self._last_tick = time.monotonic()
        # Resetting the dump flag here means: as soon as the main
        # loop becomes responsive again, the next stall will be
        # captured. The single dump per stall window is enough -- if
        # the stall keeps recurring with brief recoveries in between,
        # each one will get its own dump.
        self._dumped_for_current_stall = False

    def _watchdog_loop(self) -> None:
        while not self._stop_event.wait(self._check_interval):
            gap = time.monotonic() - self._last_tick
            if gap >= self._stall_threshold and not self._dumped_for_current_stall:
                self._dumped_for_current_stall = True
                self._dump_stacks(gap)

    def _dump_stacks(self, gap_seconds: float) -> None:
        # Log a human-readable marker first so the dump is easy to
        # find in the file later.
        log.warning(
            "MainLoopWatchdog: event loop stalled %.0f ms (threshold %.0f ms); "
            "dumping all thread stacks",
            gap_seconds * 1000.0, self._stall_threshold * 1000.0,
        )
        target = self._dump_target
        if target is not None:
            try:
                target.write(
                    f"\n--- MainLoopWatchdog stall {gap_seconds * 1000.0:.0f} ms"
                    f" at monotonic={time.monotonic():.3f} ---\n",
                )
                target.flush()
            except OSError:
                log.exception("MainLoopWatchdog: write to dump target failed")
        try:
            faulthandler.dump_traceback(file=target or None, all_threads=True)
        except Exception:
            log.exception("MainLoopWatchdog: faulthandler.dump_traceback failed")
        if target is not None:
            try:
                target.flush()
            except OSError:
                pass
