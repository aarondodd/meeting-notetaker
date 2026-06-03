"""Export menu wiring tests (#79).

Verifies the Export... button surfaces Notion + Confluence menu
items only after MainApp marks them as configured, that picking a
menu item emits the right signal with (session_id, tab_label, body),
and that the PDF item is always visible.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.models.session import Session, STATE_COMPLETE  # noqa: E402
from meeting_notetaker.ui.session_view import SessionView  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


def _make_session() -> Session:
    return Session(
        id="sess-1", title="Sample", state=STATE_COMPLETE,
        created_at="2026-06-02T09:00:00+00:00",
    )


def test_export_button_default_hides_notion_and_confluence(qt_app):
    sv = SessionView()
    try:
        actions = sv._export_menu.actions()  # noqa: SLF001
        labels = [a.text() for a in actions]
        # Wording reorg 2026-06-03: button is Save to..., PDF is Save
        # as PDF... (file format), Notion / Confluence are Save to ...
        assert sv._export_btn.text() == "Save to..."  # noqa: SLF001
        assert "Save as PDF..." in labels
        assert any("Save to Notion" in l for l in labels)
        assert any("Save to Confluence" in l for l in labels)
        notion = next(a for a in actions if "Notion" in a.text())
        confluence = next(a for a in actions if "Confluence" in a.text())
        # Hidden by default until verified.
        assert notion.isVisible() is False
        assert confluence.isVisible() is False
    finally:
        sv.deleteLater()


def test_set_integration_targets_toggles_visibility(qt_app):
    sv = SessionView()
    try:
        sv.set_integration_targets(notion_enabled=True, confluence_enabled=False)
        notion = next(
            a for a in sv._export_menu.actions()  # noqa: SLF001
            if "Notion" in a.text()
        )
        confluence = next(
            a for a in sv._export_menu.actions()  # noqa: SLF001
            if "Confluence" in a.text()
        )
        assert notion.isVisible() is True
        assert confluence.isVisible() is False
        sv.set_integration_targets(notion_enabled=False, confluence_enabled=True)
        assert notion.isVisible() is False
        assert confluence.isVisible() is True
    finally:
        sv.deleteLater()


def test_export_to_notion_action_emits_signal_with_active_tab_body(qt_app):
    sv = SessionView()
    sv.set_session(
        _make_session(),
        transcript="", notes="Synth content here.", previous_notes_paths=[],
        live_notes="My notes content here.",
    )
    captured: list[tuple[str, str, str]] = []
    sv.export_to_notion_requested.connect(
        lambda sid, label, body: captured.append((sid, label, body))
    )
    try:
        # Synthesis tab carries the notes content; trigger the action.
        sv._tabs.setCurrentWidget(sv._notes_page)  # noqa: SLF001
        sv._export_notion_action.trigger()  # noqa: SLF001
        assert len(captured) == 1
        sid, label, body = captured[0]
        assert sid == "sess-1"
        assert label == "Synthesis"
        assert "Synth content here" in body
    finally:
        sv.deleteLater()


def test_export_to_confluence_action_emits_signal_for_my_notes_tab(qt_app):
    sv = SessionView()
    sv.set_session(
        _make_session(),
        transcript="", notes="Synth content.", previous_notes_paths=[],
        live_notes="Aaron's running notes.",
    )
    captured: list[tuple[str, str, str]] = []
    sv.export_to_confluence_requested.connect(
        lambda sid, label, body: captured.append((sid, label, body))
    )
    try:
        sv._tabs.setCurrentWidget(sv._live_notes_page)  # noqa: SLF001
        sv._export_confluence_action.trigger()  # noqa: SLF001
        assert len(captured) == 1
        sid, label, body = captured[0]
        assert sid == "sess-1"
        assert label == "My Notes"
        assert "running notes" in body
    finally:
        sv.deleteLater()


def test_export_action_does_not_emit_when_no_session_loaded(qt_app):
    sv = SessionView()
    captured = []
    sv.export_to_notion_requested.connect(lambda *a: captured.append(a))
    try:
        # No session loaded.
        sv._export_notion_action.trigger()  # noqa: SLF001
        assert captured == []
    finally:
        sv.deleteLater()


def test_export_body_strips_raw_attendee_context_appendix(qt_app):
    """Regression for the post-merge feedback: Notion/Confluence
    exports were shipping the raw JSON appendix blocks instead of
    the formatted '## Appendix (auto-extracted)' tables. The
    SessionView export hook applies the same inject_appendix
    transform the PDF path uses, so the raw block is removed from
    the body the export worker receives."""
    sv = SessionView()
    notes_with_raw_appendix = (
        "# Notes\n\n"
        "Standard content.\n\n"
        "## Attendee Context (auto-extracted)\n\n"
        "```json\n"
        '{"entries": [{"name": "Alice", "observation": "VP of widgets"}]}\n'
        "```\n"
    )
    sv.set_session(
        _make_session(),
        transcript="", notes=notes_with_raw_appendix,
        previous_notes_paths=[],
        live_notes="",
    )
    captured: list[tuple[str, str, str]] = []
    sv.export_to_notion_requested.connect(
        lambda sid, label, body: captured.append((sid, label, body))
    )
    try:
        sv._tabs.setCurrentWidget(sv._notes_page)  # noqa: SLF001
        sv._export_notion_action.trigger()  # noqa: SLF001
        assert len(captured) == 1
        _, _, body = captured[0]
        # Raw JSON block is stripped from the body that goes to the
        # export converter; the formatted appendix takes its place if
        # data is present (Aaron's post-merge feedback, 2026-06-02).
        assert "VP of widgets" not in body
        assert "```json" not in body
    finally:
        sv.deleteLater()
