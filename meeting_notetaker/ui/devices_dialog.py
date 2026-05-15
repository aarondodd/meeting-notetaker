"""Audio-devices diagnostic dialog.

Shows exactly what PyAudio sees, including 0-input devices and the host API
table. Surfaces a Microsoft-Store-Python warning at the top when relevant,
since that is the most common root cause of 'No input devices found' on
Windows 11.
"""
from __future__ import annotations

import logging
import sys
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)


log = logging.getLogger(__name__)


def _format_report() -> str:
    lines: list[str] = []
    lines.append(f"Python:     {sys.executable}")
    lines.append(f"Platform:   {sys.platform}")

    try:
        from ..audio.mic_recorder import _is_store_python
        if sys.platform.startswith("win") and _is_store_python():
            lines.append("")
            lines.append("WARNING: Microsoft Store Python detected.")
            lines.append(
                "  The Store Python package runs inside a UWP AppContainer that"
            )
            lines.append("  blocks microphone access at the OS level. Install Python from")
            lines.append("  https://www.python.org/downloads/ and rebuild the venv to fix.")
    except Exception:
        pass

    lines.append("")
    lines.append("--- Audio Hosts (PyAudio) ---")
    try:
        import pyaudio
        from ..audio.mic_recorder import list_all_devices
        pa = pyaudio.PyAudio()
        try:
            try:
                default_in = pa.get_default_input_device_info()
                lines.append(f"Default input: #{default_in.get('index')} {default_in.get('name')}")
            except Exception as exc:
                lines.append(f"Default input: <unavailable: {exc}>")
            try:
                default_out = pa.get_default_output_device_info()
                lines.append(f"Default output: #{default_out.get('index')} {default_out.get('name')}")
            except Exception as exc:
                lines.append(f"Default output: <unavailable: {exc}>")

            lines.append("")
            lines.append("Host APIs:")
            for i in range(pa.get_host_api_count()):
                api = pa.get_host_api_info_by_index(i)
                lines.append(
                    f"  #{i}  {api.get('name')}  type={api.get('type')}  "
                    f"devices={api.get('deviceCount')}  "
                    f"defaultIn={api.get('defaultInputDevice')}  "
                    f"defaultOut={api.get('defaultOutputDevice')}"
                )

            lines.append("")
            lines.append("Devices:")
            devices = list_all_devices(pa)
            if not devices:
                lines.append("  (none)")
            for d in devices:
                lines.append(
                    f"  #{d.get('_index')}  {d.get('name', '?')}  hostApi={d.get('hostApi')}  "
                    f"maxIn={d.get('maxInputChannels')}  maxOut={d.get('maxOutputChannels')}  "
                    f"rate={d.get('defaultSampleRate')}"
                )
        finally:
            pa.terminate()
    except ImportError as exc:
        lines.append(f"(PyAudio not installed: {exc})")
    except Exception as exc:
        log.exception("audio diagnostic failed")
        lines.append(f"(PyAudio diagnostic failed: {exc})")

    # Loopback (WASAPI) diagnostic, Windows only.
    if sys.platform.startswith("win"):
        lines.append("")
        lines.append("--- WASAPI Loopback (PyAudioWPatch) ---")
        try:
            from ..audio.loopback_recorder import LoopbackRecorder
            if not LoopbackRecorder.is_available():
                lines.append("PyAudioWPatch not installed -- system-audio capture unavailable.")
            else:
                device = LoopbackRecorder.find_loopback_device()
                if device is None:
                    lines.append("No WASAPI loopback device found for the system default output.")
                else:
                    lines.append(
                        f"Loopback device: #{device.get('index')} {device.get('name')}  "
                        f"rate={device.get('defaultSampleRate')}  "
                        f"ch={device.get('maxInputChannels')}"
                    )
        except Exception as exc:
            log.exception("loopback diagnostic failed")
            lines.append(f"Loopback diagnostic failed: {exc}")

    return "\n".join(lines)


class DevicesDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Audio Devices")
        self.setModal(True)
        self.resize(720, 520)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Snapshot of what PyAudio + PyAudioWPatch see on this machine. "
            "Useful for diagnosing 'no mic' / 'no loopback' problems.",
            self,
        ))

        self._view = QPlainTextEdit(self)
        self._view.setReadOnly(True)
        self._view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self._view.setFont(mono)
        self._view.setPlainText(_format_report())
        layout.addWidget(self._view, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
