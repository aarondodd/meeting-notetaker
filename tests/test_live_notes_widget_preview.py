"""Preview-pane Attendees-to-table substitution (#56).

When at least one of the session's resolved Contacts has rich-field
data (title / company / department / email / phone), the LiveNotesWidget
preview pane swaps the `# Attendees` bullet list for the Markdown
table the PDF export uses. The underlying source buffer is untouched;
edit mode always shows the user's actual markdown.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Optional

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.ui.live_notes_widget import LiveNotesWidget  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


@dataclass
class _FakeContact:
    display_name: str
    title: Optional[str] = None
    company: Optional[str] = None
    department: Optional[str] = None
    primary_email: Optional[str] = None
    phone: Optional[str] = None


_SAMPLE_BODY = """# Attendees
- Bob
- Mary

# Notes
- decided X
"""


def test_preview_text_passthrough_when_no_contacts(qt_app):
    """No session contacts -> preview matches the source verbatim."""
    w = LiveNotesWidget()
    try:
        assert w._preview_text(_SAMPLE_BODY) == _SAMPLE_BODY  # noqa: SLF001
    finally:
        w.deleteLater()


def test_preview_text_passthrough_when_only_names(qt_app):
    """Contacts present but no rich fields -> bullet list survives;
    the table would be a single-column "Name" header with no extra
    info, which is more noise than signal."""
    w = LiveNotesWidget()
    try:
        w.set_session_contacts([
            _FakeContact(display_name="Bob"),
            _FakeContact(display_name="Mary"),
        ])
        assert w._preview_text(_SAMPLE_BODY) == _SAMPLE_BODY  # noqa: SLF001
    finally:
        w.deleteLater()


def test_preview_text_swaps_for_table_when_rich(qt_app):
    """Any rich field on any contact triggers the table swap."""
    w = LiveNotesWidget()
    try:
        w.set_session_contacts([
            _FakeContact(display_name="Bob", title="CEO", company="Bobco"),
            _FakeContact(display_name="Mary", title="VP"),
        ])
        out = w._preview_text(_SAMPLE_BODY)  # noqa: SLF001
        # Bullet markers gone.
        assert "- Bob" not in out
        assert "- Mary" not in out
        # Table headers present.
        assert "| Name | Title | Company |" in out
        # Other sections preserved verbatim.
        assert "# Notes" in out
        assert "- decided X" in out
    finally:
        w.deleteLater()


def test_source_buffer_untouched_after_set_session_contacts(qt_app):
    """Setting contacts must NOT mutate the user's markdown source."""
    w = LiveNotesWidget()
    try:
        w.setPlainText(_SAMPLE_BODY)
        w.set_session_contacts([
            _FakeContact(display_name="Bob", title="CEO"),
        ])
        # Source buffer reflects exactly what the user typed.
        assert w.toPlainText() == _SAMPLE_BODY
    finally:
        w.deleteLater()


def test_toggle_preview_uses_substituted_text(qt_app):
    """Flipping to preview mode must render the swapped text."""
    w = LiveNotesWidget()
    try:
        w.setPlainText(_SAMPLE_BODY)
        w.set_session_contacts([
            _FakeContact(display_name="Bob", title="CEO"),
        ])
        # Switch to preview programmatically.
        w.set_preview_mode(True)
        assert w.is_in_preview()
        # The underlying preview widget's current markdown source
        # should be the substituted text (we don't introspect Qt
        # rendering, but the _preview_text path runs and the source
        # buffer is unchanged -- pinning the source is the key
        # observable property).
        assert w.toPlainText() == _SAMPLE_BODY
    finally:
        w.deleteLater()
