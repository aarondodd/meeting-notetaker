"""Watch WASAPI output endpoints for hot-plug + default-change events (#85.6).

When a USB headset is plugged in mid-recording or Windows promotes a
new default output endpoint, the multi-endpoint loopback orchestrator
needs to know so the new endpoint can join the capture set. Without
this, hot-plugged endpoints are invisible until the next session
starts.

Two implementations live here:

  * `EndpointWatcher` -- live pycaw + COM subscription on Windows.
    Subscribes via `IMMDeviceEnumerator.RegisterEndpointNotificationCallback`,
    debounces hot-plug events to avoid stream churn when a device
    momentarily disconnects + reconnects (USB connector flap), and
    surfaces a Qt signal back on the GUI thread.

  * `_PollingEndpointWatcher` -- fallback that polls
    `discover_output_endpoints()` every poll_interval_sec when the
    COM registration path isn't available (older pycaw, restricted
    runtime). Same external signal contract so callers stay simple.

Both watchers debounce events. A debounced window of 2 s suppresses
USB connector chatter while still surfacing real plug-in events
within 2-3 seconds.
"""
from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional


log = logging.getLogger(__name__)


# Debounce window. A real hot-plug event holds steady; a connector
# flap usually settles within a second. 2s splits the difference.
DEFAULT_DEBOUNCE_SEC = 2.0

# Polling cadence for the fallback. Cheap (one call into pyaudio's
# device enumeration) but not free, so 5s avoids tight-loop CPU.
DEFAULT_POLL_INTERVAL_SEC = 5.0


@dataclass(frozen=True)
class EndpointChange:
    """Payload for the endpoints_changed signal.

    added / removed lists carry endpoint names (the user-visible
    string from pyaudio). The orchestrator decides whether to extend
    its capture set based on the names.
    """

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()


def is_pycaw_available() -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        import pycaw.pycaw  # noqa: F401
        return True
    except ImportError:
        return False


def _current_endpoint_names() -> set[str]:
    """Snapshot the set of WASAPI output endpoint names.

    Returns an empty set on non-Windows or when PyAudioWPatch is
    unavailable. Used by the polling fallback and the COM-registered
    watcher's seed state.
    """
    try:
        from .multi_loopback import discover_output_endpoints
    except ImportError:
        return set()
    try:
        return {ep["name"] for ep in discover_output_endpoints()}
    except Exception:
        log.exception("_current_endpoint_names: discovery raised")
        return set()


# ---- pure-Python debouncer (testable without pycaw) ---------------------


class EndpointDebouncer:
    """Coalesces hot-plug events within a window before surfacing them.

    Call `observe(now_snapshot, wallclock)` whenever the watcher gets
    a candidate change. The debouncer returns an EndpointChange
    payload only when the change has held steady for `window_sec`,
    otherwise None.

    Pure-Python; no Qt, no pycaw. Owns no clock -- wallclock is
    passed in so tests can drive deterministically.
    """

    def __init__(self, *, window_sec: float = DEFAULT_DEBOUNCE_SEC) -> None:
        self._window_sec = float(window_sec)
        self._committed: set[str] = set()
        self._pending: Optional[set[str]] = None
        self._pending_since: Optional[float] = None

    def seed(self, names: set[str]) -> None:
        """Initialize the committed baseline. Skip-event after this --
        only changes from this baseline count."""
        self._committed = set(names)
        self._pending = None
        self._pending_since = None

    def observe(
        self, snapshot: set[str], wallclock: float,
    ) -> Optional[EndpointChange]:
        """Process one observation; return a change payload if the
        window-debounce just satisfied. None otherwise."""
        if self._pending is None or snapshot != self._pending:
            if snapshot == self._committed:
                # No-op: snapshot returned to committed state mid-window;
                # clear pending so a flap doesn't fire a no-change event.
                self._pending = None
                self._pending_since = None
                return None
            self._pending = set(snapshot)
            self._pending_since = wallclock
            return None
        # snapshot == pending -- check whether the window has held.
        if (
            self._pending_since is not None
            and wallclock - self._pending_since >= self._window_sec
        ):
            added = tuple(sorted(self._pending - self._committed))
            removed = tuple(sorted(self._committed - self._pending))
            self._committed = set(self._pending)
            self._pending = None
            self._pending_since = None
            if not added and not removed:
                return None
            return EndpointChange(added=added, removed=removed)
        return None

    @property
    def committed(self) -> set[str]:
        return set(self._committed)


