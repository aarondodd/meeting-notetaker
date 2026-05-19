"""Speaker identity store: CRUD, running average, matching."""
from __future__ import annotations

import numpy as np
import pytest

from meeting_notetaker.diarization.store import SpeakerStore


def _unit_vec(*components: float) -> np.ndarray:
    arr = np.asarray(components, dtype=np.float32)
    return arr / np.linalg.norm(arr)


@pytest.fixture
def store(tmp_path):
    s = SpeakerStore(tmp_path / "speakers.db")
    yield s
    s.close()


def test_empty_store_lists_nothing(store):
    assert store.list_all() == []


def test_get_by_name_missing_returns_none(store):
    assert store.get_by_name("Nobody") is None


def test_upsert_new_speaker(store):
    emb = _unit_vec(1.0, 0.0, 0.0)
    record = store.upsert("Alice", emb)
    assert record.name == "Alice"
    assert record.sample_count == 1
    assert np.allclose(record.embedding, emb)
    assert record.created_at == record.last_seen_at


def test_upsert_replaces_existing(store):
    a = _unit_vec(1.0, 0.0, 0.0)
    b = _unit_vec(0.0, 1.0, 0.0)
    first = store.upsert("Alice", a)
    second = store.upsert("Alice", b, sample_count=5)
    assert first.id == second.id
    assert np.allclose(second.embedding, b)
    assert second.sample_count == 5


def test_add_sample_running_average(store):
    a = _unit_vec(1.0, 0.0)
    b = _unit_vec(0.0, 1.0)
    record_1 = store.upsert("Alice", a)
    record_2 = store.add_sample("Alice", b)
    # Sample count grew by 1.
    assert record_2.sample_count == 2
    # New centroid is the weighted average (1 * a + 1 * b) / 2 = (0.5, 0.5).
    expected = (a + b) / 2.0
    assert np.allclose(record_2.embedding, expected, atol=1e-6)


def test_add_sample_creates_speaker_when_missing(store):
    a = _unit_vec(1.0, 0.0)
    record = store.add_sample("Bob", a)
    assert record.sample_count == 1
    assert np.allclose(record.embedding, a)


def test_rename(store):
    store.upsert("OldName", _unit_vec(1.0, 0.0))
    assert store.rename("OldName", "NewName") is True
    assert store.get_by_name("OldName") is None
    assert store.get_by_name("NewName") is not None
    # Rename of nonexistent returns False.
    assert store.rename("Ghost", "Phantom") is False


def test_forget(store):
    store.upsert("Alice", _unit_vec(1.0, 0.0))
    store.upsert("Bob", _unit_vec(0.0, 1.0))
    assert store.forget("Alice") is True
    assert store.get_by_name("Alice") is None
    assert store.get_by_name("Bob") is not None
    assert store.forget("Alice") is False  # already gone


def test_forget_all(store):
    store.upsert("A", _unit_vec(1.0, 0.0))
    store.upsert("B", _unit_vec(0.0, 1.0))
    count = store.forget_all()
    assert count == 2
    assert store.list_all() == []


def test_match_finds_closest_above_threshold(store):
    store.upsert("Alice", _unit_vec(1.0, 0.0, 0.0))
    store.upsert("Bob", _unit_vec(0.0, 1.0, 0.0))
    query = _unit_vec(0.95, 0.1, 0.0)
    match = store.match(query, threshold=0.7)
    assert match is not None
    assert match.speaker.name == "Alice"
    assert match.similarity > 0.9


def test_match_returns_none_below_threshold(store):
    store.upsert("Alice", _unit_vec(1.0, 0.0, 0.0))
    query = _unit_vec(0.0, 1.0, 0.0)
    match = store.match(query, threshold=0.75)
    assert match is None


def test_match_with_empty_store(store):
    query = _unit_vec(1.0, 0.0, 0.0)
    assert store.match(query) is None


def test_match_skips_records_with_mismatched_dim(store):
    """Library entries stored under a previous encoder (different dim)
    must be silently skipped, not crash the matcher. This is the
    behavior that lets a v0.4 install upgraded to v0.5 keep its old
    Resemblyzer 256-dim entries on disk while the new ECAPA 192-dim
    encoder builds a parallel library; cross-dim entries just never
    win a match."""
    # Stored speaker: 4 dims (simulating "old encoder").
    store.upsert("OldEncoder", np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
    # Query: 3 dims (simulating "new encoder").
    query = _unit_vec(1.0, 0.0, 0.0)
    assert store.match(query, threshold=0.0) is None
    # Add a new-dim entry that should win.
    store.upsert("NewEncoder", _unit_vec(1.0, 0.0, 0.0))
    result = store.match(query, threshold=0.0)
    assert result is not None
    assert result.speaker.name == "NewEncoder"


def test_list_all_sorted_by_last_seen(store):
    import time

    store.upsert("First", _unit_vec(1.0, 0.0))
    time.sleep(1.01)  # one-second timestamp resolution
    store.upsert("Second", _unit_vec(0.0, 1.0))
    records = store.list_all()
    # Most recent first.
    assert [r.name for r in records[:2]] == ["Second", "First"]


def test_context_manager_closes_connection(tmp_path):
    db = tmp_path / "ctx.db"
    with SpeakerStore(db) as s:
        s.upsert("Alice", _unit_vec(1.0, 0.0))
    # After close, the next open should still find Alice.
    with SpeakerStore(db) as s2:
        assert s2.get_by_name("Alice") is not None


def test_embedding_round_trip_preserves_shape_and_values(store):
    emb = np.random.RandomState(42).randn(256).astype(np.float32)
    store.upsert("Alice", emb)
    loaded = store.get_by_name("Alice")
    assert loaded is not None
    assert loaded.embedding.shape == (256,)
    assert np.allclose(loaded.embedding, emb)


def test_embedding_dimension_mismatch_raises(tmp_path):
    """Defensive: a tampered DB row should refuse to decode silently."""
    s = SpeakerStore(tmp_path / "tampered.db")
    s.upsert("Alice", _unit_vec(1.0, 0.0))
    # Direct sqlite tamper: shrink the embedding bytes but leave dim at 2.
    conn = s._connect()
    conn.execute(
        "UPDATE speakers SET embedding = ? WHERE name = 'Alice'",
        (b"\x00\x00",),  # only 2 bytes -> half a float32
    )
    conn.commit()
    with pytest.raises(ValueError):
        s.get_by_name("Alice")
    s.close()
