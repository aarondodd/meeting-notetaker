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

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._state = "idle"
        self._tray = QSystemTrayIcon(make_state_icon("idle"), parent)
        self._tray.setToolTip("Meeting Notetaker (idle)")
        self._menu = QMenu()
        self._build_menu()
        self._tray.setContextMenu(self._menu)
        self._tray.activated.connect(self._on_activated)
        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(700)
        self._pulse_timer.timeout.connect(self._pulse_step)
        self._pulse_phase = False
        self._tray.show()

    def _build_menu(self) -> None:
        self._action_open = QAction("Open Meeting Notetaker", self)
        self._action_open.triggered.connect(self.open_main_window.emit)
        self._menu.addAction(self._action_open)

        self._menu.addSeparator()

        self._action_new = QAction("New Session...", self)
        self._action_new.triggered.connect(self.new_session_requested.emit)
        self._menu.addAction(self._action_new)

        self._action_pause = QAction("Pause Recording", self)
        self._action_pause.triggered.connect(self.pause_requested.emit)
        self._action_pause.setEnabled(False)
        self._menu.addAction(self._action_pause)

        self._action_resume = QAction("Resume Recording", self)
        self._action_resume.triggered.connect(self.resume_requested.emit)
        self._action_resume.setEnabled(False)
        self._menu.addAction(self._action_resume)

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
        # Enable/disable transport actions to match.
        recording = state == "recording"
        paused = state == "paused"
        self._action_pause.setEnabled(recording)
        self._action_resume.setEnabled(paused)
        self._action_stop.setEnabled(recording or paused)

    def _pulse_step(self) -> None:
        self._pulse_phase = not self._pulse_phase
        # Alternate between "recording" and "ready" colours for the pulse.
        self._tray.setIcon(make_state_icon("recording" if self._pulse_phase else "ready"))

    @property
    def state(self) -> str:
        return self._state
