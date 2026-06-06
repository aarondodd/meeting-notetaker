"""Help > About dialog.

Standard "about this app" surface: name, version, short description,
attribution, repo + license links, plus a scrollable third-party
attributions panel listing every open-source project the app
incorporates. Read-only, modal, dismiss with OK or Esc.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..version import __version__


_REPO_URL = "https://github.com/aarondodd/meeting-notetaker"


# Third-party projects + models the app incorporates. Each entry:
# (display_name, project_url, short_purpose, license_tag). Grouped
# for readability in the rendered list; the group headers are
# rendered as inline subheadings.
#
# Update this list when adding or dropping a runtime dependency.
# The corresponding pip names + version pins live in
# requirements.txt; this table is the user-facing surface.
_THIRD_PARTY_GROUPS: list[tuple[str, list[tuple[str, str, str, str]]]] = [
    ("Audio capture + transcription", [
        ("PyAudio", "https://people.csail.mit.edu/hubert/pyaudio/",
         "Cross-platform audio I/O via PortAudio", "MIT"),
        ("PyAudioWPatch", "https://github.com/s0d3s/PyAudioWPatch",
         "Windows WASAPI loopback fork of PyAudio", "MIT"),
        ("faster-whisper", "https://github.com/SYSTRAN/faster-whisper",
         "On-device speech-to-text via CTranslate2", "MIT"),
        ("OpenAI Whisper", "https://github.com/openai/whisper",
         "Speech recognition model (small.en / medium.en weights)", "MIT"),
        ("PyAV", "https://pyav.org/",
         "FFmpeg / libav bindings for audio decoding + Opus encoding", "BSD-3-Clause"),
        ("webrtcvad", "https://github.com/wiseman/py-webrtcvad",
         "Google WebRTC voice activity detection", "BSD-3-Clause"),
        ("sounddevice", "https://python-sounddevice.readthedocs.io/",
         "PortAudio bindings used for device enumeration", "MIT"),
    ]),
    ("Speaker identification", [
        ("SpeechBrain", "https://speechbrain.github.io/",
         "Speech AI toolkit (ECAPA-TDNN inference pipeline)", "Apache-2.0"),
        ("ECAPA-TDNN (spkrec-ecapa-voxceleb)",
         "https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb",
         "Speaker-embedding model trained on VoxCeleb", "Apache-2.0"),
        ("silero-vad", "https://github.com/snakers4/silero-vad",
         "Voice-activity model for diarization turn segmentation", "MIT"),
        ("PyTorch", "https://pytorch.org/",
         "Tensor + autograd runtime", "BSD-3-Clause"),
        ("torchaudio", "https://pytorch.org/audio/",
         "Audio I/O backend for SpeechBrain", "BSD-2-Clause"),
        ("SciPy", "https://scipy.org/",
         "Hierarchical clustering for speaker grouping", "BSD-3-Clause"),
    ]),
    ("UI framework", [
        ("PyQt6", "https://www.riverbankcomputing.com/software/pyqt/",
         "Python bindings for Qt 6", "GPL-3.0-or-later / commercial"),
        ("Qt", "https://www.qt.io/",
         "Cross-platform widget toolkit", "LGPL-3.0 / commercial"),
    ]),
    ("Notes + Markdown", [
        ("markdownify", "https://github.com/matthewwithanm/python-markdownify",
         "Convert clipboard HTML to Markdown on paste", "MIT"),
        ("Beautiful Soup", "https://www.crummy.com/software/BeautifulSoup/",
         "HTML parser used by markdownify", "MIT"),
        ("mistune", "https://github.com/lepture/mistune",
         "Markdown AST for Notion + Confluence export", "BSD-3-Clause"),
        ("python-docx", "https://python-docx.readthedocs.io/",
         "Read Teams .docx transcript exports (optional)", "MIT"),
    ]),
    ("Integrations + networking", [
        ("requests", "https://requests.readthedocs.io/",
         "HTTP client for Notion + Confluence APIs", "Apache-2.0"),
        ("huggingface_hub", "https://huggingface.co/docs/huggingface_hub/",
         "Model download client for ECAPA + Whisper weights", "Apache-2.0"),
        ("truststore", "https://github.com/sethmlarson/truststore",
         "OS certificate store integration for corporate MITM proxies",
         "MIT"),
    ]),
    ("Screen capture + media", [
        ("mss", "https://python-mss.readthedocs.io/",
         "Cross-platform screen capture (Windows GDI BitBlt)", "MIT"),
        ("Pillow", "https://python-pillow.org/",
         "Image encoding for retained screenshots", "MIT-CMU (HPND)"),
        ("NumPy", "https://numpy.org/",
         "Array math for audio resample + image dedup", "BSD-3-Clause"),
    ]),
    ("Windows integration", [
        ("pywin32", "https://github.com/mhammond/pywin32",
         "COM bindings for the Outlook calendar watcher", "PSF-2.0"),
        ("pycaw", "https://github.com/AndreMiras/pycaw",
         "Audio session enumeration for active-meeting detection", "MIT"),
        ("psutil", "https://github.com/giampaolo/psutil",
         "Process-name lookup for the audio detector", "BSD-3-Clause"),
    ]),
    ("Utilities + packaging", [
        ("pyperclip", "https://github.com/asweigart/pyperclip",
         "Clipboard fallback when Qt's clipboard isn't usable", "BSD-3-Clause"),
        ("tomli", "https://github.com/hukkin/tomli",
         "TOML reader for Python 3.10 (stdlib on 3.11+)", "MIT"),
        ("PyInstaller", "https://pyinstaller.org/",
         "Packages the app + dependencies into the Windows .exe", "GPL-2.0-with-classpath"),
        ("Inno Setup", "https://jrsoftware.org/isinfo.php",
         "Builds the Windows installer (.exe)", "Modified BSD"),
    ]),
]


def _render_third_party_html() -> str:
    """Build the QTextBrowser HTML body listing every project +
    license tag, grouped by category. Pure-function helper so the
    rendering can be tested without instantiating the dialog."""
    parts: list[str] = [
        "<p>This software incorporates the following open-source "
        "projects and models:</p>",
    ]
    for group_name, entries in _THIRD_PARTY_GROUPS:
        parts.append(
            f"<p><b>{group_name}</b></p><ul>"
        )
        for name, url, purpose, license_tag in entries:
            parts.append(
                f"<li><a href=\"{url}\">{name}</a> "
                f"&mdash; {purpose} "
                f"<i>({license_tag})</i></li>"
            )
        parts.append("</ul>")
    parts.append(
        "<p>Full license texts ship in the LICENSES directory of "
        "the source repository; each project's license is also "
        "available via its linked homepage above.</p>"
    )
    return "".join(parts)


class AboutDialog(QDialog):
    """Modal About dialog. Shows app metadata + attribution."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About Meeting Notetaker")
        self.setModal(True)
        self.setMinimumWidth(480)
        self.resize(560, 620)

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
            "<b>Coded by</b> Aaron Dodd using <a href=\"https://www.anthropic.com/"
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

        # Third-party attributions panel. Read-only QTextBrowser so
        # the project homepage links are clickable; sized so the
        # panel is clearly scrollable without crowding the rest of
        # the dialog. Open-source components shipping inside the
        # installer deserve a visible attribution surface, not just
        # an entry in a LICENSES file the user has to hunt for.
        attribution_heading = QLabel(
            "<b>Open-source components</b>", self,
        )
        attribution_heading.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(attribution_heading)

        self._attributions_view = QTextBrowser(self)
        self._attributions_view.setOpenExternalLinks(True)
        self._attributions_view.setHtml(_render_third_party_html())
        self._attributions_view.setMinimumHeight(180)
        layout.addWidget(self._attributions_view, 1)

        # OK / dismiss button.
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok, parent=self,
        )
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
