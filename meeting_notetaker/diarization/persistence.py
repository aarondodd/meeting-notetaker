"""Per-session diarization metadata persistence.

Lives at `<session_dir>/diarization.json` alongside raw.transcript.md.
Recording the cluster -> name mapping plus enough per-segment detail to
support the Review Speakers walker on a completed session without
re-running the refiner.

Schema (versioned for forward compat):

    {
      "version": 1,
      "refined_at": "<ISO-8601 UTC>",
      "loopback_wav": "audio/sys.wav",
      "match_threshold": 0.75,
      "clusters": [
        {
          "cluster_id": 0,
          "name": "Bob" | null,
          "match_similarity": 0.81 | null,
          "centroid": [256 floats],
          "sample_t_start": 12.3,
          "sample_t_end": 18.5
        }, ...
      ],
      "segments": [
        {"segment_index": 0, "cluster_id": 0, "t_start": 12.3, "t_end": 18.5}, ...
      ]
    }

We persist centroids (~8KB per cluster) so the Review walker can feed
corrections back to the speaker store: when the user reassigns a cluster
to a different name, we have the centroid to call `store.add_sample`
with. Sample audio is NOT persisted -- if retain_audio is set, the
loopback WAV is still on disk and the walker pulls fresh clips from it;
otherwise the walker shows text-only review (example transcript lines).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np


VERSION = 1


@dataclass
class PersistedCluster:
    cluster_id: int
    name: Optional[str]
    match_similarity: Optional[float]
    centroid: np.ndarray
    sample_t_start: float
    sample_t_end: float


@dataclass
class PersistedSegment:
    segment_index: int
    cluster_id: int
    t_start: float
    t_end: float


@dataclass
class DiarizationData:
    refined_at: str
    loopback_wav: str
    match_threshold: float
    clusters: list[PersistedCluster]
    segments: list[PersistedSegment]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def save_diarization(
    session_dir: Path,
    *,
    loopback_wav: str,
    match_threshold: float,
    refinement_result,
    transcript_segments,
) -> Path:
    """Persist a refiner.RefinementResult to <session_dir>/diarization.json.

    Sample t_start/t_end per cluster come from the first turn in that
    cluster (a representative slice the UI can replay if the WAV exists).
    """
    target = Path(session_dir) / "diarization.json"
    target.parent.mkdir(parents=True, exist_ok=True)

    clusters_payload = []
    for summary in refinement_result.clusters:
        if not summary.turn_indices:
            continue
        first_turn = refinement_result.turns[summary.turn_indices[0]]
        clusters_payload.append({
            "cluster_id": summary.cluster_id,
            "name": summary.name,
            "match_similarity": summary.match_similarity,
            "centroid": [float(x) for x in summary.centroid.tolist()],
            "sample_t_start": float(first_turn.t_start),
            "sample_t_end": float(first_turn.t_end),
        })

    segments_payload = []
    for label in refinement_result.segment_labels:
        if label.cluster_id is None:
            continue
        seg = transcript_segments[label.segment_index]
        segments_payload.append({
            "segment_index": label.segment_index,
            "cluster_id": label.cluster_id,
            "t_start": float(seg.t_start),
            "t_end": float(seg.t_end),
        })

    payload = {
        "version": VERSION,
        "refined_at": _now_iso(),
        "loopback_wav": loopback_wav,
        "match_threshold": match_threshold,
        "clusters": clusters_payload,
        "segments": segments_payload,
    }
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return target


def load_diarization(session_dir: Path) -> Optional[DiarizationData]:
    """Return the diarization metadata for a session, or None if missing.

    Tolerates older versions by extending defaults; raises only on
    malformed JSON or a wholly unknown version number.
    """
    path = Path(session_dir) / "diarization.json"
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    version = int(raw.get("version", 1))
    if version > VERSION:
        raise ValueError(
            f"diarization.json version {version} not understood "
            f"(this build supports up to {VERSION})"
        )
    clusters = [
        PersistedCluster(
            cluster_id=int(c["cluster_id"]),
            name=c.get("name"),
            match_similarity=c.get("match_similarity"),
            centroid=np.asarray(c["centroid"], dtype=np.float32),
            sample_t_start=float(c["sample_t_start"]),
            sample_t_end=float(c["sample_t_end"]),
        )
        for c in raw.get("clusters", [])
    ]
    segments = [
        PersistedSegment(
            segment_index=int(s["segment_index"]),
            cluster_id=int(s["cluster_id"]),
            t_start=float(s["t_start"]),
            t_end=float(s["t_end"]),
        )
        for s in raw.get("segments", [])
    ]
    return DiarizationData(
        refined_at=str(raw.get("refined_at", "")),
        loopback_wav=str(raw.get("loopback_wav", "")),
        match_threshold=float(raw.get("match_threshold", 0.75)),
        clusters=clusters,
        segments=segments,
    )


def update_cluster_name(
    session_dir: Path,
    cluster_id: int,
    new_name: Optional[str],
) -> bool:
    """Rename one cluster in the persisted file. Returns True if updated."""
    path = Path(session_dir) / "diarization.json"
    if not path.exists():
        return False
    raw = json.loads(path.read_text(encoding="utf-8"))
    changed = False
    for c in raw.get("clusters", []):
        if int(c["cluster_id"]) == cluster_id:
            c["name"] = new_name
            changed = True
            break
    if changed:
        path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    return changed
