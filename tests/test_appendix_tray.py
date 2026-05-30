"""Appendix tray widget (#64).

Smoke tests for the collapsible multi-section tray under the
SessionView editors. The tray itself doesn't parse markdown -- it
takes a pre-built AppendixData payload via set_data() and renders
each populated section. The transform tests live in
test_appendix_transform; this file pins widget behavior.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.ui.appendix_tray import AppendixTray  # noqa: E402
from meeting_notetaker.utils.appendix_transform import AppendixData  # noqa: E402
from meeting_notetaker.utils.attendee_appendix import AttendeeAppendixEntry  # noqa: E402
from meeting_notetaker.utils.attendee_context import AttendeeContextEntry  # noqa: E402
from meeting_notetaker.utils.invite_mentions import InviteMentionEntry  # noqa: E402
from meeting_notetaker.utils.link_extractor import ExtractedLink  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


def _empty() -> AppendixData:
    return AppendixData(
        attendee_context=[],
        attendee_details=[],
        topics=[],
        referenced_attachments=[],
        session_attachments=[],
        links=[],
    )


def _populated() -> AppendixData:
    return AppendixData(
        attendee_context=[
            AttendeeContextEntry(name="Bob", observation="Listed but passive."),
        ],
        attendee_details=[
            AttendeeAppendixEntry(name="Bob", title="CEO", company="Bobco"),
        ],
        topics=["Q3 hiring", "Backend migration"],
        referenced_attachments=[
            InviteMentionEntry(name="budget deck", context="Q3 spend review"),
        ],
        session_attachments=["meeting-notes.pptx"],
        links=[
            ExtractedLink(url="https://wiki/auth", label="auth", source="notes"),
        ],
    )


def test_initial_state_collapsed_zero_count(qt_app):
    tray = AppendixTray()
    try:
        assert not tray.is_expanded()
        assert tray._title.text() == "Appendix (0)"  # noqa: SLF001
    finally:
        tray.deleteLater()


def test_set_data_updates_count(qt_app):
    """Header count aggregates entries across every section."""
    tray = AppendixTray()
    try:
        tray.set_data(_populated())
        # 1 + 1 + 2 + 1 + 1 + 1 = 7
        assert tray._title.text() == "Appendix (7)"  # noqa: SLF001
    finally:
        tray.deleteLater()


def test_set_data_empty_collapses_when_expanded(qt_app):
    """An expanded tray that becomes empty auto-collapses so the
    empty body doesn't take vertical space."""
    tray = AppendixTray()
    try:
        tray.set_data(_populated())
        tray.set_expanded(True)
        assert tray.is_expanded()
        tray.set_data(_empty())
        assert not tray.is_expanded()
        assert tray._title.text() == "Appendix (0)"  # noqa: SLF001
    finally:
        tray.deleteLater()


def test_toggle_expands_and_collapses(qt_app):
    tray = AppendixTray()
    try:
        tray.set_data(_populated())
        tray._on_toggle()  # noqa: SLF001
        assert tray.is_expanded()
        tray._on_toggle()  # noqa: SLF001
        assert not tray.is_expanded()
    finally:
        tray.deleteLater()


def test_only_populated_sections_render(qt_app):
    """A payload with only one populated source renders just that
    section -- no empty-section headings."""
    tray = AppendixTray()
    try:
        only_topics = AppendixData(
            attendee_context=[],
            attendee_details=[],
            topics=["Just one topic"],
            referenced_attachments=[],
            session_attachments=[],
            links=[],
        )
        tray.set_data(only_topics)
        # One section was added (topics).
        assert tray._sections_added == 1  # noqa: SLF001
    finally:
        tray.deleteLater()


def test_replacing_data_tears_down_prior_sections(qt_app):
    """set_data() is idempotent: a second call doesn't accumulate
    sections from the first."""
    tray = AppendixTray()
    try:
        tray.set_data(_populated())
        first_added = tray._sections_added  # noqa: SLF001
        tray.set_data(_populated())
        assert tray._sections_added == first_added  # noqa: SLF001
    finally:
        tray.deleteLater()
