"""End-to-end post-meeting speaker-identification pass.

Composes segmenter + embedder + clusterer + store into one entry point:

    result = refine_loopback(
        wav_path=session_dir/'audio'/'sys.wav',
        transcript_segments=[...TranscriptSegment, sys-source only...],
        speaker_store=open_speaker_store(),
        encoder=default_encoder,
    )

`result` carries the segment-by-segment speaker assignments and a list of
unknown clusters the UI can prompt the user to label. The caller (the
controller) rewrites raw.transcript.md from the labelled segments and
calls the Label Unknown Speakers dialog for any unmatched clusters.

This module is pure-Python; the encoder is the only SpeechBrain dependency
and it's pluggable (anything with an `embed_turn(turn) -> np.ndarray`
method works, which is what tests use).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Protocol

import numpy as np

from .cluster import ClusterAssignment, cluster_segments, compute_centroid, cosine_similarity
from .segmenter import Turn, find_turns_in_wav
from .store import MatchResult, SpeakerStore
from ..models.speaker_tags import SpeakerTag


log = logging.getLogger(__name__)


# Minimum turn duration that gets sent through clustering. Sub-second
# voiced regions (back-channel "yeah" / "mm-hm", coughs, brief overlap
# tails) produce noisy fixed-length embeddings -- the encoder doesn't
# have enough acoustic context to land them at a stable point in the
# 256-dim space. Including them pulls two real speakers' centroids
# together and shows up as cluster munging. They still get transcribed;
# they just don't drive diarization decisions. Transcript segments that
# only overlap dropped turns fall back to the source-default label
# (Them: for sys, Me: for mic), which is the same outcome as having no
# diarization data for that span.
MIN_TURN_DURATION_FOR_CLUSTERING_SEC = 1.0

# Max distance (seconds) between a user-supplied speaker tag and the
# nearest surviving turn for the tag to take effect. Captures the human
# click-after-hearing latency (people click ~1s after a speaker starts).
# Tags landing further than this from any turn are dropped silently --
# typically clicks that fell in silence or on filtered-out short turns.
DEFAULT_TAG_MATCH_TOLERANCE_SEC = 2.0


class _ProgressEmitter:
    """Throttled progress relay for refine_loopback.

    The underlying callback may end up wired to a Qt signal that crosses
    a thread boundary on every emission, so we only forward when the
    integer percent changes. Also clamps to [0, 100] and provides
    `sub_range(lo, hi)` to hand a callable to nested helpers that
    expects to emit 0..100 within its own work but should land in
    the outer bar's [lo, hi] band.
    """

    def __init__(self, cb: Optional[Callable[[int], None]]) -> None:
        self._cb = cb
        self._last: int = -1

    def set(self, pct: int) -> None:
        if self._cb is None:
            return
        pct = max(0, min(100, int(pct)))
        if pct == self._last:
            return
        self._last = pct
        try:
            self._cb(pct)
        except Exception:
            log.exception("refiner progress callback raised; suppressing")

    def sub_range(self, lo: int, hi: int) -> Optional[Callable[[int], None]]:
        if self._cb is None:
            return None
        lo = max(0, min(100, int(lo)))
        hi = max(lo, min(100, int(hi)))
        span = hi - lo

        def _scaled(inner_pct: int) -> None:
            inner_pct = max(0, min(100, int(inner_pct)))
            self.set(lo + int(round(span * inner_pct / 100)))

        return _scaled


class Embedder(Protocol):
    """Anything that can turn a Turn into a fixed-length vector."""

    def embed_turn(self, turn: Turn) -> np.ndarray: ...


@dataclass
class ClusterSummary:
    """One detected anonymous speaker cluster.

    `name` is None for unmatched clusters (UI must prompt the user) and
    set to the matched speaker's name for known clusters.
    `match_similarity` is the cosine similarity of the best match, or
    None if no match exceeded threshold.
    `turn_indices` indexes into the refinement's `turns` list -- handy
    for the UI to pull a sample audio clip from any turn.
    `was_user_tagged` is True when the cluster's name came from an
    in-meeting speaker tag (rather than a store match or being unknown).
    `tag_times_seconds` are the user-tag timestamps that landed in this
    cluster -- shown in the walker as a "tagged at HH:MM, ..." badge.
    """

    cluster_id: int
    centroid: np.ndarray
    turn_indices: list[int]
    name: Optional[str] = None
    match_similarity: Optional[float] = None
    was_user_tagged: bool = False
    tag_times_seconds: list[float] = field(default_factory=list)


@dataclass
class SegmentLabel:
    """Per-transcript-segment speaker assignment.

    `segment_index` is the position of the segment in the input list
    (interleaved or sys-only -- the caller decides). `cluster_id` is
    None when no diarization turn overlaps the segment (e.g. very
    short utterances filtered out by the segmenter). `name` mirrors
    the cluster's name at refinement time.
    """

    segment_index: int
    cluster_id: Optional[int]
    name: Optional[str]
    confidence: Optional[float]


@dataclass
class RefinementResult:
    turns: list[Turn]
    clusters: list[ClusterSummary]
    segment_labels: list[SegmentLabel]
    # Convenience for the UI: quick lookup of which clusters need labeling.
    unknown_cluster_ids: list[int] = field(default_factory=list)

    def has_unknown(self) -> bool:
        return bool(self.unknown_cluster_ids)


def refine_loopback(
    wav_path: Path,
    transcript_segments,
    *,
    speaker_store: SpeakerStore,
    encoder: Embedder,
    sys_source: str = "sys",
    mic_source: str = "mic",
    mic_wav: Optional[Path] = None,
    user_voiceprint: Optional[np.ndarray] = None,
    user_match_threshold: float = 0.7,
    match_threshold: float = 0.75,
    merge_threshold: float = 0.75,
    min_overlap_sec: float = 0.25,
    speaker_tags: Optional[list[SpeakerTag]] = None,
    tag_match_tolerance_sec: float = DEFAULT_TAG_MATCH_TOLERANCE_SEC,
    progress_cb: Optional[Callable[[int], None]] = None,
) -> RefinementResult:
    """Run the full pipeline.

    Returns a RefinementResult with per-segment labels and per-cluster
    summaries. System-audio segments get labeled against clusters derived
    from `wav_path`. When `mic_wav` and `user_voiceprint` are supplied,
    microphone-channel segments also get vetted: a mic turn that fails
    to match the voiceprint but overlaps a system-audio cluster turn is
    relabeled with that cluster's name -- the bleed case where the room
    mic picks up another speaker via speakers / headset bleed. Mic
    turns that match the voiceprint (or that don't overlap any sys
    cluster) keep the default "user" labeling via the source-aware
    rewrite pipeline.

    The caller is responsible for any side effects (rewriting
    raw.transcript.md, persisting decisions, prompting the user for
    unknown-cluster names). This function does not mutate the store.

    `progress_cb`, when supplied, is invoked with a percentage 0..100
    representing progress through the refinement phase only. The
    callback runs on whatever thread invoked refine_loopback (the
    QThread-driven Qt signal it usually wraps handles the cross-thread
    hop). Emissions are bucketed and throttled to 1% deltas:

        0-5%   find_turns_in_wav(sys.wav)
        5-80%  sys-turn embedding (per-turn updates)
        80-95% mic-bleed detection
        95-99% clustering + per-cluster summary
        100%   after the result is fully built
    """
    progress = _ProgressEmitter(progress_cb)
    progress.set(1)  # signal liveness immediately on entry

    all_turns = find_turns_in_wav(Path(wav_path))
    progress.set(5)
    if not all_turns:
        log.info("refiner: no turns detected in %s; nothing to label", wav_path)
        progress.set(100)
        return _empty_result(transcript_segments)

    turns = [t for t in all_turns if t.duration >= MIN_TURN_DURATION_FOR_CLUSTERING_SEC]
    dropped = len(all_turns) - len(turns)
    if dropped:
        log.info(
            "refiner: dropped %d/%d turns shorter than %.1fs from clustering",
            dropped, len(all_turns), MIN_TURN_DURATION_FOR_CLUSTERING_SEC,
        )
    if not turns:
        # Every voiced turn was too short to cluster reliably. Bail out
        # to the empty-result path; transcript still synthesizes normally
        # but no per-speaker attribution lands on disk.
        log.info("refiner: no turns >= %.1fs in %s; skipping diarization",
                 MIN_TURN_DURATION_FOR_CLUSTERING_SEC, wav_path)
        progress.set(100)
        return _empty_result(transcript_segments)

    embeddings: list[np.ndarray] = []
    sys_total = len(turns)
    for i, t in enumerate(turns):
        embeddings.append(np.asarray(encoder.embed_turn(t), dtype=np.float32))
        # Map (i+1)/sys_total -> 5..80 of the refinement budget.
        progress.set(5 + int(round(75 * (i + 1) / sys_total)))

    # Match user-supplied speaker tags (captured during recording) to the
    # nearest turn that survived the duration filter. The mapping feeds
    # the clusterer's must-link / cannot-link constraints AND the
    # per-cluster summary so the walker can show a "tagged at ..." badge.
    matched_tags = _match_tags_to_turns(
        speaker_tags or [], turns, tolerance_sec=tag_match_tolerance_sec
    )
    turn_labels = {turn_idx: name for turn_idx, (name, _ts) in matched_tags.items()}

    assignment = cluster_segments(
        embeddings, merge_threshold=merge_threshold, labels=turn_labels or None,
    )
    clusters = _summarize_clusters(
        assignment, turns, speaker_store, match_threshold,
        matched_tags=matched_tags,
    )
    progress.set(80)

    mic_bleed_assignments: dict[int, int] = {}
    if (
        mic_wav is not None
        and Path(mic_wav).exists()
        and user_voiceprint is not None
        and user_voiceprint.size > 0
    ):
        mic_bleed_assignments = _detect_mic_bleed(
            Path(mic_wav),
            transcript_segments,
            mic_source=mic_source,
            sys_turns=turns,
            sys_assignment=assignment,
            encoder=encoder,
            user_voiceprint=user_voiceprint,
            user_match_threshold=user_match_threshold,
            min_overlap_sec=min_overlap_sec,
            progress_cb=progress.sub_range(80, 95),
        )
    progress.set(95)

    segment_labels = _label_transcript_segments(
        transcript_segments,
        turns,
        assignment,
        clusters,
        sys_source=sys_source,
        min_overlap_sec=min_overlap_sec,
        mic_bleed_assignments=mic_bleed_assignments,
    )
    # Drop clusters whose voice turns never overlapped any transcript
    # segment with enough margin to win an assignment. They're a
    # speaker-walker dead end -- no example lines to show -- and tend to
    # come from short non-speech blips silero+webrtcvad called voiced
    # but Whisper produced nothing for.
    clusters = _drop_clusters_without_segments(clusters, segment_labels)
    unknown_ids = [c.cluster_id for c in clusters if c.name is None]
    progress.set(100)
    return RefinementResult(
        turns=turns,
        clusters=clusters,
        segment_labels=segment_labels,
        unknown_cluster_ids=unknown_ids,
    )


def _detect_mic_bleed(
    mic_wav: Path,
    transcript_segments,
    *,
    mic_source: str,
    sys_turns: list[Turn],
    sys_assignment: ClusterAssignment,
    encoder: Embedder,
    user_voiceprint: np.ndarray,
    user_match_threshold: float,
    min_overlap_sec: float,
    progress_cb: Optional[Callable[[int], None]] = None,
) -> dict[int, int]:
    """Find mic-source segments that should be reattributed to a sys cluster.

    A mic turn is considered "bleed" if it (a) does NOT match the user's
    stored voiceprint and (b) substantially overlaps a sys cluster turn.
    The bleed mic turn inherits that sys cluster's id, and any mic-source
    transcript segment falling inside the bleed turn gets relabeled.

    Returns a mapping of `segment_index -> sys_cluster_id`. Segments not
    in the mapping fall through to the existing mic-source default
    (user_name in the display label).

    The implementation only embeds the few mic turns that overlap at
    least one mic-source transcript segment -- silent stretches of
    mic.wav (most of a meeting where the user isn't talking) get skipped.
    """
    mic_segment_spans = [
        (i, float(seg.t_start), float(seg.t_end))
        for i, seg in enumerate(transcript_segments)
        if getattr(seg, "source", None) == mic_source
    ]
    if not mic_segment_spans:
        return {}
    mic_turns_all = find_turns_in_wav(mic_wav)
    mic_turns = [
        t for t in mic_turns_all
        if t.duration >= MIN_TURN_DURATION_FOR_CLUSTERING_SEC
    ]
    if not mic_turns:
        return {}
    voiceprint = np.asarray(user_voiceprint, dtype=np.float32).reshape(-1)
    bleed: dict[int, int] = {}
    mic_total = len(mic_turns)
    for i, turn in enumerate(mic_turns):
        overlapping_segs = [
            (idx, s_start, s_end)
            for (idx, s_start, s_end) in mic_segment_spans
            if _interval_overlap(turn.t_start, turn.t_end, s_start, s_end) > 0
        ]
        if progress_cb is not None:
            progress_cb(int(round(100 * (i + 1) / mic_total)))
        if not overlapping_segs:
            continue
        # Voiceprint check first -- if this is the user talking, leave
        # the segment alone (mic-default labeling already attributes it
        # correctly).
        mic_embedding = np.asarray(encoder.embed_turn(turn), dtype=np.float32)
        if np.linalg.norm(mic_embedding) > 1e-6:
            sim_user = cosine_similarity(mic_embedding, voiceprint)
            if sim_user >= user_match_threshold:
                continue
        # Not the user. Look for a sys cluster whose turns overlap this
        # mic turn -- the same speech is on both channels (bleed).
        cluster_overlap: dict[int, float] = {}
        for sys_idx, sys_turn in enumerate(sys_turns):
            ov = _interval_overlap(
                turn.t_start, turn.t_end, sys_turn.t_start, sys_turn.t_end
            )
            if ov > 0:
                cid = sys_assignment.labels[sys_idx]
                cluster_overlap[cid] = cluster_overlap.get(cid, 0.0) + ov
        if not cluster_overlap:
            continue
        best_cluster = max(cluster_overlap, key=cluster_overlap.get)
        if cluster_overlap[best_cluster] < min_overlap_sec:
            continue
        # Tag every mic-source transcript segment inside this bleed turn.
        for (idx, _, _) in overlapping_segs:
            bleed[idx] = best_cluster
    return bleed


def _drop_clusters_without_segments(
    clusters: list[ClusterSummary],
    segment_labels: list[SegmentLabel],
) -> list[ClusterSummary]:
    used = {lbl.cluster_id for lbl in segment_labels if lbl.cluster_id is not None}
    return [c for c in clusters if c.cluster_id in used]


def _empty_result(transcript_segments) -> RefinementResult:
    return RefinementResult(
        turns=[],
        clusters=[],
        segment_labels=[
            SegmentLabel(segment_index=i, cluster_id=None, name=None, confidence=None)
            for i, _ in enumerate(transcript_segments)
        ],
        unknown_cluster_ids=[],
    )


def _summarize_clusters(
    assignment: ClusterAssignment,
    turns: list[Turn],
    store: SpeakerStore,
    match_threshold: float,
    *,
    matched_tags: Optional[dict[int, tuple[str, float]]] = None,
) -> list[ClusterSummary]:
    """Build per-cluster summaries. Clusters carrying an anchor name (from
    a user-supplied speaker tag) bypass the cross-meeting store match and
    use the anchor name directly -- the user's word beats fuzzy cosine
    matching against the long-term store. Untagged clusters fall back to
    the store match path.
    """
    matched_tags = matched_tags or {}
    summaries: list[ClusterSummary] = []
    for cid in range(assignment.cluster_count()):
        members = assignment.segments_for(cid)
        centroid = assignment.centroids[cid]
        anchor_name = assignment.name_for(cid)
        if anchor_name is not None:
            # Collect every tag time that landed in a turn assigned to
            # this cluster, for badge rendering.
            tag_times = sorted(
                t for turn_idx, (_name, t) in matched_tags.items()
                if turn_idx in members
            )
            summary = ClusterSummary(
                cluster_id=cid,
                centroid=centroid,
                turn_indices=members,
                name=anchor_name,
                match_similarity=None,
                was_user_tagged=True,
                tag_times_seconds=tag_times,
            )
        else:
            match = store.match(centroid, threshold=match_threshold)
            summary = ClusterSummary(
                cluster_id=cid,
                centroid=centroid,
                turn_indices=members,
                name=match.speaker.name if match else None,
                match_similarity=match.similarity if match else None,
            )
        summaries.append(summary)
    return summaries


def _match_tags_to_turns(
    tags: list[SpeakerTag],
    turns: list[Turn],
    *,
    tolerance_sec: float,
) -> dict[int, tuple[str, float]]:
    """Snap each tag to the nearest turn within `tolerance_sec`.

    Returns a mapping `turn_idx -> (name, tag_t_seconds)`. Tags that
    don't have any turn within tolerance are dropped silently (typically
    clicks that landed in silence or in a sub-second turn that got
    filtered before clustering). If multiple tags target the same turn
    with different names, the latest tag wins -- the user's most recent
    intent is the most likely to be correct.
    """
    if not tags or not turns:
        return {}
    out: dict[int, tuple[str, float]] = {}
    for tag in tags:
        name = (tag.name or "").strip()
        if not name:
            continue
        best_idx = -1
        best_dist = float("inf")
        for i, turn in enumerate(turns):
            if tag.t_seconds < turn.t_start:
                dist = turn.t_start - tag.t_seconds
            elif tag.t_seconds > turn.t_end:
                dist = tag.t_seconds - turn.t_end
            else:
                dist = 0.0
            if dist < best_dist:
                best_dist = dist
                best_idx = i
        if best_idx >= 0 and best_dist <= tolerance_sec:
            out[best_idx] = (name, float(tag.t_seconds))
    return out


def _label_transcript_segments(
    transcript_segments,
    turns: list[Turn],
    assignment: ClusterAssignment,
    clusters: list[ClusterSummary],
    *,
    sys_source: str,
    min_overlap_sec: float,
    mic_bleed_assignments: Optional[dict[int, int]] = None,
) -> list[SegmentLabel]:
    """For each transcript segment, pick the cluster with maximum time overlap.

    Mic-source segments stay unlabeled by default so the existing
    rewrite_user_label pipeline attributes them to the user. When
    `mic_bleed_assignments` flags a mic-source segment as overlapping a
    system-audio cluster (bleed), that segment gets the sys cluster's
    label instead. For sys-source segments, sums total overlap per
    cluster across all member turns. The cluster with the largest
    overlap wins, provided the overlap exceeds `min_overlap_sec`
    (avoids spurious assignment from tiny turn-boundary intersections).
    """
    bleed = mic_bleed_assignments or {}
    cluster_by_id = {c.cluster_id: c for c in clusters}
    labels: list[SegmentLabel] = []
    for i, seg in enumerate(transcript_segments):
        if getattr(seg, "source", None) != sys_source:
            cluster_id = bleed.get(i)
            if cluster_id is not None and cluster_id in cluster_by_id:
                summary = cluster_by_id[cluster_id]
                labels.append(SegmentLabel(
                    segment_index=i,
                    cluster_id=cluster_id,
                    name=summary.name,
                    confidence=summary.match_similarity,
                ))
            else:
                labels.append(SegmentLabel(
                    segment_index=i,
                    cluster_id=None,
                    name=None,
                    confidence=None,
                ))
            continue
        seg_start = float(seg.t_start)
        seg_end = float(seg.t_end)
        # Sum overlap per cluster.
        overlaps: dict[int, float] = {}
        for turn_idx, turn in enumerate(turns):
            cluster_id = assignment.labels[turn_idx]
            overlap = _interval_overlap(seg_start, seg_end, turn.t_start, turn.t_end)
            if overlap > 0:
                overlaps[cluster_id] = overlaps.get(cluster_id, 0.0) + overlap
        if not overlaps:
            labels.append(SegmentLabel(
                segment_index=i,
                cluster_id=None,
                name=None,
                confidence=None,
            ))
            continue
        best_cluster_id = max(overlaps, key=overlaps.get)
        best_overlap = overlaps[best_cluster_id]
        if best_overlap < min_overlap_sec:
            labels.append(SegmentLabel(
                segment_index=i,
                cluster_id=None,
                name=None,
                confidence=None,
            ))
            continue
        summary = cluster_by_id[best_cluster_id]
        labels.append(SegmentLabel(
            segment_index=i,
            cluster_id=best_cluster_id,
            name=summary.name,
            confidence=summary.match_similarity,
        ))
    return labels


def _interval_overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def apply_labels_to_segments(
    transcript_segments,
    segment_labels: list[SegmentLabel],
    *,
    fallback_template: str = "Speaker {cid}",
) -> list:
    """Return a new list of TranscriptSegments with `speaker_name` set.

    Mic-source segments are returned unchanged (the existing user-name
    rewrite pipeline owns those). Sys-source segments get `speaker_name`
    set to the matched name, or to `Speaker N` for unknown clusters that
    the user hasn't labeled yet. Source channel ("sys") is preserved so
    downstream code can still tell mic from system audio.
    """
    out = []
    label_by_index = {lbl.segment_index: lbl for lbl in segment_labels}
    for i, seg in enumerate(transcript_segments):
        label = label_by_index.get(i)
        if label is None or label.cluster_id is None:
            out.append(seg)
            continue
        speaker_name = label.name or fallback_template.format(cid=label.cluster_id + 1)
        out.append(_with_speaker(seg, speaker_name))
    return out


def _with_speaker(seg, speaker_name: str):
    """Return a copy of `seg` with `speaker_name` set.

    Uses dataclasses.replace when available; falls back to a manual
    rebuild so the refiner can accept simple SimpleNamespace test doubles.
    """
    try:
        from dataclasses import replace as dc_replace
        return dc_replace(seg, speaker_name=speaker_name)
    except TypeError:
        clone = type(seg)(
            source=seg.source,
            text=seg.text,
            t_start=seg.t_start,
            t_end=seg.t_end,
            is_provisional=getattr(seg, "is_provisional", False),
            speaker_name=speaker_name,
        )
        return clone
