"""Adapter tests: refinement / persistence -> SpeakerWalkerEntry."""
from __future__ import annotations

import numpy as np
import pytest

from meeting_notetaker.diarization.persistence import (
    DiarizationData,
    PersistedCluster,
    PersistedSegment,
)
from meeting_notetaker.diarization.refiner import (
    ClusterSummary,
    RefinementResult,
    SegmentLabel,
)
from meeting_notetaker.models.transcript import TranscriptSegment
from meeting_notetaker.ui.speaker_walker_helpers import (
    entries_from_persistence,
    entries_from_refinement,
    gather_suggestions,
)


def _fake_centroid(value: float, dim: int = 8) -> np.ndarray:
    return np.full(dim, value, dtype=np.float32)


def _seg(source: str, text: str, idx: int) -> TranscriptSegment:
    return TranscriptSegment(
        source=source, text=text, t_start=float(idx), t_end=float(idx) + 1.0,
    )


@pytest.fixture
def two_cluster_refinement():
    clusters = [
        ClusterSummary(
            cluster_id=0, centroid=_fake_centroid(0.5),
            turn_indices=[0], name="Alice", match_similarity=0.88,
        ),
        ClusterSummary(
            cluster_id=1, centroid=_fake_centroid(-0.3),
            turn_indices=[1], name=None, match_similarity=None,
        ),
    ]
    segment_labels = [
        SegmentLabel(segment_index=0, cluster_id=0, name="Alice", confidence=0.88),
        SegmentLabel(segment_index=1, cluster_id=1, name=None, confidence=None),
        SegmentLabel(segment_index=2, cluster_id=0, name="Alice", confidence=0.88),
    ]
    return RefinementResult(
        turns=[],
        clusters=clusters,
        segment_labels=segment_labels,
        unknown_cluster_ids=[1],
    )


def test_entries_from_refinement_filters_to_unknowns_by_default(two_cluster_refinement):
    segments = [
        _seg("sys", "Hi there",  0),
        _seg("sys", "Hello back", 1),
        _seg("sys", "More chat",  2),
    ]
    entries = entries_from_refinement(two_cluster_refinement, segments)
    assert len(entries) == 1
    assert entries[0].cluster_id == 1
    assert entries[0].current_name is None


def test_entries_from_refinement_includes_known_when_unknown_only_false(two_cluster_refinement):
    segments = [_seg("sys", "Hi", 0), _seg("sys", "Hello", 1), _seg("sys", "More", 2)]
    entries = entries_from_refinement(
        two_cluster_refinement, segments, only_unknown=False
    )
    assert len(entries) == 2
    assert {e.cluster_id for e in entries} == {0, 1}


def test_entries_carry_example_lines(two_cluster_refinement):
    segments = [
        _seg("sys", "This is a longish line about alpha topic that should win",  0),
        _seg("sys", "Brief",                                                     1),
        _seg("sys", "Another long line from the same speaker about something",    2),
    ]
    entries = entries_from_refinement(
        two_cluster_refinement, segments, only_unknown=False
    )
    alice = next(e for e in entries if e.current_name == "Alice")
    # Alice's two segments should both appear; the longer one first.
    assert any("alpha topic" in line for line in alice.example_lines)
    assert any("Another long line" in line for line in alice.example_lines)


def test_entries_from_persistence():
    data = DiarizationData(
        refined_at="2026-05-17T00:00:00Z",
        loopback_wav="audio/sys.wav",
        match_threshold=0.75,
        clusters=[
            PersistedCluster(
                cluster_id=0, name="Bob", match_similarity=0.81,
                centroid=_fake_centroid(0.5), sample_t_start=1.0, sample_t_end=3.0,
            ),
            PersistedCluster(
                cluster_id=1, name=None, match_similarity=None,
                centroid=_fake_centroid(-0.5), sample_t_start=4.0, sample_t_end=6.0,
            ),
        ],
        segments=[
            PersistedSegment(segment_index=0, cluster_id=0, t_start=1.0, t_end=3.0),
            PersistedSegment(segment_index=1, cluster_id=1, t_start=4.0, t_end=6.0),
        ],
    )
    segments = [
        _seg("sys", "Bob speaks here",   0),
        _seg("sys", "Unknown speaks",     1),
    ]
    entries = entries_from_persistence(data, segments, only_unknown=False)
    assert len(entries) == 2
    bob = next(e for e in entries if e.current_name == "Bob")
    assert any("Bob speaks" in line for line in bob.example_lines)


def test_gather_suggestions_dedupes_case_insensitive():
    known = ["Alice", "Bob"]
    attendees = ["alice", "Carol", "Bob", ""]
    pool = gather_suggestions(known, attendees)
    # Order: known first, then attendees not already present.
    assert pool == ["Alice", "Bob", "Carol"]


def test_gather_suggestions_preserves_case_of_first_occurrence():
    pool = gather_suggestions(["aaron"], ["Aaron"])
    assert pool == ["aaron"]


def test_gather_suggestions_handles_empty_inputs():
    assert gather_suggestions([], []) == []
    assert gather_suggestions(["Alice"], []) == ["Alice"]
    assert gather_suggestions([], ["Bob"]) == ["Bob"]
