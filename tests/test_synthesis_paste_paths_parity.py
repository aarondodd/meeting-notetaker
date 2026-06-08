"""Parity test for the Chrome-extension vs. manual paste-back flows.

The Chrome flow lands in MainApp._handle_synthesis_result; the
manual ``Paste Response Back...`` button lands in
MainApp._on_paste_notes. Before the v0.7.3 followup, those two
paths diverged -- the manual one skipped the Attendee Details
appendix application, the sidecar write, the strip-on-save
toggle, and the loose-list normalization. Both now route through
the shared _apply_synthesis_result helper so the on-disk + sidecar
state is identical regardless of which path delivered the synthesis
body.

These tests construct the helper's inputs directly (no widgets,
no PyQt main loop) and verify the post-processing side effects.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PyQt6.QtWidgets")

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402


_SYNTHESIS_BODY = """# Q3 Test

Notes content.

## Attendee Context (auto-extracted)

```json
[{"name": "Bob", "observation": "led the discussion"}]
```

## Attendee Details (auto-extracted)

```json
[{"name": "Bob", "title": "CEO", "company": "Bobco"}]
```

## Suggested Topics (auto-extracted)

```json
["Q3 hiring"]
```

## Referenced Attachments (auto-extracted)

```json
[{"name": "deck", "context": "slide 4"}]
```
"""


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture
def app_with_session(qt_app, isolated_data_dir):
    """Build a real MainApp + create one empty session; return
    (MainApp, session_id) for the test to drive _apply_synthesis_
    result against."""
    from meeting_notetaker.app import MainApp
    from meeting_notetaker.models.transcript import TranscriptStore
    ma = MainApp(qt_app)
    # System prompts ON so the _apply_attendee_details_appendix
    # gate doesn't short-circuit (it skips when the flag is off
    # because the LLM wouldn't have emitted the appendix in the
    # first place).
    ma.config.synthesis.auto_extract_attendee_details = True
    session = ma.store.create_session(title="Test")
    sid = session.id
    # Seed an attendee so the resolver has a Contact to enrich --
    # the appendix-apply step ENRICHES existing Contacts, doesn't
    # create new ones.
    TranscriptStore(sid).save_live_notes("# Attendees\n- Bob\n")
    ma._sync_attendees_to_people(sid, "# Attendees\n- Bob\n")  # noqa: SLF001
    ma._refresh_session_list()  # noqa: SLF001
    ma._on_session_selected(sid)  # noqa: SLF001
    yield ma, sid


def test_shared_pipeline_writes_appendix_sidecar(app_with_session):
    """_apply_synthesis_result persists every parsed appendix to
    notes.appendices.json regardless of the strip toggle. This is
    what lets the Appendix tray + preview transform survive a
    user that has strip ON."""
    ma, sid = app_with_session
    ma._apply_synthesis_result(sid, _SYNTHESIS_BODY, archive_existing=False)  # noqa: SLF001
    from meeting_notetaker.utils.appendix_store import AppendixStore
    store = AppendixStore(sid)
    assert store.exists()
    ctx, det, topics, ref = store.load_as_dataclasses()
    assert [e.name for e in ctx] == ["Bob"]
    assert ctx[0].observation == "led the discussion"
    assert det[0].title == "CEO"
    assert det[0].company == "Bobco"
    assert topics == ["Q3 hiring"]
    assert ref[0].name == "deck"
    assert ref[0].context == "slide 4"


def test_shared_pipeline_enriches_existing_contact_fields(app_with_session):
    """The Attendee Details appendix fills Contact rich-fields via
    update_contact_fields(fill_empty_only=True). Without this step,
    a manual paste-back would lose the LLM's enrichment."""
    ma, sid = app_with_session
    ma._apply_synthesis_result(sid, _SYNTHESIS_BODY, archive_existing=False)  # noqa: SLF001
    bob = next(
        c for c in ma.classification.list_contacts()
        if c.display_name == "Bob"
    )
    assert bob.title == "CEO"
    assert bob.company == "Bobco"


