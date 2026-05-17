"""diarization.json save / load / rename round-trip."""
from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest

from meeting_notetaker.diarization.persistence import (
    DiarizationData,
    load_diarization,
    save_diarization,
    update_cluster_name,
)
from meeting_notetaker.diarization.refiner import (
    ClusterSummary,
    RefinementResult,
    SegmentLabel,
)
from meeting_notetaker.diarization.segmenter import Turn
from meeting_notetaker.models.transcript import TranscriptSegment


def _fake_turn(t_start: float, t_end: float) -> Turn:
    return Turn(
        t_start=t_start,
        t_end=t_end,
        sample_rate=16000,
        pcm=np.zeros(int((t_end - t_start) * 16000), dtype=np.int16),
    )


@pytest.fixture
def refinement(tmp_path):
    centroid_alice = np.full(8, 0.5, dtype=np.float32)
    centroid_bob = np.full(8, -0.3, dtype=np.float32)
    turns = [_fake_turn(1.0, 2.5), _fake_turn(3.0, 4.0), _fake_turn(5.0, 6.5)]
    clusters = [
        ClusterSummary(
            cluster_id=0,
            centroid=centroid_alice,
            turn_indices=[0, 2],
            name="Alice",
            match_similarity=0.82,
        ),
        ClusterSummary(
            cluster_id=1,
            centroid=centroid_bob,
            turn_indices=[1],
            name=None,
            match_similarity=None,
        ),
    ]
    segment_labels = [
        SegmentLabel(segment_index=0, cluster_id=0, name="Alice", confidence=0.82),
        SegmentLabel(segment_index=1, cluster_id=1, name=None, confidence=None),
    ]
    return RefinementResult(
        turns=turns,
        clusters=clusters,
        segment_labels=segment_labels,
        unknown_cluster_ids=[1],
    )


@pytest.fixture
def transcript_segments():
    return [
        TranscriptSegment(source="sys", text="A", t_start=1.0, t_end=2.5),
        TranscriptSegment(source="sys", text="B", t_start=3.0, t_end=4.0),
    ]


def test_save_writes_expected_shape(tmp_path, refinement, transcript_segments):
    path = save_diarization(
        tmp_path,
        loopback_wav="audio/sys.wav",
        match_threshold=0.75,
        refinement_result=refinement,
        transcript_segments=transcript_segments,
    )
    raw = json.loads(path.read_text())
    assert raw["version"] == 1
    assert raw["loopback_wav"] == "audio/sys.wav"
    assert len(raw["clusters"]) == 2
    assert raw["clusters"][0]["name"] == "Alice"
    assert raw["clusters"][1]["name"] is None
    assert len(raw["segments"]) == 2


def test_load_round_trip(tmp_path, refinement, transcript_segments):
    save_diarization(
        tmp_path,
        loopback_wav="audio/sys.wav",
        match_threshold=0.8,
        refinement_result=refinement,
        transcript_segments=transcript_segments,
    )
    loaded = load_diarization(tmp_path)
    assert loaded is not None
    assert loaded.match_threshold == 0.8
    assert len(loaded.clusters) == 2
    assert loaded.clusters[0].name == "Alice"
    assert loaded.clusters[1].name is None
    # Centroid round-trips with float32 precision.
    assert np.allclose(loaded.clusters[0].centroid, refinement.clusters[0].centroid)
    assert len(loaded.segments) == 2


def test_load_missing_returns_none(tmp_path):
    assert load_diarization(tmp_path) is None


def test_update_cluster_name(tmp_path, refinement, transcript_segments):
    save_diarization(
        tmp_path,
        loopback_wav="audio/sys.wav",
        match_threshold=0.75,
        refinement_result=refinement,
        transcript_segments=transcript_segments,
    )
    assert update_cluster_name(tmp_path, cluster_id=1, new_name="Bob") is True
    loaded = load_diarization(tmp_path)
    assert loaded is not None
    assert loaded.clusters[1].name == "Bob"


def test_update_cluster_name_no_op_on_unknown(tmp_path, refinement, transcript_segments):
    save_diarization(
        tmp_path,
        loopback_wav="audio/sys.wav",
        match_threshold=0.75,
        refinement_result=refinement,
        transcript_segments=transcript_segments,
    )
    assert update_cluster_name(tmp_path, cluster_id=999, new_name="Whoever") is False


def test_update_cluster_name_no_file_returns_false(tmp_path):
    assert update_cluster_name(tmp_path, cluster_id=0, new_name="X") is False


def test_load_rejects_unsupported_version(tmp_path):
    # Write a future-version file.
    (tmp_path / "diarization.json").write_text(json.dumps({
        "version": 999,
        "refined_at": "2026-05-17T00:00:00Z",
        "loopback_wav": "audio/sys.wav",
        "match_threshold": 0.75,
        "clusters": [],
        "segments": [],
    }))
    with pytest.raises(ValueError):
        load_diarization(tmp_path)
