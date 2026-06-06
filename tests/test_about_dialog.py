"""About dialog tests.

Pins the attribution phrasing + the third-party open-source
attributions panel content. Aaron asked for two changes
(2026-06-05): drop "Vibe coded" in favor of "Coded by", and add a
scrollable attributions surface listing every open-source project
the app incorporates.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel, QTextBrowser  # noqa: E402

from meeting_notetaker.ui.about_dialog import (  # noqa: E402
    AboutDialog,
    _THIRD_PARTY_GROUPS,
    _render_third_party_html,
)


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


# ---- attribution phrasing -----------------------------------------------


def test_attribution_uses_coded_by_not_vibe_coded(qt_app):
    """Pin Aaron's phrasing change. "Vibe coded" was the old text;
    "Coded by" is the new phrasing."""
    dlg = AboutDialog()
    try:
        joined = " ".join(
            label.text() for label in dlg.findChildren(QLabel)
        )
        assert "Vibe coded" not in joined
        assert "Coded by" in joined
        # Suffix preserved verbatim.
        assert "Aaron Dodd using" in joined
        assert "Claude Code" in joined
    finally:
        dlg.deleteLater()


# ---- attributions panel structure ---------------------------------------


def test_attributions_panel_exists_as_qtextbrowser(qt_app):
    """The panel is a QTextBrowser so the project homepage links
    are clickable. setOpenExternalLinks is on so clicks route to
    the user's browser via QDesktopServices."""
    dlg = AboutDialog()
    try:
        browsers = dlg.findChildren(QTextBrowser)
        assert len(browsers) == 1
        assert browsers[0].openExternalLinks() is True
        # Sized so the scrollbar appears reliably even when the
        # dialog is at its minimum height.
        assert browsers[0].minimumHeight() >= 150
    finally:
        dlg.deleteLater()


def test_attributions_panel_heading_present(qt_app):
    dlg = AboutDialog()
    try:
        joined = " ".join(
            label.text() for label in dlg.findChildren(QLabel)
        )
        assert "Open-source components" in joined
    finally:
        dlg.deleteLater()


# ---- attributions content ----------------------------------------------


def test_third_party_groups_cover_every_runtime_category():
    """The grouping is the user-visible structure; pin that each
    expected category is present. Catches a typo or an accidental
    drop in a future edit."""
    group_names = {name for name, _ in _THIRD_PARTY_GROUPS}
    assert "Audio capture + transcription" in group_names
    assert "Speaker identification" in group_names
    assert "UI framework" in group_names
    assert "Notes + Markdown" in group_names
    assert "Integrations + networking" in group_names
    assert "Screen capture + media" in group_names
    assert "Windows integration" in group_names
    assert "Utilities + packaging" in group_names


def test_third_party_entries_carry_required_fields():
    """Each entry is (name, url, purpose, license_tag). All four
    must be non-empty -- a missing url or license tag would
    silently render as a broken link / unattributed component."""
    for group_name, entries in _THIRD_PARTY_GROUPS:
        for entry in entries:
            name, url, purpose, license_tag = entry
            assert name.strip(), f"{group_name}: empty project name"
            assert url.startswith(("http://", "https://")), (
                f"{group_name}/{name}: url must be http(s); got {url!r}"
            )
            assert purpose.strip(), f"{group_name}/{name}: empty purpose"
            assert license_tag.strip(), (
                f"{group_name}/{name}: empty license_tag"
            )


def test_third_party_list_includes_headline_runtime_deps():
    """A few critical projects MUST appear by name: the audio +
    transcription path (PyAudio, faster-whisper, OpenAI Whisper),
    the speaker-ID model (ECAPA-TDNN), the UI framework (PyQt6 +
    Qt), and the packaging tooling (PyInstaller, Inno Setup)."""
    all_names = {
        name
        for _, entries in _THIRD_PARTY_GROUPS
        for name, _, _, _ in entries
    }
    for required in (
        "PyAudio", "PyAudioWPatch",
        "faster-whisper", "OpenAI Whisper",
        "PyAV", "webrtcvad",
        "SpeechBrain", "silero-vad", "PyTorch",
        "PyQt6", "Qt",
        "markdownify", "mistune",
        "requests", "huggingface_hub",
        "mss", "Pillow", "NumPy",
        "pywin32", "pycaw", "psutil",
        "PyInstaller", "Inno Setup",
    ):
        assert required in all_names, (
            f"required project {required!r} missing from attributions"
        )


def test_third_party_list_includes_ecapa_model_attribution():
    """The ECAPA-TDNN model weights download from HuggingFace at
    runtime; the model itself deserves its own line (separate from
    the SpeechBrain toolkit entry) since it's the actual artifact
    shipped with the model cache."""
    all_names = {
        name
        for _, entries in _THIRD_PARTY_GROUPS
        for name, _, _, _ in entries
    }
    assert any("ECAPA-TDNN" in n for n in all_names)


# ---- rendered HTML smoke test ------------------------------------------


def test_render_third_party_html_produces_well_formed_body():
    html = _render_third_party_html()
    # Intro sentence present.
    assert "incorporates the following open-source" in html
    # Each group heading rendered as a <b>; ULs around list items.
    assert "<b>Audio capture + transcription</b>" in html
    assert "<ul>" in html and "</ul>" in html
    # Each entry renders the project name + license tag.
    assert "PyQt6" in html
    assert "(GPL-3.0-or-later" in html or "GPL-3.0" in html
    # Anchor tags use href attribute.
    assert "<a href=\"https://" in html
    # Closing trailer.
    assert "LICENSES" in html


def test_render_third_party_html_is_deterministic():
    """Two consecutive renders return byte-identical output. The
    grouping table is module-level static, so this should hold
    trivially -- the test guards against a future drift into a
    dict / set iteration order."""
    assert _render_third_party_html() == _render_third_party_html()
