"""Help > About dialog.

Standard "about this app" surface: name, version, short description,
attribution, repo + license links. Read-only, modal, dismiss with
OK or Esc.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ..version import __version__


_REPO_URL = "https://github.com/aarondodd/meeting-notetaker"


class AboutDialog(QDialog):
    """Modal About dialog. Shows app metadata + attribution."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About Meeting Notetaker")
        self.setModal(True)
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel("<h2>Meeting Notetaker</h2>", self)
        title.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(title)

        version = QLabel(f"<b>Version:</b> {__version__}", self)
        version.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(version)

        description = QLabel(
            "Local meeting capture for Windows. Records mic + system "
            "audio, transcribes on-device with faster-whisper, captures "
            "screen regions, plays the recording back with transcript "
            "sync, and hands the transcript to your LLM of choice for "
            "synthesis. No audio leaves the machine; no API key required.",
            self,
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        attribution = QLabel(
            "<b>Vibe coded</b> by Aaron Dodd using <a href=\"https://www.anthropic.com/"
            "claude-code\">Claude Code</a>. ",
            self,
        )
        attribution.setWordWrap(True)
        attribution.setTextFormat(Qt.TextFormat.RichText)
        attribution.setOpenExternalLinks(True)
        layout.addWidget(attribution)

        repo = QLabel(
            f"<b>Source:</b> <a href=\"{_REPO_URL}\">{_REPO_URL}</a>",
            self,
        )
        repo.setTextFormat(Qt.TextFormat.RichText)
        repo.setOpenExternalLinks(True)
        repo.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )
        layout.addWidget(repo)

        license_label = QLabel(
            "<b>License:</b> MIT. See "
            f"<a href=\"{_REPO_URL}/blob/main/LICENSE\">LICENSE</a> in "
            "the repository.",
            self,
        )
        license_label.setTextFormat(Qt.TextFormat.RichText)
        license_label.setOpenExternalLinks(True)
        layout.addWidget(license_label)

        # OK / dismiss button.
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok, parent=self,
        )
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