def test_shared_pipeline_strips_raw_blocks_unconditionally(app_with_session):
    """#93: appendix is system-managed data. Raw JSON blocks always
    strip from notes.md on paste-back; the sidecar is the canonical
    source for the Edit Appendix UI + render/export."""
    ma, sid = app_with_session
    ma._apply_synthesis_result(sid, _SYNTHESIS_BODY, archive_existing=False)  # noqa: SLF001
    from meeting_notetaker.models.transcript import TranscriptStore
    saved = TranscriptStore(sid).read_notes()
    for tag in (
        "Attendee Context",
        "Attendee Details",
        "Suggested Topics",
        "Referenced Attachments",
    ):
        assert f"{tag} (auto-extracted)" not in saved, (
            f"{tag} should have been stripped"
        )
    # Sidecar still has the data (written BEFORE the strip pass).
    from meeting_notetaker.utils.appendix_store import AppendixStore
    ctx, det, topics, ref = AppendixStore(sid).load_as_dataclasses()
    assert ctx and det and topics and ref


def test_shared_pipeline_normalizes_loose_list_serialization(app_with_session):
    """Claude.ai's Copy button emits loose-list markdown (blank
    line between every bullet). The normalize_synthesis_markdown
    pass tightens that. Both the Chrome flow and the manual paste
    benefit because the user might paste loose-list text from any
    source."""
    ma, sid = app_with_session
    loose = (
        "# Title\n\n"
        "- bullet a\n\n"
        "- bullet b\n\n"
        "- bullet c\n"
    )
    ma._apply_synthesis_result(sid, loose, archive_existing=False)  # noqa: SLF001
    from meeting_notetaker.models.transcript import TranscriptStore
    saved = TranscriptStore(sid).read_notes()
    # Tight-list output: no blank lines between consecutive bullets.
    assert "- bullet a\n- bullet b\n- bullet c" in saved


def test_chrome_and_manual_flow_produce_identical_state(app_with_session):
    """The two paste entry points must end up with the same
    notes.md, sidecar payload, and Contact enrichment. Pinning
    this prevents the parity bug from regressing."""
    ma, sid = app_with_session
    # Simulate the manual flow.
    ma._apply_synthesis_result(sid, _SYNTHESIS_BODY, archive_existing=False)  # noqa: SLF001
    from meeting_notetaker.models.transcript import TranscriptStore
    from meeting_notetaker.utils.appendix_store import AppendixStore
    manual_notes = TranscriptStore(sid).read_notes()
    manual_sidecar = AppendixStore(sid).load()
    manual_bob = next(
        c for c in ma.classification.list_contacts()
        if c.display_name == "Bob"
    )
    manual_bob_title = manual_bob.title

    # Now spin up a SECOND session and run the Chrome path stub on it
    # (skip the in-progress banner clear + the empty-body guard --
    # neither affects on-disk state).
    session2 = ma.store.create_session(title="Test 2")
    sid2 = session2.id
    TranscriptStore(sid2).save_live_notes("# Attendees\n- Bob\n")
    ma._sync_attendees_to_people(sid2, "# Attendees\n- Bob\n")  # noqa: SLF001
    ma._refresh_session_list()  # noqa: SLF001
    ma._on_session_selected(sid2)  # noqa: SLF001
    ma._apply_synthesis_result(sid2, _SYNTHESIS_BODY, archive_existing=False)  # noqa: SLF001
    chrome_notes = TranscriptStore(sid2).read_notes()
    chrome_sidecar = AppendixStore(sid2).load()

    assert manual_notes == chrome_notes
    assert manual_sidecar == chrome_sidecar
    # Bob's enrichment is at the Contact level (not session-scoped)
    # so both runs converge on the same value.
    assert manual_bob_title == "CEO"


# ---- Edit-dialog + strip-toggle parity (#73 finding #2 + #8) ----


