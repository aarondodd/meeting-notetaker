"""SpeakerStore Phase 2: contact_id column + set_contact_id +
list_unlinked + merge.

Pure-Python tests. Voice embeddings are stand-in numpy vectors --
the centroid math is exercised in test_diarization_cluster.py and
isn't repeated here.
"""
from __future__ import annotations

import numpy as np
import pytest

from meeting_notetaker.diarization.store import SpeakerStore


@pytest.fixture
def store(tmp_path):
    s = SpeakerStore(tmp_path / "speakers.db")
    with s:
        yield s


def _emb(seed: int, dim: int = 192) -> np.ndarray:
    """Reproducible random embedding so different test calls don't
    accidentally land on identical centroids."""
    rng = np.random.default_rng(seed)
    return rng.normal(size=dim).astype(np.float32)


# ---- contact_id column / set + list ----


def test_set_contact_id_links_existing_speaker(store):
    store.upsert("Alice", _emb(1))
    assert store.set_contact_id("Alice", 42) is True
    rec = store.get_by_name("Alice")
    assert rec.contact_id == 42


def test_set_contact_id_unlinks_with_none(store):
    store.upsert("Alice", _emb(1))
    store.set_contact_id("Alice", 42)
    store.set_contact_id("Alice", None)
    assert store.get_by_name("Alice").contact_id is None


def test_set_contact_id_returns_false_when_speaker_unknown(store):
    assert store.set_contact_id("Ghost", 1) is False


def test_list_unlinked_returns_only_null_contact_id(store):
    store.upsert("Alice", _emb(1))
    store.upsert("Bob", _emb(2))
    store.set_contact_id("Alice", 1)
    unlinked = store.list_unlinked()
    names = [r.name for r in unlinked]
    assert names == ["Bob"]


def test_list_unlinked_empty_when_all_linked(store):
    store.upsert("Alice", _emb(1))
    store.set_contact_id("Alice", 1)
    assert store.list_unlinked() == []


def test_contact_id_survives_full_record_round_trip(store):
    store.upsert("Alice", _emb(1))
    store.set_contact_id("Alice", 99)
    # list_all reads back the column too.
    records = {r.name: r for r in store.list_all()}
    assert records["Alice"].contact_id == 99


# ---- migration via PRAGMA-checked ALTER ----


def test_ensure_contact_id_column_idempotent(store):
    """Closing + reopening the store must not double-add the column."""
    store.upsert("Alice", _emb(1))
    store.set_contact_id("Alice", 7)
    store.close()
    # Re-open via fresh instance against the same path.
    s2 = SpeakerStore(store.db_path)
    with s2:
        rec = s2.get_by_name("Alice")
        assert rec.contact_id == 7


# ---- merge ----


def test_merge_combines_sample_counts_and_centroids(store):
    a_emb = _emb(1)
    b_emb = _emb(2)
    store.upsert("Alice", a_emb, sample_count=2)
    store.upsert("Aly", b_emb, sample_count=3)
    merged = store.merge("Aly", "Alice")
    assert merged is not None
    # Sample count is the sum.
    assert merged.sample_count == 5
    # Source row gone.
    assert store.get_by_name("Aly") is None
    # Centroid is the weighted average (within float tolerance).
    expected = (a_emb * 2 + b_emb * 3) / 5
    assert np.allclose(merged.embedding, expected, atol=1e-5)


def test_merge_drops_source_and_keeps_target_name(store):
    store.upsert("Aly", _emb(1))
    store.upsert("Alice", _emb(2))
    store.merge("Aly", "Alice")
    names = [r.name for r in store.list_all()]
    assert "Alice" in names
    assert "Aly" not in names


def test_merge_preserves_target_contact_id_if_set(store):
    store.upsert("Aly", _emb(1))
    store.upsert("Alice", _emb(2))
    store.set_contact_id("Alice", 42)
    store.merge("Aly", "Alice")
    assert store.get_by_name("Alice").contact_id == 42


def test_merge_falls_back_to_source_contact_id_when_target_unlinked(store):
    store.upsert("Aly", _emb(1))
    store.upsert("Alice", _emb(2))
    store.set_contact_id("Aly", 7)  # source linked, target not
    store.merge("Aly", "Alice")
    assert store.get_by_name("Alice").contact_id == 7


def test_merge_self_is_noop(store):
    rec = store.upsert("Alice", _emb(1))
    result = store.merge("Alice", "Alice")
    assert result is not None
    assert result.id == rec.id


def test_merge_returns_none_when_either_missing(store):
    store.upsert("Alice", _emb(1))
    assert store.merge("Ghost", "Alice") is None
    assert store.merge("Alice", "Ghost") is None


def test_merge_rejects_mismatched_embedding_dims(store):
    store.upsert("Alice", _emb(1, dim=192))
    store.upsert("Aly", _emb(2, dim=256))
    with pytest.raises(ValueError, match="embedding dims"):
        store.merge("Aly", "Alice")


def test_merge_concatenates_notes(store):
    store.upsert("Aly", _emb(1), notes="Source notes")
    store.upsert("Alice", _emb(2), notes="Target notes")
    store.merge("Aly", "Alice")
    merged = store.get_by_name("Alice")
    assert "Source notes" in merged.notes
    assert "Target notes" in merged.notes
