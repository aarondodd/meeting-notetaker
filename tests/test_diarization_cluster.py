"""Cosine clustering correctness."""
from __future__ import annotations

import numpy as np
import pytest

from meeting_notetaker.diarization.cluster import (
    cluster_segments,
    compute_centroid,
    cosine_similarity,
)


def _unit_vec(*components: float) -> np.ndarray:
    arr = np.asarray(components, dtype=np.float32)
    return arr / np.linalg.norm(arr)


def test_empty_input():
    result = cluster_segments([])
    assert result.labels == []
    assert result.centroids == []
    assert result.cluster_count() == 0


def test_single_segment_one_cluster():
    result = cluster_segments([_unit_vec(1.0, 0.0)])
    assert result.cluster_count() == 1
    assert result.labels == [0]


def test_two_close_embeddings_merge():
    a = _unit_vec(1.0, 0.0, 0.0)
    b = _unit_vec(0.98, 0.2, 0.0)
    result = cluster_segments([a, b], merge_threshold=0.75)
    assert result.cluster_count() == 1
    assert result.labels == [0, 0]


def test_two_far_embeddings_stay_separate():
    a = _unit_vec(1.0, 0.0, 0.0)
    b = _unit_vec(0.0, 1.0, 0.0)
    result = cluster_segments([a, b], merge_threshold=0.75)
    assert result.cluster_count() == 2
    assert result.labels == [0, 1]


def test_three_close_one_far():
    # Three "Alice" embeddings near each other, one "Bob" far away.
    alice_1 = _unit_vec(1.0, 0.0, 0.0)
    alice_2 = _unit_vec(0.95, 0.3, 0.0)
    alice_3 = _unit_vec(0.97, 0.25, 0.05)
    bob_1 = _unit_vec(0.0, 0.0, 1.0)
    result = cluster_segments([alice_1, alice_2, bob_1, alice_3], merge_threshold=0.75)
    assert result.cluster_count() == 2
    # The 3 Alice embeddings share a cluster, the 1 Bob is alone.
    alice_label = result.labels[0]
    bob_label = result.labels[2]
    assert alice_label != bob_label
    assert result.labels == [alice_label, alice_label, bob_label, alice_label]


def test_threshold_controls_merge_aggressiveness():
    a = _unit_vec(1.0, 0.0)
    b = _unit_vec(0.7, 0.7)
    # cosine similarity is ~0.707
    high_threshold = cluster_segments([a, b], merge_threshold=0.9)
    low_threshold = cluster_segments([a, b], merge_threshold=0.5)
    assert high_threshold.cluster_count() == 2
    assert low_threshold.cluster_count() == 1


def test_centroid_is_weighted_average_of_members():
    a = _unit_vec(1.0, 0.0)
    b = _unit_vec(0.95, 0.3)
    c = _unit_vec(0.99, 0.1)
    result = cluster_segments([a, b, c], merge_threshold=0.75)
    assert result.cluster_count() == 1
    centroid = result.centroids[0]
    expected = (a + b + c) / 3.0
    # Allow small numerical drift from sequential weighted merges.
    assert np.allclose(centroid, expected, atol=0.05)


def test_cosine_similarity_zero_vector():
    assert cosine_similarity(np.zeros(3), np.array([1.0, 0.0, 0.0])) == 0.0
    assert cosine_similarity(np.zeros(3), np.zeros(3)) == 0.0


def test_compute_centroid_simple():
    a = _unit_vec(1.0, 0.0)
    b = _unit_vec(0.0, 1.0)
    centroid = compute_centroid([a, b])
    expected = (a + b) / 2.0
    assert np.allclose(centroid, expected)


def test_compute_centroid_empty_raises():
    with pytest.raises(ValueError):
        compute_centroid([])
