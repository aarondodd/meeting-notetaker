"""Adapters: refinement results / persisted diarization data -> walker entries.

Pulled out of the dialog so it can stay focused on Qt widgets. These
helpers walk the transcript and build per-cluster example lines, then
package the data into `SpeakerWalkerEntry` for the dialog.
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from ..diarization.persistence import DiarizationData
from ..diarization.refiner import RefinementResult
from ..models.transcript import TranscriptSegment, format_segment
from .speaker_walker_dialog import SpeakerWalkerEntry


MAX_EXAMPLES_PER_CLUSTER = 4


def entries_from_refinement(
    result: RefinementResult,
    transcript_segments: list[TranscriptSegment],
    *,
    suggestions: Optional[list[str]] = None,
    only_unknown: bool = True,
    include_tagged_for_confirmation: bool = False,
) -> list[SpeakerWalkerEntry]:
    """Build walker entries from an in-memory refinement result.

    `transcript_segments` is the list of segments the refiner ran over
    (so segment indices in `result.segment_labels` line up).

    `suggestions` is a deduplicated list of names the combo box should
    offer (Outlook attendees + known speakers).

    `only_unknown=True` (the post-meeting auto-pop case) filters to
    clusters where no name was matched. Pass False for Review mode.

    `include_tagged_for_confirmation=True` keeps user-tagged clusters
    in the result even when `only_unknown=True`, so the walker can
    show them pre-filled for the user to confirm. Used in the
    not-yet-confident default mode for click-to-tag.
    """
    suggestions = list(suggestions or [])
    entries: list[SpeakerWalkerEntry] = []
    for summary in result.clusters:
        # Filter logic: in only_unknown mode we drop named clusters
        # EXCEPT when include_tagged_for_confirmation is on and the
        # cluster carries an in-meeting click-to-tag name -- those
        # land in the walker for a confirm pass.
        is_tagged = getattr(summary, "was_user_tagged", False)
        if only_unknown and summary.name is not None:
            if not (include_tagged_for_confirmation and is_tagged):
                continue
        example_lines = _example_lines_for_cluster(
            summary.cluster_id, result, transcript_segments
        )
        if not example_lines:
            # Cluster has no transcript-segment evidence; the walker
            # can't help the user act on it. Skip.
            continue
        entries.append(SpeakerWalkerEntry(
            cluster_id=summary.cluster_id,
            current_name=summary.name,
            example_lines=example_lines,
            centroid=np.asarray(summary.centroid, dtype=np.float32).copy(),
            match_similarity=summary.match_similarity,
            suggestions=suggestions,
            was_user_tagged=is_tagged,
            tag_times_seconds=list(getattr(summary, "tag_times_seconds", [])),
        ))
    return entries


def entries_from_persistence(
    data: DiarizationData,
    transcript_segments: list[TranscriptSegment],
    *,
    suggestions: Optional[list[str]] = None,
    only_unknown: bool = False,
) -> list[SpeakerWalkerEntry]:
    """Build walker entries from a persisted diarization.json file.

    Same shape as `entries_from_refinement` but driven by the on-disk
    data rather than an in-memory refinement. Used by the Review
    Speakers walker.

    `transcript_segments` should be the segments as loaded from
    raw.transcript.md (or, more cheaply, just the parsed lines as
    short strings). For now we accept TranscriptSegment list to keep
    the example-line logic shared.
    """
    suggestions = list(suggestions or [])
    # Build a per-cluster segment-index list from the persisted segments.
    by_cluster: dict[int, list[int]] = {}
    for seg in data.segments:
        by_cluster.setdefault(seg.cluster_id, []).append(seg.segment_index)
    entries: list[SpeakerWalkerEntry] = []
    for cluster in data.clusters:
        if only_unknown and cluster.name is not None:
            continue
        seg_indices = by_cluster.get(cluster.cluster_id, [])
        example_lines = _format_examples(seg_indices, transcript_segments)
        if not example_lines:
            # Legacy persisted clusters from before the refiner pruned
            # empty ones; skip rather than show an unactionable card.
            continue
        entries.append(SpeakerWalkerEntry(
            cluster_id=cluster.cluster_id,
            current_name=cluster.name,
            example_lines=example_lines,
            centroid=cluster.centroid,
            match_similarity=cluster.match_similarity,
            suggestions=suggestions,
        ))
    return entries


def _example_lines_for_cluster(
    cluster_id: int,
    result: RefinementResult,
    transcript_segments: list[TranscriptSegment],
) -> list[str]:
    seg_indices = [
        lbl.segment_index
        for lbl in result.segment_labels
        if lbl.cluster_id == cluster_id
    ]
    return _format_examples(seg_indices, transcript_segments)


def _format_examples(
    seg_indices: list[int],
    transcript_segments: list[TranscriptSegment],
) -> list[str]:
    """Pick up to MAX_EXAMPLES_PER_CLUSTER segments and render them."""
    if not seg_indices or not transcript_segments:
        return []
    n = len(transcript_segments)
    chosen: list[str] = []
    # Prefer longer-text segments (more recognizable) then chronological.
    candidates = [
        transcript_segments[i] for i in seg_indices if 0 <= i < n
    ]
    candidates.sort(key=lambda s: len(s.text or ""), reverse=True)
    for seg in candidates[:MAX_EXAMPLES_PER_CLUSTER]:
        chosen.append(format_segment(seg))
    return chosen


def gather_suggestions(
    known_speakers: list[str],
    attendees: list[str],
) -> list[str]:
    """Combined name pool for the combo-box auto-complete.

    Known speakers first (most likely to match), then any meeting
    attendees not already in the store. De-duplicated, case-preserving.
    """
    seen: set[str] = set()
    out: list[str] = []
    for name in list(known_speakers) + list(attendees):
        clean = (name or "").strip()
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(clean)
    return out
