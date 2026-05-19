"""SpeakerTagStore -- per-session JSON-backed tag store."""
from __future__ import annotations

import json

import pytest

from meeting_notetaker.models.speaker_tags import (
    FILENAME,
    SCHEMA_VERSION,
    SpeakerTag,
    SpeakerTagStore,
    load_tags,
)


def test_load_returns_empty_when_file_missing(tmp_path):
    store = SpeakerTagStore(tmp_path)
    assert store.load() == []


def test_append_persists_and_reloads(tmp_path):
    store = SpeakerTagStore(tmp_path)
    store.append(SpeakerTag(name="Pat", t_seconds=12.5))
    store.append(SpeakerTag(name="Sam", t_seconds=42.0))
    loaded = store.load()
    assert loaded == [
        SpeakerTag(name="Pat", t_seconds=12.5),
        SpeakerTag(name="Sam", t_seconds=42.0),
    ]


def test_append_strips_whitespace_in_name(tmp_path):
    store = SpeakerTagStore(tmp_path)
    store.append(SpeakerTag(name="  Pat  ", t_seconds=1.0))
    assert store.load()[0].name == "Pat"


def test_append_rejects_empty_name(tmp_path):
    store = SpeakerTagStore(tmp_path)
    with pytest.raises(ValueError):
        store.append(SpeakerTag(name="   ", t_seconds=1.0))


def test_append_rejects_negative_seconds(tmp_path):
    store = SpeakerTagStore(tmp_path)
    with pytest.raises(ValueError):
        store.append(SpeakerTag(name="Pat", t_seconds=-1.0))


def test_remove_last_for_pops_most_recent(tmp_path):
    store = SpeakerTagStore(tmp_path)
    store.append(SpeakerTag(name="Pat", t_seconds=1.0))
    store.append(SpeakerTag(name="Sam", t_seconds=2.0))
    store.append(SpeakerTag(name="Pat", t_seconds=3.0))
    removed = store.remove_last_for("Pat")
    assert removed is True
    remaining = store.load()
    assert remaining == [
        SpeakerTag(name="Pat", t_seconds=1.0),
        SpeakerTag(name="Sam", t_seconds=2.0),
    ]


def test_remove_last_for_returns_false_when_no_match(tmp_path):
    store = SpeakerTagStore(tmp_path)
    store.append(SpeakerTag(name="Pat", t_seconds=1.0))
    assert store.remove_last_for("Sam") is False
    # Original entry must still be there.
    assert len(store.load()) == 1


def test_remove_last_for_case_insensitive(tmp_path):
    store = SpeakerTagStore(tmp_path)
    store.append(SpeakerTag(name="Pat", t_seconds=1.0))
    assert store.remove_last_for("PAT") is True
    assert store.load() == []


def test_counts(tmp_path):
    store = SpeakerTagStore(tmp_path)
    for t, name in [(1.0, "Pat"), (2.0, "Sam"), (3.0, "Pat"), (4.0, "Pat")]:
        store.append(SpeakerTag(name=name, t_seconds=t))
    assert store.counts() == {"Pat": 3, "Sam": 1}


def test_garbage_json_treated_as_empty(tmp_path):
    (tmp_path / FILENAME).write_text("not-valid-json", encoding="utf-8")
    store = SpeakerTagStore(tmp_path)
    assert store.load() == []


def test_partial_record_skipped(tmp_path):
    # Mix valid + invalid entries; only the valid ones round-trip.
    bad = {
        "version": SCHEMA_VERSION,
        "tags": [
            {"name": "Pat", "t_seconds": 5.0},
            {"name": "", "t_seconds": 6.0},          # empty name
            {"name": "Bad", "t_seconds": -1.0},      # negative
            {"name": "Sam"},                          # missing t_seconds
            "not-a-dict",
            {"name": "Maya", "t_seconds": 7.0},
        ],
    }
    (tmp_path / FILENAME).write_text(json.dumps(bad), encoding="utf-8")
    store = SpeakerTagStore(tmp_path)
    loaded = store.load()
    assert [t.name for t in loaded] == ["Pat", "Maya"]


def test_load_tags_helper(tmp_path):
    SpeakerTagStore(tmp_path).append(SpeakerTag(name="Pat", t_seconds=1.0))
    assert load_tags(tmp_path) == [SpeakerTag(name="Pat", t_seconds=1.0)]


def test_atomic_write_does_not_leak_temp_files(tmp_path):
    store = SpeakerTagStore(tmp_path)
    store.append(SpeakerTag(name="Pat", t_seconds=1.0))
    store.append(SpeakerTag(name="Sam", t_seconds=2.0))
    temps = [p for p in tmp_path.iterdir() if p.name.startswith(".speaker_tags-")]
    assert temps == []
