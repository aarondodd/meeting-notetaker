"""Sidecar persistence + sidecar-aware collector for the LLM
appendices (#64 sidecar followup).

The four LLM-derived appendices (Attendee Context, Attendee
Details, Suggested Topics, Referenced Attachments) now persist in
``notes.appendices.json`` alongside notes.md so the strip-on-save
toggle no longer empties the Appendix tray.
"""
from __future__ import annotations

import json

import pytest

from meeting_notetaker.utils.appendix_store import (
    AppendixStore,
    SIDECAR_FILENAME,
    collect_for_session,
    regenerate_notes_json,
)
from meeting_notetaker.utils.attendee_appendix import AttendeeAppendixEntry
from meeting_notetaker.utils.attendee_context import AttendeeContextEntry
from meeting_notetaker.utils.invite_mentions import InviteMentionEntry


@pytest.fixture
def session_id():
    """A unique session id per test (the isolated_data_dir autouse
    fixture in conftest gives every test its own %APPDATA% root)."""
    return "test-session-001"


def test_load_returns_empty_dict_when_sidecar_missing(session_id):
    store = AppendixStore(session_id)
    assert store.exists() is False
    assert store.load() == {}


def test_save_persists_all_four_sections(session_id):
    store = AppendixStore(session_id)
    store.save(
        attendee_context=[
            AttendeeContextEntry(name="Bob", observation="Passive."),
        ],
        attendee_details=[
            AttendeeAppendixEntry(name="Bob", title="CEO", company="Bobco"),
        ],
        topics=["Q3 hiring", "Backend migration"],
        referenced_attachments=[
            InviteMentionEntry(name="deck", context="discussed slide 4"),
        ],
    )
    assert store.exists()
    raw = store.load()
    assert raw["attendee_context"][0]["name"] == "Bob"
    assert raw["attendee_details"][0]["company"] == "Bobco"
    assert raw["topics"] == ["Q3 hiring", "Backend migration"]
    assert raw["referenced_attachments"][0]["context"] == "discussed slide 4"


def test_save_empty_removes_existing_sidecar(session_id):
    """An empty payload deletes the file so stale data doesn't
    linger after the user empties every appendix."""
    store = AppendixStore(session_id)
    store.save(
        attendee_context=[],
        attendee_details=[],
        topics=["topic"],
        referenced_attachments=[],
    )
    assert store.exists()
    store.save(
        attendee_context=[],
        attendee_details=[],
        topics=[],
        referenced_attachments=[],
    )
    assert store.exists() is False


def test_load_as_dataclasses_round_trip(session_id):
    """Save then load_as_dataclasses returns the same entries."""
    ctx_in = [AttendeeContextEntry(name="Bob", observation="ok")]
    det_in = [AttendeeAppendixEntry(name="Bob", title="CEO")]
    topics_in = ["x"]
    ref_in = [InviteMentionEntry(name="doc", context="ref")]
    store = AppendixStore(session_id)
    store.save(
        attendee_context=ctx_in,
        attendee_details=det_in,
        topics=topics_in,
        referenced_attachments=ref_in,
    )
    ctx, det, topics, ref = store.load_as_dataclasses()
    assert ctx == ctx_in
    assert det == det_in
    assert topics == topics_in
    assert ref == ref_in


def test_load_as_dataclasses_skips_malformed_entries(session_id):
    """Entries missing a non-empty name are dropped silently."""
    store = AppendixStore(session_id)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps({
        "attendee_context": [
            {"name": "Bob", "observation": "ok"},
            {"observation": "no name"},
            {"name": "", "observation": "empty"},
        ],
        "attendee_details": [],
        "topics": ["x", "", "y"],
        "referenced_attachments": [],
    }))
    ctx, _det, topics, _ref = store.load_as_dataclasses()
    assert [e.name for e in ctx] == ["Bob"]
    assert topics == ["x", "y"]


def test_load_handles_corrupt_json_gracefully(session_id):
    """A corrupt sidecar yields an empty payload instead of raising."""
    store = AppendixStore(session_id)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ not valid json")
    assert store.load() == {}
    ctx, det, topics, ref = store.load_as_dataclasses()
    assert ctx == []
    assert det == []
    assert topics == []
    assert ref == []


def test_save_from_notes_parses_and_persists(session_id):
    """The single-call save_from_notes parses all four appendices
    out of a notes.md buffer + writes the sidecar."""
    notes = """# TL;DR
body

## Attendee Context (auto-extracted)

```json
[{"name": "Bob", "observation": "ok"}]
```

## Suggested Topics (auto-extracted)

```json
["Topic A"]
```
"""
    store = AppendixStore(session_id)
    store.save_from_notes(notes)
    raw = store.load()
    assert raw["attendee_context"][0]["name"] == "Bob"
    assert raw["topics"] == ["Topic A"]
    # Sections the source didn't carry stay as empty lists.
    assert raw["attendee_details"] == []
    assert raw["referenced_attachments"] == []