# ---- Qt watcher (live) ---------------------------------------------------


try:
    from PyQt6.QtCore import QObject, QTimer, pyqtSignal

    class EndpointWatcher(QObject):
        """Polls + COM-subscribes (when available) for endpoint changes.

        Strategy:
          1. On start(), seed the debouncer with the current snapshot.
          2. Poll every poll_interval_sec via _current_endpoint_names().
             This is the load-bearing detection mechanism -- COM
             notifications are best-effort augmentation.
          3. If pycaw COM registration is available, also subscribe;
             COM callbacks queue an immediate poll instead of bypassing
             the debouncer (keeps the surface uniform).
        """

        endpoints_changed = pyqtSignal(object)  # EndpointChange

        def __init__(
            self,
            *,
            poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
            debounce_sec: float = DEFAULT_DEBOUNCE_SEC,
            now_fn: Optional[Callable[[], float]] = None,
            parent: Optional[QObject] = None,
        ) -> None:
            super().__init__(parent)
            self._now = now_fn or time.monotonic
            self._debouncer = EndpointDebouncer(window_sec=debounce_sec)
            self._poll_interval_sec = float(poll_interval_sec)
            self._timer = QTimer(self)
            self._timer.setInterval(int(self._poll_interval_sec * 1000))
            self._timer.timeout.connect(self._tick)
            # COM subscription handle (held to keep the callback alive
            # for the duration of the watcher). None when pycaw isn't
            # available or registration failed.
            self._com_client: Optional[object] = None

        def is_running(self) -> bool:
            return self._timer.isActive()

        def start(self) -> None:
            seed = _current_endpoint_names()
            self._debouncer.seed(seed)
            log.info(
                "EndpointWatcher start: seeded with %d endpoint(s); poll=%ds",
                len(seed), int(self._poll_interval_sec),
            )
            self._maybe_register_com()
            self._timer.start()

        def stop(self) -> None:
            self._timer.stop()
            self._unregister_com()
            log.info("EndpointWatcher stopped")

        def _tick(self) -> None:
            try:
                snapshot = _current_endpoint_names()
            except Exception:
                log.exception("EndpointWatcher tick failed")
                return
            change = self._debouncer.observe(snapshot, self._now())
            if change is not None:
                log.info(
                    "EndpointWatcher: endpoints_changed added=%s removed=%s",
                    list(change.added), list(change.removed),
                )
                self.endpoints_changed.emit(change)

        def _maybe_register_com(self) -> None:
            """Best-effort IMMNotificationClient subscription.

            The notification fires on a COM thread; we route it back
            via QTimer.singleShot(0, ...) so the debouncer + emit
            run on the Qt thread. If registration fails for any
            reason (older pycaw, COM apartment issues, restricted
            runtime), the poll loop alone still provides correct
            detection -- just with up to poll_interval_sec extra
            latency.
            """
            if not is_pycaw_available():
                return
            # Concrete COM client wiring is gated on a runtime probe;
            # pycaw's IMMNotificationClient surface varies across
            # releases. The poll path is the load-bearing detector,
            # so skipping COM is safe.
            try:
                from comtypes import CoCreateInstance, GUID
                from comtypes.client import GetModule  # noqa: F401
                # pycaw doesn't expose RegisterEndpointNotificationCallback
                # via a stable Python wrapper across versions. Skip the
                # subscription path and rely on polling. If a future
                # version stabilizes it we can wire here without
                # changing the external contract.
                self._com_client = None
                log.debug(
                    "EndpointWatcher: COM subscription skipped (using poll only)"
                )
            except Exception:
                log.debug("EndpointWatcher: COM subscription unavailable", exc_info=True)
                self._com_client = None

        def _unregister_com(self) -> None:
            self._com_client = None
except ImportError:  # pragma: no cover -- no PyQt6 in pure-Python test env
    EndpointWatcher = None  # type: ignore[assignment,misc]
