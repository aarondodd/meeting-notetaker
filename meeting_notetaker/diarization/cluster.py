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

from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass(frozen=True)
class ClusterAssignment:
    """Per-segment cluster id + the cluster centroid lookup.

    `labels[i]` is the cluster id (0-based) assigned to segment i.
    `centroids[k]` is the embedding centroid for cluster k.
    `names[k]` is the user-supplied anchor name for cluster k when
    constrained clustering was used, else None. This is the side
    channel the refiner uses to skip the store match for clusters the
    user explicitly tagged during recording.
    """

    labels: list[int]
    centroids: list[np.ndarray]
    names: list[Optional[str]] = field(default_factory=list)

    def cluster_count(self) -> int:
        return len(self.centroids)

    def segments_for(self, cluster_id: int) -> list[int]:
        return [i for i, c in enumerate(self.labels) if c == cluster_id]

    def name_for(self, cluster_id: int) -> Optional[str]:
        if 0 <= cluster_id < len(self.names):
            return self.names[cluster_id]
        return None


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
    labels: Optional[dict[int, str]] = None,
) -> ClusterAssignment:
    """Greedy agglomerative cosine clustering, optionally constrained by
    user-supplied anchor labels.

    Each segment starts as its own cluster. The two clusters with the
    highest centroid-cosine similarity get merged, weighted by the number
    of segments in each (so centroid drifts toward the larger cluster).
    Repeats until no remaining pair is above `merge_threshold`.

    When `labels` is provided (mapping segment-index -> name), the
    clustering becomes constrained:

    - **must-link:** segments sharing the same name are forced to
      end up in the same cluster (merged regardless of similarity).
    - **cannot-link:** segments with different names are forbidden
      from merging, regardless of similarity.

    Same-name merges happen first (the constraint is non-negotiable),
    then the unsupervised pass runs the similarity-driven merges,
    respecting the cannot-link constraint. Unlabeled segments behave
    exactly as in the unsupervised case.

    Returns a ClusterAssignment whose `names[k]` field carries the
    anchor name for each cluster (or None for unlabeled clusters).

    Empty input -> empty assignment.
    """
    if not embeddings:
        return ClusterAssignment(labels=[], centroids=[], names=[])
    label_map = dict(labels or {})
    # Validate label indices: silently drop any out-of-range entries.
    label_map = {i: n for i, n in label_map.items() if 0 <= i < len(embeddings)}

    # Per-cluster carry-forward state. Each cluster starts as a single
    # segment; cluster_name[c] tracks whichever anchor name the cluster
    # currently carries (None if no anchor yet).
    centroids: list[np.ndarray] = [emb.astype(np.float32).copy() for emb in embeddings]
    weights: list[int] = [1] * len(embeddings)
    cluster_labels = list(range(len(embeddings)))
    cluster_name: list[Optional[str]] = [
        label_map.get(i) for i in range(len(embeddings))
    ]

    def _merge(ci: int, cj: int) -> None:
        """Merge cluster cj into ci, weighted-averaging the centroid."""
        new_w = weights[ci] + weights[cj]
        centroids[ci] = (
            centroids[ci] * weights[ci] + centroids[cj] * weights[cj]
        ) / new_w
        weights[ci] = new_w
        # Inherit a name from whichever side has one; both having names
        # only happens for same-name merges (the constraint loop forces
        # that), so the assignment is unambiguous.
        if cluster_name[ci] is None and cluster_name[cj] is not None:
            cluster_name[ci] = cluster_name[cj]
        for k, lbl in enumerate(cluster_labels):
            if lbl == cj:
                cluster_labels[k] = ci
        weights[cj] = 0
        cluster_name[cj] = None

    # ---- Phase 1: must-link merges. ----
    if label_map:
        # Group segments by name and collapse each group into a single
        # cluster. The first segment in each group is the seed; later
        # segments merge into it.
        groups: dict[str, list[int]] = {}
        for seg_idx, name in label_map.items():
            groups.setdefault(name, []).append(seg_idx)
        for name, members in groups.items():
            if len(members) < 2:
                continue
            seed = members[0]
            for other in members[1:]:
                # `other` may have already been merged into `seed`
                # transitively in this loop; check the active label.
                if cluster_labels[other] == cluster_labels[seed]:
                    continue
                _merge(cluster_labels[seed], cluster_labels[other])

    # ---- Phase 2: unsupervised greedy merges, respecting cannot-link. ----
    while True:
        active = sorted({lbl for lbl in cluster_labels})
        if len(active) < 2:
            break
        best = (-1.0, -1, -1)
        for i_idx, ci in enumerate(active):
            for cj in active[i_idx + 1:]:
                # Cannot-link: if both clusters carry an anchor name and
                # the names differ, refuse this pair regardless of sim.
                ni, nj = cluster_name[ci], cluster_name[cj]
                if ni is not None and nj is not None and ni != nj:
                    continue
                sim = cosine_similarity(centroids[ci], centroids[cj])
                if sim > best[0]:
                    best = (sim, ci, cj)
        sim, ci, cj = best
        if sim < merge_threshold or ci < 0:
            break
        _merge(ci, cj)

    # ---- Compact cluster ids: remap to 0..K-1 in first-appearance order. ----
    remap: dict[int, int] = {}
    next_id = 0
    compact_labels: list[int] = []
    for lbl in cluster_labels:
        if lbl not in remap:
            remap[lbl] = next_id
            next_id += 1
        compact_labels.append(remap[lbl])

    compact_centroids: list[np.ndarray] = [np.zeros_like(embeddings[0])] * next_id
    compact_names: list[Optional[str]] = [None] * next_id
    for old, new in remap.items():
        compact_centroids[new] = centroids[old]
        compact_names[new] = cluster_name[old]

    return ClusterAssignment(
        labels=compact_labels,
        centroids=compact_centroids,
        names=compact_names,
    )


def compute_centroid(embeddings: list[np.ndarray]) -> np.ndarray:
    """Average embeddings together. Returns a zero-norm vector for empty input."""
    if not embeddings:
        raise ValueError("cannot compute centroid of empty list")
    stacked = np.stack([e.astype(np.float32) for e in embeddings], axis=0)
    return stacked.mean(axis=0)