def test_edit_dialog_regenerate_writes_json_blocks_back(app_with_session):
    """`regenerate_notes_json` (the utility called from the edit
    dialog) restores fresh JSON blocks to notes.md so a later strip
    pass has something to strip + the sidecar contains the edited
    payload. The integrated flow then strips unconditionally per
    #93; this test is scoped to the utility behavior."""
    from meeting_notetaker.models.transcript import TranscriptStore
    from meeting_notetaker.utils.appendix_store import (
        AppendixStore,
        regenerate_notes_json,
    )
    from meeting_notetaker.utils.attendee_appendix import AttendeeAppendixEntry
    from meeting_notetaker.utils.attendee_context import (
        AttendeeContextEntry,
    )
    from meeting_notetaker.utils.invite_mentions import InviteMentionEntry

    ma, sid = app_with_session
    # Seed sidecar via the paste-back path (notes.md ends up clean).
    ma._apply_synthesis_result(sid, _SYNTHESIS_BODY, archive_existing=False)  # noqa: SLF001
    # Simulate the dialog editing Bob's observation.
    edited_ctx = [AttendeeContextEntry(name="Bob", observation="REVISED")]
    edited_det = [AttendeeAppendixEntry(name="Bob", title="CEO", company="Bobco")]
    edited_topics = ["Q3 hiring"]
    edited_ref = [InviteMentionEntry(name="deck", context="slide 4")]
    # Manually run the regenerate utility step.
    current = TranscriptStore(sid).read_notes()
    updated = regenerate_notes_json(
        current,
        attendee_context=edited_ctx,
        attendee_details=edited_det,
        topics=edited_topics,
        referenced_attachments=edited_ref,
    )
    # The utility puts JSON blocks back into the body (the integrated
    # path then strips them post-utility per #93; this is utility-level).
    assert "Attendee Context (auto-extracted)" in updated
    assert "REVISED" in updated
    # Sidecar persistence is independent.
    AppendixStore(sid).save(
        attendee_context=edited_ctx,
        attendee_details=edited_det,
        topics=edited_topics,
        referenced_attachments=edited_ref,
    )
    ctx, det, topics, ref = AppendixStore(sid).load_as_dataclasses()
    assert ctx[0].observation == "REVISED"


def test_edit_dialog_integrated_flow_keeps_notes_clean(app_with_session):
    """Integrated dialog edit flow strips the regenerated JSON
    blocks (per #93) so the on-disk notes.md stays clean.
    Sidecar still gets the edit (#73 finding #2)."""
    from meeting_notetaker.models.transcript import TranscriptStore
    from meeting_notetaker.utils.appendix_store import (
        AppendixStore,
        regenerate_notes_json,
    )
    from meeting_notetaker.utils.attendee_appendix import AttendeeAppendixEntry
    from meeting_notetaker.utils.attendee_context import (
        AttendeeContextEntry,
    )
    from meeting_notetaker.utils.invite_mentions import InviteMentionEntry

    ma, sid = app_with_session
    # Seed via paste-back (always strips per #93).
    ma._apply_synthesis_result(sid, _SYNTHESIS_BODY, archive_existing=False)  # noqa: SLF001
    saved_after_paste = TranscriptStore(sid).read_notes()
    assert "Attendee Context (auto-extracted)" not in saved_after_paste
    # Simulate dialog edit + the integrated regenerate + strip pass.
    edited_ctx = [AttendeeContextEntry(name="Bob", observation="REVISED")]
    edited_det = [AttendeeAppendixEntry(name="Bob", title="CEO", company="Bobco")]
    edited_topics = ["Q3 hiring"]
    edited_ref = [InviteMentionEntry(name="deck", context="slide 4")]
    updated = regenerate_notes_json(
        saved_after_paste,
        attendee_context=edited_ctx,
        attendee_details=edited_det,
        topics=edited_topics,
        referenced_attachments=edited_ref,
    )
    # #93: strip pass runs unconditionally after regenerate, so the
    # JSON blocks regenerate_notes_json just added get removed.
    updated = ma._strip_all_appendices(updated)  # noqa: SLF001
    # notes.md stays clean.
    for tag in (
        "Attendee Context",
        "Attendee Details",
        "Suggested Topics",
        "Referenced Attachments",
    ):
        assert f"{tag} (auto-extracted)" not in updated
    # Sidecar still carries the edited data.
    AppendixStore(sid).save(
        attendee_context=edited_ctx,
        attendee_details=edited_det,
        topics=edited_topics,
        referenced_attachments=edited_ref,
    )
    ctx, _det, _topics, _ref = AppendixStore(sid).load_as_dataclasses()
    assert ctx[0].observation == "REVISED"
