"""HighlightsStore + Highlight mutators.

The highlight model is one-file-per-session JSON. Tests exercise
CRUD, overlap detection, validation, and the load/save round-trip --
which has to gracefully handle a missing or corrupted file (the
session view shouldn't blow up if a user hand-edits highlights.json
into garbage).
"""
from __future__ import annotations

import json

import pytest

from meeting_notetaker.models.highlights import (
    Highlight,
    HighlightSet,
    HighlightsStore,
    add_highlight,
    has_overlap_with_existing,
    remove_highlight,
    update_highlight_range,
    update_highlight_title,
    validate_range,
)


# ---- Highlight basics --------------------------------------------------


def test_highlight_duration_ms():
    h = Highlight(start_ms=1000, end_ms=5000)
    assert h.duration_ms() == 4000


def test_highlight_zero_duration_is_zero_not_negative():
    """An immediately-cancelled toggle could land start==end; the
    duration is 0, not negative or undefined."""
    assert Highlight(start_ms=500, end_ms=500).duration_ms() == 0


def test_highlight_overlaps_detects_overlap():
    a = Highlight(start_ms=1000, end_ms=2000)
    b = Highlight(start_ms=1500, end_ms=2500)
    assert a.overlaps(b)
    assert b.overlaps(a)


def test_highlight_overlaps_treats_adjacent_as_non_overlap():
    a = Highlight(start_ms=1000, end_ms=2000)
    b = Highlight(start_ms=2000, end_ms=3000)
    assert not a.overlaps(b)
    assert not b.overlaps(a)


def test_highlight_overlaps_non_overlapping():
    a = Highlight(start_ms=1000, end_ms=2000)
    b = Highlight(start_ms=3000, end_ms=4000)
    assert not a.overlaps(b)


# ---- HighlightSet basics ----------------------------------------------


def test_highlight_set_sorted_by_start():
    hs = HighlightSet(highlights=[
        Highlight(2000, 3000),
        Highlight(500, 1500),
        Highlight(4000, 5000),
    ])
    starts = [h.start_ms for h in hs.sorted_by_start()]
    assert starts == [500, 2000, 4000]


def test_highlight_set_total_duration_ms():
    hs = HighlightSet(highlights=[
        Highlight(0, 1000),
        Highlight(2000, 4500),
    ])
    assert hs.total_duration_ms() == 3500


# ---- validate_range ---------------------------------------------------


def test_validate_range_accepts_zero_duration():
    validate_range(1000, 1000)


def test_validate_range_rejects_negative_start():
    with pytest.raises(ValueError):
        validate_range(-1, 100)


def test_validate_range_rejects_inverted_range():
    with pytest.raises(ValueError):
        validate_range(2000, 1000)


def test_validate_range_clamps_to_total_duration():
    """Asking to mark up to 200 s of a 60 s recording fails fast."""
    with pytest.raises(ValueError):
        validate_range(0, 200_000, total_duration_ms=60_000)


def test_validate_range_ok_at_exact_total_duration():
    validate_range(0, 60_000, total_duration_ms=60_000)


# ---- has_overlap_with_existing ----------------------------------------


def test_has_overlap_true_for_obvious_overlap():
    existing = [Highlight(1000, 2000)]
    assert has_overlap_with_existing(Highlight(1500, 2500), existing)


def test_has_overlap_false_for_adjacent():
    existing = [Highlight(1000, 2000)]
    assert not has_overlap_with_existing(Highlight(2000, 3000), existing)


def test_has_overlap_false_for_empty_existing():
    assert not has_overlap_with_existing(Highlight(0, 1000), [])


# ---- add_highlight ----------------------------------------------------


def test_add_highlight_appends_and_returns():
    hs = HighlightSet()
    h = add_highlight(hs, 100, 500, title="t")
    assert len(hs.highlights) == 1
    assert hs.highlights[0] is h
    assert h.title == "t"


def test_add_highlight_rejects_overlap():
    hs = HighlightSet(highlights=[Highlight(0, 1000)])
    with pytest.raises(ValueError, match="overlap"):
        add_highlight(hs, 500, 1500)


def test_add_highlight_rejects_invalid_range():
    hs = HighlightSet()
    with pytest.raises(ValueError):
        add_highlight(hs, -1, 100)


def test_add_highlight_total_duration_clamp():
    hs = HighlightSet()
    with pytest.raises(ValueError):
        add_highlight(hs, 0, 100_000, total_duration_ms=50_000)


# ---- remove_highlight -------------------------------------------------


def test_remove_highlight_returns_true_on_hit():
    h = Highlight(0, 1000, "x")
    hs = HighlightSet(highlights=[h])
    assert remove_highlight(hs, h) is True
    assert hs.highlights == []


def test_remove_highlight_returns_false_when_missing():
    hs = HighlightSet(highlights=[Highlight(0, 1000)])
    assert remove_highlight(hs, Highlight(2000, 3000)) is False


