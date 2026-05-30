"""Pre-export Appendix inclusion prompt (#followup).

Six per-section checkboxes under a master "Include Appendix"
toggle. Default selects every populated section; sections that
have zero entries render as a disabled "(empty)" checkbox so the
user understands why they can't be toggled.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.ui.appendix_inclusion_dialog import (  # noqa: E402
    AppendixInclusion,
    AppendixInclusionDialog,
    apply_inclusion,
)
from meeting_notetaker.utils.appendix_transform import AppendixData  # noqa: E402
from meeting_notetaker.utils.attendee_appendix import (  # noqa: E402
    AttendeeAppendixEntry,
)
from meeting_notetaker.utils.attendee_context import (  # noqa: E402
    AttendeeContextEntry,
)
from meeting_notetaker.utils.invite_mentions import (  # noqa: E402
    InviteMentionEntry,
)
from meeting_notetaker.utils.link_extractor import ExtractedLink  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


def _populated() -> AppendixData:
    return AppendixData(
        attendee_context=[
            AttendeeContextEntry(name="Bob", observation="ok"),
        ],
        attendee_details=[
            AttendeeAppendixEntry(name="Bob", title="CEO"),
        ],
        topics=["Q3 hiring"],
        referenced_attachments=[
            InviteMentionEntry(name="deck", context="slide 4"),
        ],
        session_attachments=["notes.pptx"],
        links=[
            ExtractedLink(url="https://x.example", label="x", source="notes"),
        ],
    )


def _partial() -> AppendixData:
    """Populates only two sections so the dialog disables four."""
    return AppendixData(
        attendee_context=[],
        attendee_details=[
            AttendeeAppendixEntry(name="Bob", title="CEO"),
        ],
        topics=[],
        referenced_attachments=[],
        session_attachments=[],
        links=[
            ExtractedLink(url="https://x.example", label="x", source="notes"),
        ],
    )


# ---- AppendixInclusion + apply_inclusion ---------------------------


def test_all_on_keeps_every_section():
    data = _populated()
    out = apply_inclusion(data, AppendixInclusion.all_on())
    assert out.attendee_context == data.attendee_context
    assert out.attendee_details == data.attendee_details
    assert out.topics == data.topics
    assert out.referenced_attachments == data.referenced_attachments
    assert out.session_attachments == data.session_attachments
    assert out.links == data.links


def test_all_off_empties_everything():
    data = _populated()
    out = apply_inclusion(data, AppendixInclusion.all_off())
    assert out.attendee_context == []
    assert out.attendee_details == []
    assert out.topics == []
    assert out.referenced_attachments == []
    assert out.session_attachments == []
    assert out.links == []


def test_per_section_toggle_only_drops_that_section():
    data = _populated()
    inc = AppendixInclusion(
        include_appendix=True,
        include_attendee_context=False,
        include_attendee_details=True,
        include_topics=True,
        include_referenced_attachments=True,
        include_session_attachments=True,
        include_links=True,
    )
    out = apply_inclusion(data, inc)
    assert out.attendee_context == []
    assert out.attendee_details == data.attendee_details
    assert out.topics == data.topics


def test_master_off_overrides_section_flags():
    """include_appendix=False zeros everything regardless of per-
    section flags."""
    inc = AppendixInclusion(
        include_appendix=False,
        include_attendee_context=True,
        include_topics=True,
    )
    out = apply_inclusion(_populated(), inc)
    assert out.attendee_context == []
    assert out.topics == []


# ---- Dialog widget -------------------------------------------------


def test_dialog_defaults_to_all_populated_on(qt_app):
    dlg = AppendixInclusionDialog(_populated())
    try:
        inc = dlg.inclusion()
        assert inc.include_appendix
        assert inc.include_attendee_context
        assert inc.include_topics
    finally:
        dlg.deleteLater()


def test_dialog_disables_empty_sections(qt_app):
    """Empty sections render as disabled "(empty)" checkboxes so
    the user knows they're not skipped by choice."""
    dlg = AppendixInclusionDialog(_partial())
    try:
        # Attendee Context is empty in _partial.
        ctx_cb = dlg._sections["attendee_context"]  # noqa: SLF001
        assert not ctx_cb.isEnabled()
        assert "(empty)" in ctx_cb.text()
        # Details is populated; enabled + checked.
        det_cb = dlg._sections["attendee_details"]  # noqa: SLF001
        assert det_cb.isEnabled()
        assert det_cb.isChecked()
    finally:
        dlg.deleteLater()


def test_master_uncheck_disables_subsections(qt_app):
    dlg = AppendixInclusionDialog(_populated())
    try:
        dlg._master.setChecked(False)  # noqa: SLF001
        for cb in dlg._sections.values():  # noqa: SLF001
            assert not cb.isChecked()
            assert not cb.isEnabled()
        inc = dlg.inclusion()
        assert not inc.include_appendix
    finally:
        dlg.deleteLater()


def test_master_recheck_only_restores_populated(qt_app):
    """Sections that are empty stay disabled even when master flips
    back on -- there's nothing to include."""
    dlg = AppendixInclusionDialog(_partial())
    try:
        dlg._master.setChecked(False)  # noqa: SLF001
        dlg._master.setChecked(True)  # noqa: SLF001
        det_cb = dlg._sections["attendee_details"]  # noqa: SLF001
        ctx_cb = dlg._sections["attendee_context"]  # noqa: SLF001
        assert det_cb.isEnabled() and det_cb.isChecked()
        assert not ctx_cb.isEnabled()
        assert "(empty)" in ctx_cb.text()
    finally:
        dlg.deleteLater()


def test_defaults_kwarg_pre_checks_user_saved_state(qt_app):
    """Settings-saved AppendixInclusion gets pre-checked into the
    dialog (#65/#66 followup). Aaron's defaults: attendee context
    + documents + links ON; details + topics OFF."""
    saved = AppendixInclusion(
        include_appendix=True,
        include_attendee_context=True,
        include_attendee_details=False,
        include_topics=False,
        include_referenced_attachments=True,
        include_session_attachments=True,
        include_links=True,
    )
    dlg = AppendixInclusionDialog(_populated(), defaults=saved)
    try:
        cb = dlg._sections  # noqa: SLF001
        assert cb["attendee_context"].isChecked()
        assert not cb["attendee_details"].isChecked()
        assert not cb["topics"].isChecked()
        assert cb["referenced_attachments"].isChecked()
        assert cb["session_attachments"].isChecked()
        assert cb["links"].isChecked()
        # Saved default-off sections are still ENABLED so the user
        # can opt back in on a one-off export.
        assert cb["attendee_details"].isEnabled()
    finally:
        dlg.deleteLater()


def test_defaults_master_off_disables_subsections(qt_app):
    """A saved master-off default opens with everything off + disabled,
    matching the runtime master-uncheck behavior."""
    saved = AppendixInclusion.all_off()
    dlg = AppendixInclusionDialog(_populated(), defaults=saved)
    try:
        assert not dlg._master.isChecked()  # noqa: SLF001
        for cb in dlg._sections.values():  # noqa: SLF001
            assert not cb.isChecked()
            assert not cb.isEnabled()
    finally:
        dlg.deleteLater()


def test_inclusion_reflects_user_uncheck(qt_app):
    """Unchecking one section's checkbox excludes only that
    section from the returned inclusion."""
    dlg = AppendixInclusionDialog(_populated())
    try:
        dlg._sections["topics"].setChecked(False)  # noqa: SLF001
        inc = dlg.inclusion()
        assert inc.include_topics is False
        assert inc.include_attendee_context is True
    finally:
        dlg.deleteLater()
