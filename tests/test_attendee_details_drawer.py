"""AttendeeDetailsDrawer (issue #51 Phase 3).

Widget-level tests for the collapsible attendee-details drawer
mounted above the My Notes + Synthesis editors. Headless via
QT_QPA_PLATFORM=offscreen.
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

from meeting_notetaker.ui.attendee_details_drawer import AttendeeDetailsDrawer  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


@dataclass
class _FakeContact:
    """Minimal Contact stand-in for drawer tests; the drawer only
    reads display_name + rich-field attrs + per-field source attrs +
    the last_enriched_source rollup."""
    id: int
    display_name: str
    title: Optional[str] = None
    company: Optional[str] = None
    department: Optional[str] = None
    primary_email: Optional[str] = None
    phone: Optional[str] = None
    notes: str = ""
    last_enriched_source: Optional[str] = None
    title_source: Optional[str] = None
    company_source: Optional[str] = None
    department_source: Optional[str] = None
    primary_email_source: Optional[str] = None
    phone_source: Optional[str] = None
    notes_source: Optional[str] = None


def test_drawer_initial_state_collapsed_with_zero_count(qt_app):
    """A freshly-constructed drawer starts collapsed and reports
    'Attendees (0)' before set_contacts is called."""
    d = AttendeeDetailsDrawer()
    try:
        assert not d.is_expanded()
        assert d._title.text() == "Attendees (0)"  # noqa: SLF001
    finally:
        d.deleteLater()


def test_drawer_expand_toggle_changes_state(qt_app):
    """Clicking the toggle expands; clicking again collapses."""
    d = AttendeeDetailsDrawer()
    try:
        d._on_toggle()  # noqa: SLF001
        assert d.is_expanded()
        d._on_toggle()  # noqa: SLF001
        assert not d.is_expanded()
    finally:
        d.deleteLater()


def test_drawer_table_renders_contact_fields(qt_app):
    """set_contacts populates the table; cells map to rich fields.

    Column shape: Name | Title | Company | Email | Notes | (edit button).
    Phone field on Contact is intentionally not shown -- the drawer
    favors Notes (free-form context) over Phone (rarely used in
    meeting prep) per Aaron's 2026-05-28 feedback. Each value cell
    that has a non-None source gets a trailing source emoji.
    """
    d = AttendeeDetailsDrawer()
    try:
        d.set_contacts([
            _FakeContact(
                id=1, display_name="Bob Smith",
                title="VP", company="Acme",
                primary_email="bob@acme.com",
                notes="met at strategy offsite",
            ),
        ])
        assert d._title.text() == "Attendees (1)"  # noqa: SLF001
        assert d._table.rowCount() == 1  # noqa: SLF001
        assert d._table.columnCount() == 6  # noqa: SLF001
        # Name cell holds the display name + (possibly) a source badge.
        assert "Bob Smith" in d._table.item(0, 0).text()  # noqa: SLF001
        # No per-field sources set -> cells carry just the values.
        assert d._table.item(0, 1).text() == "VP"  # noqa: SLF001
        assert d._table.item(0, 2).text() == "Acme"  # noqa: SLF001
        assert d._table.item(0, 3).text() == "bob@acme.com"  # noqa: SLF001
        assert d._table.item(0, 4).text() == "met at strategy offsite"  # noqa: SLF001
        # Edit button column carries a widget, not text.
        assert d._table.cellWidget(0, 5) is not None  # noqa: SLF001
    finally:
        d.deleteLater()


def test_drawer_renders_per_field_source_badges(qt_app):
    """Each value cell gets a trailing badge matching its *_source."""
    d = AttendeeDetailsDrawer()
    try:
        d.set_contacts([
            _FakeContact(
                id=1, display_name="Bob",
                title="VP", title_source="outlook",
                company="Acme", company_source="llm",
                primary_email="bob@acme.com", primary_email_source="manual",
                notes="ok", notes_source="manual",
            ),
        ])
        assert "\U0001F4E7" in d._table.item(0, 1).text()  # noqa: SLF001 # Title -> envelope
        assert "\U0001F916" in d._table.item(0, 2).text()  # noqa: SLF001 # Company -> robot
        assert "✋" in d._table.item(0, 3).text()  # noqa: SLF001 # Email -> hand
        assert "✋" in d._table.item(0, 4).text()  # noqa: SLF001 # Notes -> hand
    finally:
        d.deleteLater()


def test_drawer_empty_value_omits_badge_even_with_source(qt_app):
    """Defensive: an empty value with a stale source column shouldn't
    render a lone badge. The cell stays empty."""
    d = AttendeeDetailsDrawer()
    try:
        d.set_contacts([
            _FakeContact(
                id=1, display_name="Bob",
                title="", title_source="outlook",  # cleared, source stale
            ),
        ])
        assert d._table.item(0, 1).text() == ""  # noqa: SLF001
    finally:
        d.deleteLater()


def test_drawer_manual_emoji_is_hand_not_pencil(qt_app):
    """The manual-edit emoji moved from pencil to a raised hand so it
    doesn't visually clash with the Edit button's pencil glyph."""
    from meeting_notetaker.ui.attendee_details_drawer import _SOURCE_EMOJI
    assert _SOURCE_EMOJI["manual"] == "✋"
    assert "✏" not in _SOURCE_EMOJI["manual"]


def test_drawer_empty_fields_render_as_blank(qt_app):
    """A Contact with NULL rich fields renders empty cells, not 'n/a'."""
    d = AttendeeDetailsDrawer()
    try:
        d.set_contacts([_FakeContact(id=1, display_name="Plain Jane")])
        for col in (1, 2, 3, 4):
            assert d._table.item(0, col).text() == ""  # noqa: SLF001
    finally:
        d.deleteLater()


def test_drawer_edit_button_emits_contact_id(qt_app):
    """Clicking the per-row Edit button emits contact_clicked so the
    SessionView opens the Address Book editor filtered to that contact."""
    d = AttendeeDetailsDrawer()
    captured = []
    d.contact_clicked.connect(captured.append)
    try:
        d.set_contacts([
            _FakeContact(id=42, display_name="Alpha"),
            _FakeContact(id=99, display_name="Beta"),
        ])
        # Trigger the embedded button programmatically.
        d._table.cellWidget(0, 5).click()  # noqa: SLF001
        d._table.cellWidget(1, 5).click()  # noqa: SLF001
        assert captured == [42, 99]
    finally:
        d.deleteLater()


def test_drawer_notes_cell_has_tooltip_with_full_text(qt_app):
    """Notes can be long; the cell tooltip exposes the full string
    even when the visible text is truncated."""
    d = AttendeeDetailsDrawer()
    try:
        long_notes = "a" * 200
        d.set_contacts([_FakeContact(
            id=1, display_name="Bob", notes=long_notes,
        )])
        assert d._table.item(0, 4).toolTip() == long_notes  # noqa: SLF001
    finally:
        d.deleteLater()


def test_drawer_source_badge_renders_for_outlook(qt_app):
    """A contact enriched from Outlook gets the envelope emoji."""
    d = AttendeeDetailsDrawer()
    try:
        d.set_contacts([_FakeContact(
            id=1, display_name="Bob",
            last_enriched_source="outlook",
        )])
        name_cell = d._table.item(0, 0).text()  # noqa: SLF001
        assert "Bob" in name_cell
        assert "\U0001F4E7" in name_cell, (
            f"expected envelope emoji, got {name_cell!r}"
        )
    finally:
        d.deleteLater()


def test_drawer_source_badge_renders_for_llm(qt_app):
    """A contact enriched from LLM extraction gets the robot emoji."""
    d = AttendeeDetailsDrawer()
    try:
        d.set_contacts([_FakeContact(
            id=1, display_name="Mary",
            last_enriched_source="llm",
        )])
        name_cell = d._table.item(0, 0).text()  # noqa: SLF001
        assert "\U0001F916" in name_cell
    finally:
        d.deleteLater()


def test_drawer_no_badge_when_no_source(qt_app):
    """A never-enriched contact's name cell has no trailing emoji."""
    d = AttendeeDetailsDrawer()
    try:
        d.set_contacts([_FakeContact(
            id=1, display_name="Anonymous",
            last_enriched_source=None,
        )])
        # Strip() in the drawer means trailing space from missing
        # emoji is removed.
        assert d._table.item(0, 0).text() == "Anonymous"  # noqa: SLF001
    finally:
        d.deleteLater()


def test_drawer_cell_double_click_emits_contact_id(qt_app):
    """Double-clicking a row's cell emits contact_clicked with the
    Contact's id. MainApp routes that to the Address Book."""
    d = AttendeeDetailsDrawer()
    captured = []
    d.contact_clicked.connect(captured.append)
    try:
        d.set_contacts([
            _FakeContact(id=42, display_name="Alpha"),
            _FakeContact(id=99, display_name="Beta"),
        ])
        d._on_cell_clicked(0, 0)  # noqa: SLF001
        d._on_cell_clicked(1, 2)  # noqa: SLF001
        assert captured == [42, 99]
    finally:
        d.deleteLater()


def test_drawer_set_contacts_with_empty_collapses(qt_app):
    """If the drawer is expanded and set_contacts gets an empty list,
    auto-collapse so the empty body isn't taking visual space."""
    d = AttendeeDetailsDrawer()
    try:
        d.set_contacts([_FakeContact(id=1, display_name="Bob")])
        d.set_expanded(True)
        assert d.is_expanded()
        d.set_contacts([])
        assert not d.is_expanded()
        assert d._title.text() == "Attendees (0)"  # noqa: SLF001
    finally:
        d.deleteLater()
