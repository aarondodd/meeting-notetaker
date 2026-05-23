"""System tray icon with state-driven coloring + pulse during recording."""
from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

from ..utils.icons import make_state_icon


class TrayIcon(QObject):
    """Wrapper around QSystemTrayIcon. Owns its menu + state animation."""

    open_main_window = pyqtSignal()
    new_session_requested = pyqtSignal()
    pause_requested = pyqtSignal()
    resume_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    settings_requested = pyqtSignal()
    quit_requested = pyqtSignal()
    # Fires when the user clicks the most-recent meeting notification toast.
    # Payload: the MeetingInfo passed to notify_meeting().
    meeting_notification_clicked = pyqtSignal(object)
    # Fires when the user clicks the most-recent ad-hoc audio-detected toast.
    # Payload: the MeetingAudioInfo passed to notify_audio_detected().
    audio_notification_clicked = pyqtSignal(object)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._state = "idle"
        self._tray = QSystemTrayIcon(make_state_icon("idle"), parent)
        self._tray.setToolTip("Meeting Notetaker (idle)")
        self._menu = QMenu()
        self._build_menu()
        self._tray.setContextMenu(self._menu)
        self._tray.activated.connect(self._on_activated)
        self._tray.messageClicked.connect(self._on_message_clicked)
        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(700)
        self._pulse_timer.timeout.connect(self._pulse_step)
        self._pulse_phase = False
        # Each notify_* call registers a callable here; clicking the most-
        # recent toast invokes it. Cleared after dispatch so a stale click
        # cannot re-fire later.
        self._pending_click: Optional[Callable[[], None]] = None
        self._tray.show()

    def _build_menu(self) -> None:
        self._action_open = QAction("Open Meeting Notetaker", self)
        self._action_open.triggered.connect(self.open_main_window.emit)
        self._menu.addAction(self._action_open)

        self._menu.addSeparator()

        self._action_new = QAction("New Session...", self)
        self._action_new.triggered.connect(self.new_session_requested.emit)
        self._menu.addAction(self._action_new)

        # Pause / Resume tray actions removed in v0.6.5 alongside
        # the SessionView buttons -- the recording is now a fixed
        # Start -> Stop block (avoids wall-clock / mic / sys
        # alignment drift). Stop is the only mid-recording action.

        self._action_stop = QAction("Stop Recording", self)
        self._action_stop.triggered.connect(self.stop_requested.emit)
        self._action_stop.setEnabled(False)
        self._menu.addAction(self._action_stop)

        self._menu.addSeparator()

        self._action_settings = QAction("Settings...", self)
        self._action_settings.triggered.connect(self.settings_requested.emit)
        self._menu.addAction(self._action_settings)

        self._menu.addSeparator()

        self._action_quit = QAction("Quit", self)
        self._action_quit.triggered.connect(self.quit_requested.emit)
        self._menu.addAction(self._action_quit)

    def _on_activated(self, reason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.open_main_window.emit()

    def set_state(self, state: str, tooltip: Optional[str] = None) -> None:
        self._state = state
        self._tray.setIcon(make_state_icon(state))
        self._tray.setToolTip(tooltip or f"Meeting Notetaker ({state})")
        # Pulse the icon during recording for visual feedback.
        if state == "recording":
            self._pulse_phase = False
            self._pulse_timer.start()
        else:
            self._pulse_timer.stop()
        # Stop is the only transport action; enable while recording
        # or (for legacy sessions) paused.
        recording = state == "recording"
        paused = state == "paused"
        self._action_stop.setEnabled(recording or paused)

    def _pulse_step(self) -> None:
        self._pulse_phase = not self._pulse_phase
        # Alternate between "recording" and "ready" colours for the pulse.
        self._tray.setIcon(make_state_icon("recording" if self._pulse_phase else "ready"))

    @property
    def state(self) -> str:
        return self._state

    def notify_meeting(self, info, *, title: str, body: str, timeout_ms: int = 15000) -> None:
        """Surface an imminent-meeting toast. Click -> meeting_notification_clicked.

        QSystemTrayIcon.showMessage's clicked signal fires per-toast but does
        not carry payload; we stash a click callback on the wrapper and
        emit the right signal when the user clicks. The most-recent toast
        wins -- only one notification can be pending at a time.
        """
        self._pending_click = lambda: self.meeting_notification_clicked.emit(info)
        self._show_message(title, body, timeout_ms)

    def notify_audio_detected(
        self, info, *, title: str, body: str, timeout_ms: int = 15000
    ) -> None:
        """Surface an ad-hoc-meeting-audio toast. Click -> audio_notification_clicked.

        Same single-pending-callback contract as notify_meeting; whichever
        notify_* was called most recently is what a tray click dispatches.
        """
        self._pending_click = lambda: self.audio_notification_clicked.emit(info)
        self._show_message(title, body, timeout_ms)

    def _show_message(self, title: str, body: str, timeout_ms: int) -> None:
        try:
            self._tray.showMessage(
                title,
                body,
                QSystemTrayIcon.MessageIcon.Information,
                int(timeout_ms),
            )
        except Exception:
            # showMessage can fail on platforms without a notification daemon
            # (some Linux WMs); a missed toast is non-fatal -- the user can
            # still launch a session from the tray menu or main window.
            pass

    def _on_message_clicked(self) -> None:
        cb = self._pending_click
        self._pending_click = None
        if cb is not None:
            cb()