def test_collect_for_session_prefers_sidecar_over_notes(session_id):
    """When the sidecar carries data, the collector uses it even
    if notes.md is empty -- this is the strip-on-save path."""
    store = AppendixStore(session_id)
    store.save(
        attendee_context=[
            AttendeeContextEntry(name="Sidecar", observation="from cache"),
        ],
        attendee_details=[],
        topics=["sidecar-topic"],
        referenced_attachments=[],
    )
    data = collect_for_session(
        session_id=session_id,
        notes_text="",  # stripped
        live_notes_text="",
        session_attachments=[],
    )
    assert [e.name for e in data.attendee_context] == ["Sidecar"]
    assert data.topics == ["sidecar-topic"]


def test_collect_for_session_falls_back_to_parsing_notes(session_id):
    """When the sidecar is absent, the collector parses notes.md
    for the four LLM sections (back-compat for sessions that
    predate the sidecar)."""
    # No sidecar written.
    notes = """## Suggested Topics (auto-extracted)

```json
["legacy-topic"]
```
"""
    data = collect_for_session(
        session_id=session_id,
        notes_text=notes,
        live_notes_text="",
        session_attachments=[],
    )
    assert data.topics == ["legacy-topic"]


def test_collect_for_session_always_recomputes_links_and_attachments(session_id):
    """Links + session attachments are live -- never persisted to
    the sidecar."""
    AppendixStore(session_id).save(
        attendee_context=[],
        attendee_details=[],
        topics=["x"],
        referenced_attachments=[],
    )
    data = collect_for_session(
        session_id=session_id,
        notes_text="See https://example.com/a",
        live_notes_text="And https://example.com/b",
        session_attachments=["doc.pptx"],
    )
    assert data.session_attachments == ["doc.pptx"]
    urls = {link.url for link in data.links}
    assert urls == {
        "https://example.com/a",
        "https://example.com/b",
    }


def test_collect_for_session_with_no_session_id_falls_back():
    """No session id -> no sidecar lookup; parse notes.md."""
    data = collect_for_session(
        session_id=None,
        notes_text="## Suggested Topics (auto-extracted)\n\n```json\n[\"x\"]\n```",
        live_notes_text="",
        session_attachments=[],
    )
    assert data.topics == ["x"]


def test_regenerate_notes_json_replaces_existing_blocks():
    """The dialog persistence path strips raw appendix blocks +
    appends fresh ones rendered from the edited entries."""
    source = """# TL;DR
synthesis body

## Attendee Context (auto-extracted)

```json
[{"name": "OLD", "observation": "stale"}]
```

## Suggested Topics (auto-extracted)

```json
["old topic"]
```
"""
    out = regenerate_notes_json(
        source,
        attendee_context=[
            AttendeeContextEntry(name="Bob", observation="fresh"),
        ],
        attendee_details=[],
        topics=["new topic"],
        referenced_attachments=[],
    )
    # Synthesis body survives.
    assert "synthesis body" in out
    # Old entries gone.
    assert "OLD" not in out
    assert "old topic" not in out
    # New entries present.
    assert '"name": "Bob"' in out
    assert '"fresh"' in out
    assert '"new topic"' in out


def test_regenerate_notes_json_omits_empty_sections():
    """A section with zero entries doesn't get a heading +
    code block, only the populated ones do."""
    source = "# Title\nbody\n"
    out = regenerate_notes_json(
        source,
        attendee_context=[
            AttendeeContextEntry(name="Bob", observation="ok"),
        ],
        attendee_details=[],
        topics=[],
        referenced_attachments=[],
    )
    assert "Attendee Context (auto-extracted)" in out
    assert "Attendee Details" not in out
    assert "Suggested Topics" not in out
    assert "Referenced Attachments" not in out


def test_regenerate_notes_json_with_all_empty_strips_appendices():
    """All sections empty -> existing blocks vanish and nothing
    new gets appended (the dialog used to delete everything)."""
    source = """body

## Attendee Context (auto-extracted)

```json
[{"name": "x"}]
```
"""
    out = regenerate_notes_json(
        source,
        attendee_context=[],
        attendee_details=[],
        topics=[],
        referenced_attachments=[],
    )
    assert "Attendee Context" not in out
    assert "body" in out


def test_sidecar_filename_is_well_known():
    """Pin the on-disk filename so the build packaging + external
    tools depend on a stable name."""
    assert SIDECAR_FILENAME == "notes.appendices.json"