# ---- update_highlight_title -------------------------------------------


def test_update_highlight_title_replaces_in_place():
    h = Highlight(0, 1000)
    hs = HighlightSet(highlights=[h])
    updated = update_highlight_title(hs, h, "new title")
    assert updated is not None and updated.title == "new title"
    # The frozen dataclass means a new instance is stored.
    assert hs.highlights[0].title == "new title"


def test_update_highlight_title_returns_none_when_missing():
    hs = HighlightSet()
    assert update_highlight_title(hs, Highlight(0, 1000), "x") is None


# ---- update_highlight_range -------------------------------------------


def test_update_highlight_range_moves_boundaries():
    h = Highlight(0, 1000, "x")
    hs = HighlightSet(highlights=[h, Highlight(5000, 6000)])
    updated = update_highlight_range(hs, h, 500, 1500)
    assert updated.start_ms == 500
    assert updated.end_ms == 1500
    assert updated.title == "x"   # title preserved across range edit


def test_update_highlight_range_rejects_overlap_with_others():
    h = Highlight(0, 1000)
    hs = HighlightSet(highlights=[h, Highlight(2000, 3000)])
    with pytest.raises(ValueError, match="overlap"):
        update_highlight_range(hs, h, 0, 2500)


def test_update_highlight_range_allows_resize_into_own_old_extent():
    """Shrinking / extending a highlight without crossing into a
    neighbor should always work. The self-overlap exclusion logic
    is the load-bearing piece here."""
    h = Highlight(0, 1000)
    hs = HighlightSet(highlights=[h, Highlight(2000, 3000)])
    updated = update_highlight_range(hs, h, 0, 500)  # shrink to half
    assert updated.duration_ms() == 500


# ---- HighlightsStore --------------------------------------------------


def test_store_load_returns_empty_when_file_missing(tmp_path, monkeypatch):
    """Brand-new session has no highlights.json -- load() must return
    an empty HighlightSet, not raise."""
    monkeypatch.setenv("MEETING_NOTETAKER_DATA_DIR", str(tmp_path / "mn"))
    store = HighlightsStore("sess-a")
    hs = store.load()
    assert hs.highlights == []


def test_store_load_returns_empty_on_corrupt_json(tmp_path, monkeypatch):
    """A hand-edited highlights.json that's no longer valid JSON
    must not crash the session view."""
    monkeypatch.setenv("MEETING_NOTETAKER_DATA_DIR", str(tmp_path / "mn"))
    store = HighlightsStore("sess-a")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{this is not json", encoding="utf-8")
    assert store.load().highlights == []


def test_store_save_then_load_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTETAKER_DATA_DIR", str(tmp_path / "mn"))
    store = HighlightsStore("sess-a")
    hs = HighlightSet(highlights=[
        Highlight(0, 1000, "Title A"),
        Highlight(2000, 3500, "Title B"),
    ])
    store.save(hs)
    loaded = store.load()
    assert [
        (h.start_ms, h.end_ms, h.title) for h in loaded.highlights
    ] == [
        (0, 1000, "Title A"),
        (2000, 3500, "Title B"),
    ]


def test_store_save_writes_indented_json(tmp_path, monkeypatch):
    """File stays human-readable for hand-inspection."""
    monkeypatch.setenv("MEETING_NOTETAKER_DATA_DIR", str(tmp_path / "mn"))
    store = HighlightsStore("sess-a")
    store.save(HighlightSet(highlights=[Highlight(0, 1000)]))
    text = store.path.read_text(encoding="utf-8")
    data = json.loads(text)
    assert "\n" in text   # pretty-printed
    assert isinstance(data, dict) and "highlights" in data


def test_store_load_filters_invalid_entries(tmp_path, monkeypatch):
    """Negative or inverted entries in a hand-edited file get
    silently dropped -- load() only returns usable highlights."""
    monkeypatch.setenv("MEETING_NOTETAKER_DATA_DIR", str(tmp_path / "mn"))
    store = HighlightsStore("sess-a")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps({"highlights": [
            {"start_ms": 100, "end_ms": 500, "title": "ok"},
            {"start_ms": -5, "end_ms": 500},
            {"start_ms": 1000, "end_ms": 500, "title": "inverted"},
            {"junk": True},
            {"start_ms": 2000, "end_ms": 3000, "title": "also ok"},
        ]}),
        encoding="utf-8",
    )
    titles = [h.title for h in store.load().highlights]
    assert titles == ["ok", "also ok"]


def test_store_delete_all_removes_file(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTETAKER_DATA_DIR", str(tmp_path / "mn"))
    store = HighlightsStore("sess-a")
    store.save(HighlightSet(highlights=[Highlight(0, 1000)]))
    assert store.path.exists()
    store.delete_all()
    assert not store.path.exists() or store.load().highlights == []
