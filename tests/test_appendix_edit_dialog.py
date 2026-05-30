"""Editor for the four LLM-derived appendix sections (#followup).

The dialog seeds itself from the sidecar, lets the user edit
cells / add rows / delete rows, and on Accept returns the new
entries. MainApp persists them back to the sidecar + regenerates
the JSON blocks in notes.md.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QTableWidgetItem  # noqa: E402

from meeting_notetaker.ui.appendix_edit_dialog import (  # noqa: E402
    AppendixEditDialog,
)
from meeting_notetaker.utils.attendee_appendix import (  # noqa: E402
    AttendeeAppendixEntry,
)
from meeting_notetaker.utils.attendee_context import (  # noqa: E402
    AttendeeContextEntry,
)
from meeting_notetaker.utils.invite_mentions import (  # noqa: E402
    InviteMentionEntry,
)


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


def _dialog(qt_app, **overrides):
    seed = dict(
        attendee_context=[
            AttendeeContextEntry(name="Bob", observation="Passive."),
        ],
        attendee_details=[
            AttendeeAppendixEntry(
                name="Bob", title="CEO", company="Bobco",
            ),
        ],
        topics=["Q3 hiring"],
        referenced_attachments=[
            InviteMentionEntry(name="deck", context="Slide 4"),
        ],
    )
    seed.update(overrides)
    return AppendixEditDialog(**seed)


def test_seeds_tables_from_inputs(qt_app):
    dlg = _dialog(qt_app)
    try:
        # Each tab's table has one row seeded.
        assert dlg._context_tab._table.rowCount() == 1  # noqa: SLF001
        assert dlg._details_tab._table.rowCount() == 1  # noqa: SLF001
        assert dlg._topics_tab._table.rowCount() == 1  # noqa: SLF001
        assert dlg._referenced_tab._table.rowCount() == 1  # noqa: SLF001
        # The Attendee Context observation lands in column 1.
        item = dlg._context_tab._table.item(0, 1)  # noqa: SLF001
        assert item.text() == "Passive."
    finally:
        dlg.deleteLater()


def test_collect_returns_edited_values(qt_app):
    dlg = _dialog(qt_app)
    try:
        # Simulate the user editing Bob's observation.
        dlg._context_tab._table.setItem(  # noqa: SLF001
            0, 1, QTableWidgetItem("Engaged on Q3 numbers."),
        )
        result = dlg.attendee_context()
        assert len(result) == 1
        assert result[0].observation == "Engaged on Q3 numbers."
    finally:
        dlg.deleteLater()


def test_collect_skips_rows_without_name(qt_app):
    """An entry with no name field is meaningless -- the dialog
    drops it on collect, matching the parser posture."""
    dlg = _dialog(qt_app)
    try:
        # Add an empty row to attendee_context.
        dlg._context_tab._on_add()  # noqa: SLF001
        # Now have 2 rows but the new one is blank.
        assert dlg._context_tab._table.rowCount() == 2  # noqa: SLF001
        result = dlg.attendee_context()
        # Only the seeded row survives.
        assert len(result) == 1
    finally:
        dlg.deleteLater()


def test_delete_row_removes_entry(qt_app):
    dlg = _dialog(qt_app)
    try:
        dlg._context_tab._table.selectRow(0)  # noqa: SLF001
        dlg._context_tab._on_delete()  # noqa: SLF001
        assert dlg.attendee_context() == []
    finally:
        dlg.deleteLater()


def test_add_row_inserts_blank(qt_app):
    dlg = _dialog(qt_app)
    try:
        before = dlg._topics_tab._table.rowCount()  # noqa: SLF001
        dlg._topics_tab._on_add()  # noqa: SLF001
        after = dlg._topics_tab._table.rowCount()  # noqa: SLF001
        assert after == before + 1
    finally:
        dlg.deleteLater()


def test_topics_dedupes_case_insensitive(qt_app):
    dlg = _dialog(qt_app, topics=["Q3 hiring"])
    try:
        # User added a near-duplicate.
        dlg._topics_tab._on_add()  # noqa: SLF001
        dlg._topics_tab._table.setItem(  # noqa: SLF001
            1, 0, QTableWidgetItem("q3 HIRING"),
        )
        out = dlg.topics()
        assert out == ["Q3 hiring"]
    finally:
        dlg.deleteLater()


def test_attendee_details_full_row_round_trip(qt_app):
    dlg = _dialog(qt_app, attendee_details=[
        AttendeeAppendixEntry(
            name="Mary", title="VP",
            company="MaryCo", department="Eng",
            email="mary@co.com", phone="555-0100",
        ),
    ])
    try:
        out = dlg.attendee_details()
        assert len(out) == 1
        e = out[0]
        assert e.name == "Mary"
        assert e.title == "VP"
        assert e.company == "MaryCo"
        assert e.department == "Eng"
        assert e.email == "mary@co.com"
        assert e.phone == "555-0100"
    finally:
        dlg.deleteLater()
