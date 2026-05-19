"""Agglomerative cosine clustering of speaker embeddings.

The segmenter gives us N voiced turns; the embedder gives one fixed-length
vector per turn. This module groups those vectors into anonymous clusters
(Speaker 1 / Speaker 2 / ...) using cosine similarity.

Algorithm: greedy nearest-neighbor agglomerative clustering. We seed with
each segment as its own cluster, then repeatedly merge the two closest
clusters (by centroid cosine similarity) as long as the similarity is
above `merge_threshold`. Stops when no two clusters are similar enough.

For a typical meeting (10-200 turns) this is fast enough (~ms) that we
don't need k-means or HDBSCAN. The threshold is the only knob worth tuning;
default 0.75 is a reasonable starting point for ECAPA-TDNN embeddings,
which typically place same-speaker pairs at cosine similarity 0.7-0.9 and
different-speaker pairs at 0.1-0.3.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ClusterAssignment:
    """Per-segment cluster id + the cluster centroid lookup.

    `labels[i]` is the cluster id (0-based) assigned to segment i.
    `centroids[k]` is the embedding centroid for cluster k.
    """

    labels: list[int]
    centroids: list[np.ndarray]

    def cluster_count(self) -> int:
        return len(self.centroids)

    def segments_for(self, cluster_id: int) -> list[int]:
        return [i for i, c in enumerate(self.labels) if c == cluster_id]


def _l2_normalize(x: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(x)
    if norm < 1e-8:
        return x
    return x / norm


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two vectors. Returns 0 for zero vectors."""
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na < 1e-8 or nb < 1e-8:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def cluster_segments(
    embeddings: list[np.ndarray],
    *,
    merge_threshold: float = 0.75,
) -> ClusterAssignment:
    """Greedy agglomerative cosine clustering.

    Each segment starts as its own cluster. The two clusters with the
    highest centroid-cosine similarity get merged, weighted by the number
    of segments in each (so centroid drifts toward the larger cluster).
    Repeats until no remaining pair is above `merge_threshold`.

    Empty input -> empty assignment.
    """
    if not embeddings:
        return ClusterAssignment(labels=[], centroids=[])
    # Start: one cluster per segment.
    centroids: list[np.ndarray] = [emb.astype(np.float32).copy() for emb in embeddings]
    weights: list[int] = [1] * len(embeddings)
    labels = list(range(len(embeddings)))

    while True:
        # Find the pair of *current* clusters with the highest similarity.
        active = sorted({label for label in labels})
        if len(active) < 2:
            break
        best = (-1.0, -1, -1)
        for i_idx, ci in enumerate(active):
            for cj in active[i_idx + 1:]:
                sim = cosine_similarity(centroids[ci], centroids[cj])
                if sim > best[0]:
                    best = (sim, ci, cj)
        sim, ci, cj = best
        if sim < merge_threshold:
            break
        # Merge cj into ci. Weighted-average centroid; relabel all members.
        new_w = weights[ci] + weights[cj]
        centroids[ci] = (centroids[ci] * weights[ci] + centroids[cj] * weights[cj]) / new_w
        weights[ci] = new_w
        for k, lbl in enumerate(labels):
            if lbl == cj:
                labels[k] = ci
        # Mark cj as inactive (won't appear in next `active` sweep).
        weights[cj] = 0

    # Compact cluster ids: remap to 0..K-1 in first-appearance order.
    remap: dict[int, int] = {}
    next_id = 0
    compact_labels: list[int] = []
    for lbl in labels:
        if lbl not in remap:
            remap[lbl] = next_id
            next_id += 1
        compact_labels.append(remap[lbl])

    compact_centroids: list[np.ndarray] = [np.zeros_like(embeddings[0])] * next_id
    for old, new in remap.items():
        compact_centroids[new] = centroids[old]

    return ClusterAssignment(labels=compact_labels, centroids=compact_centroids)


def compute_centroid(embeddings: list[np.ndarray]) -> np.ndarray:
    """Average embeddings together. Returns a zero-norm vector for empty input."""
    if not embeddings:
        raise ValueError("cannot compute centroid of empty list")
    stacked = np.stack([e.astype(np.float32) for e in embeddings], axis=0)
    return stacked.mean(axis=0)
