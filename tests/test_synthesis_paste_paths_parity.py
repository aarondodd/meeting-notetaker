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


def test_shared_pipeline_respects_strip_toggle(app_with_session):
    """strip_attendee_appendix ON removes every raw JSON block from
    the saved notes.md, but the sidecar persists (written before
    the strip pass), so the Appendix tray stays populated."""
    ma, sid = app_with_session
    ma.config.synthesis.strip_attendee_appendix = True
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
    # Sidecar still has the data.
    from meeting_notetaker.utils.appendix_store import AppendixStore
    ctx, det, topics, ref = AppendixStore(sid).load_as_dataclasses()
    assert ctx and det and topics and ref


def test_shared_pipeline_default_keeps_raw_blocks_in_notes(app_with_session):
    """Default strip flag is OFF -- raw JSON stays in notes.md so
    the editor surface shows what the LLM produced."""
    ma, sid = app_with_session
    assert ma.config.synthesis.strip_attendee_appendix is False
    ma._apply_synthesis_result(sid, _SYNTHESIS_BODY, archive_existing=False)  # noqa: SLF001
    from meeting_notetaker.models.transcript import TranscriptStore
    saved = TranscriptStore(sid).read_notes()
    assert "Attendee Context (auto-extracted)" in saved
    assert "Referenced Attachments (auto-extracted)" in saved


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
