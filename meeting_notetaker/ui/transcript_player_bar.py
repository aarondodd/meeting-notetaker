"""Compact playback bar for the Transcript pane.

Layout:

  [ Play ] [ time MM:SS / MM:SS ] [---------- scrubber ----------]

Emits play_clicked / pause_clicked when the toggle button fires, and
seek_ms_requested(int_ms) when the user releases the scrubber. The
bar tracks its own visual state (button label, slider position) from
external set_* calls; MainApp owns the AudioPlayer and drives those
methods.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSlider, QWidget


class TranscriptPlayerBar(QWidget):
    """Play/Pause toggle + scrubber + time readout for the Transcript pane."""

    play_clicked = pyqtSignal()
    pause_clicked = pyqtSignal()
    # Fired when the user drags then releases the slider. While
    # dragging, the bar emits nothing -- the AudioPlayer would
    # otherwise restart playback on every pixel of drag.
    seek_ms_requested = pyqtSignal(int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._is_playing = False
        self._total_ms = 0
        # Suppress slider valueChanged echoes while we programmatically
        # update the slider from a position-tick; otherwise the value
        # change re-emits seek_ms_requested and the player flutters.
        self._suppress_slider_signal = False
        # Tri-state: "loading" overrides "--:-- / --:--" so the user
        # sees a positive cue that the decode is in flight (#61). The
        # slider + button stay disabled either way.
        self._loading = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)

        self._play_btn = QPushButton("Play", self)
        self._play_btn.clicked.connect(self._on_toggle)
        layout.addWidget(self._play_btn)

        self._time_label = QLabel("--:-- / --:--", self)
        self._time_label.setMinimumWidth(110)
        layout.addWidget(self._time_label)

        self._slider = QSlider(Qt.Orientation.Horizontal, self)
        self._slider.setMinimum(0)
        self._slider.setMaximum(0)
        self._slider.valueChanged.connect(self._on_slider_value_changed)
        self._slider.sliderReleased.connect(self._on_slider_released)
        layout.addWidget(self._slider, 1)

        self.set_enabled_state(False)

    # ------------------------------------------------------------------
    # Public API

    def set_enabled_state(self, enabled: bool) -> None:
        """Master enable. The bar greys out when no audio is loaded."""
        self._play_btn.setEnabled(enabled)
        self._slider.setEnabled(enabled)
        if not enabled:
            self.set_total_ms(0)
            self.set_position_ms(0)
            self.set_is_playing(False)
        else:
            # Clear any lingering "Loading audio..." label once
            # playback is actually ready.
            self._loading = False
            self._refresh_time_label(self._slider.value())

    def set_loading_state(self, loading: bool) -> None:
        """Visual cue that the decode worker is in flight (#61).

        Controls stay disabled (no playback action is valid mid-
        decode) but the time label switches from the broken-looking
        \"--:-- / --:--\" to \"Loading audio...\" so the user knows
        the bar isn't broken.
        """
        self._loading = bool(loading)
        if self._loading:
            self._play_btn.setEnabled(False)
            self._slider.setEnabled(False)
            self._time_label.setText("Loading audio...")
        else:
            self._refresh_time_label(self._slider.value())

    def set_total_ms(self, total_ms: int) -> None:
        self._total_ms = max(0, int(total_ms))
        # Slider drives in milliseconds; max int is 2^31-1 so any
        # realistic meeting fits with room to spare.
        self._slider.setMaximum(self._total_ms)
        self._refresh_time_label(self._slider.value())

    def set_position_ms(self, ms: int) -> None:
        """Push a new position into the slider + time label.

        Used by MainApp on every position_changed tick. While the
        user is dragging the slider, leave their position alone --
        otherwise the playhead snaps back to the player position
        every 100ms and the drag feels broken.
        """
        if self._slider.isSliderDown():
            return
        self._suppress_slider_signal = True
        try:
            self._slider.setValue(int(ms))
        finally:
            self._suppress_slider_signal = False
        self._refresh_time_label(int(ms))

    def set_is_playing(self, playing: bool) -> None:
        """Flip the toggle button label.

        Labels "Play" / "Stop" rather than "Play" / "Pause" -- there's
        no separate Resume affordance, so a Stop press keeps the
        playhead where it is and the next Play picks up from the same
        spot. Aaron preferred Stop as the clearer verb.
        """
        self._is_playing = bool(playing)
        self._play_btn.setText("Stop" if self._is_playing else "Play")

    def is_user_dragging(self) -> bool:
        return self._slider.isSliderDown()

    # ------------------------------------------------------------------
    # Internal slot handlers

    def _on_toggle(self) -> None:
        if self._is_playing:
            self.pause_clicked.emit()
        else:
            self.play_clicked.emit()

    def _on_slider_value_changed(self, value: int) -> None:
        if self._suppress_slider_signal:
            return
        # Keep the time label live during drag so the readout matches
        # the slider thumb. The seek itself happens on release.
        self._refresh_time_label(value)

    def _on_slider_released(self) -> None:
        self.seek_ms_requested.emit(int(self._slider.value()))

    def _refresh_time_label(self, position_ms: int) -> None:
        if self._loading:
            # Don't clobber the loading cue with the "--:-- / --:--"
            # placeholder when an internal position update fires
            # during decode.
            return
        if self._total_ms <= 0:
            self._time_label.setText("--:-- / --:--")
            return
        self._time_label.setText(
            f"{_fmt_ms(position_ms)} / {_fmt_ms(self._total_ms)}"
        )


def _fmt_ms(ms: int) -> str:
    """Format milliseconds as M:SS or H:MM:SS for longer meetings."""
    total_sec = max(0, ms) // 1000
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    seconds = total_sec % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"
