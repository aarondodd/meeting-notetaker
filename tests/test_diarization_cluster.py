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


# ---- Constrained clustering (must-link / cannot-link via labels) ----------


def test_labels_must_link_same_name_even_when_far_apart():
    """Two embeddings tagged with the same name end up in one cluster
    regardless of cosine distance."""
    a = _unit_vec(1.0, 0.0, 0.0)
    b = _unit_vec(0.0, 1.0, 0.0)  # would normally stay separate
    result = cluster_segments(
        [a, b],
        merge_threshold=0.75,
        labels={0: "Pat", 1: "Pat"},
    )
    assert result.cluster_count() == 1
    assert result.labels == [0, 0]
    assert result.name_for(0) == "Pat"


def test_labels_cannot_link_different_names_even_when_close():
    """Two near-identical embeddings tagged with different names stay
    in separate clusters."""
    a = _unit_vec(1.0, 0.0, 0.0)
    b = _unit_vec(0.99, 0.1, 0.0)  # would normally merge
    result = cluster_segments(
        [a, b],
        merge_threshold=0.75,
        labels={0: "Pat", 1: "Sam"},
    )
    assert result.cluster_count() == 2
    assert result.labels == [0, 1]
    names = {result.name_for(0), result.name_for(1)}
    assert names == {"Pat", "Sam"}


def test_unlabeled_segment_joins_named_cluster_by_similarity():
    """An unlabeled segment close to a Pat-tagged segment ends up in
    the Pat cluster and inherits the name."""
    pat_anchor = _unit_vec(1.0, 0.0, 0.0)
    pat_like = _unit_vec(0.98, 0.2, 0.0)  # close to anchor
    sam_anchor = _unit_vec(0.0, 0.0, 1.0)
    result = cluster_segments(
        [pat_anchor, pat_like, sam_anchor],
        merge_threshold=0.75,
        labels={0: "Pat", 2: "Sam"},
    )
    assert result.cluster_count() == 2
    pat_cluster = result.labels[0]
    sam_cluster = result.labels[2]
    assert pat_cluster != sam_cluster
    assert result.labels[1] == pat_cluster  # the unlabeled one joins Pat
    assert result.name_for(pat_cluster) == "Pat"
    assert result.name_for(sam_cluster) == "Sam"


def test_unlabeled_segment_blocked_from_named_cluster_when_far():
    """An unlabeled segment that's far from any named cluster forms its
    own cluster; the names side channel keeps it None."""
    pat_anchor = _unit_vec(1.0, 0.0, 0.0)
    unrelated = _unit_vec(0.0, 1.0, 0.0)
    result = cluster_segments(
        [pat_anchor, unrelated],
        merge_threshold=0.85,
        labels={0: "Pat"},
    )
    assert result.cluster_count() == 2
    assert result.name_for(result.labels[0]) == "Pat"
    assert result.name_for(result.labels[1]) is None


def test_no_labels_keeps_legacy_behavior():
    """Passing no labels (or empty) must reproduce the pre-constraint
    behavior exactly: names side channel is all None, cluster count
    matches the unsupervised case."""
    a = _unit_vec(1.0, 0.0)
    b = _unit_vec(0.99, 0.1)
    c = _unit_vec(0.0, 1.0)
    legacy = cluster_segments([a, b, c], merge_threshold=0.75)
    with_empty = cluster_segments([a, b, c], merge_threshold=0.75, labels={})
    assert legacy.labels == with_empty.labels
    assert all(n is None for n in with_empty.names)


def test_out_of_range_label_indices_silently_dropped():
    """Garbage label indices (negative / out of range) don't crash."""
    a = _unit_vec(1.0, 0.0)
    b = _unit_vec(0.99, 0.1)
    result = cluster_segments(
        [a, b],
        merge_threshold=0.75,
        labels={5: "Ghost", -1: "Negative", 0: "Pat"},
    )
    # Only the {0: "Pat"} label survived; segments still cluster together
    # by similarity (no cannot-link conflict).
    assert result.cluster_count() == 1
    assert result.name_for(0) == "Pat"
