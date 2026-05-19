"""Walker integration with in-meeting click-to-tag clusters."""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.diarization.refiner import (  # noqa: E402
    ClusterSummary,
    RefinementResult,
    SegmentLabel,
)
from meeting_notetaker.models.transcript import TranscriptSegment  # noqa: E402
from meeting_notetaker.ui.speaker_walker_dialog import (  # noqa: E402
    SpeakerWalkerEntry,
    _SpeakerCard,
    _format_times,
)
from meeting_notetaker.ui.speaker_walker_helpers import (  # noqa: E402
    entries_from_refinement,
)


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _unit(*c: float) -> np.ndarray:
    arr = np.asarray(c, dtype=np.float32)
    return arr / np.linalg.norm(arr)


def _make_segment(idx: int, *, text: str = "hi", source: str = "sys") -> TranscriptSegment:
    return TranscriptSegment(source=source, text=text, t_start=float(idx), t_end=float(idx) + 1.0)


def test_format_times_human_readable():
    assert _format_times([42.0]) == "0:42"
    assert _format_times([90.0, 600.0]) == "1:30, 10:00"
    assert _format_times([3725.0]) == "1:02:05"


def test_speaker_card_shows_tagged_badge_for_user_tagged_entry(qt_app):
    entry = SpeakerWalkerEntry(
        cluster_id=0,
        current_name="Pat",
        example_lines=["[00:01] sample"],
        centroid=_unit(1.0, 0.0),
        suggestions=["Pat", "Sam"],
        was_user_tagged=True,
        tag_times_seconds=[342.0, 401.0],
    )
    card = _SpeakerCard(entry, mode="label")
    from PyQt6.QtWidgets import QLabel
    badge_text = " | ".join(
        lbl.text() for lbl in card.findChildren(QLabel) if "tagged" in lbl.text().lower()
    )
    assert "tagged during meeting" in badge_text
    assert "5:42" in badge_text and "6:41" in badge_text


def test_entries_from_refinement_carries_tag_metadata_to_walker_entry():
    """Tagged clusters that survive the only_unknown filter (i.e. Review
    mode, where named clusters are kept) propagate was_user_tagged +
    tag_times_seconds onto the walker entry so the card can render the
    'tagged during meeting at ...' badge."""
    centroid = _unit(1.0, 0.0)
    summary = ClusterSummary(
        cluster_id=0,
        centroid=centroid,
        turn_indices=[0],
        name="Pat",
        was_user_tagged=True,
        tag_times_seconds=[5.0, 12.0],
    )
    result = RefinementResult(
        turns=[],
        clusters=[summary],
        segment_labels=[
            SegmentLabel(segment_index=0, cluster_id=0, name="Pat", confidence=None),
        ],
    )
    segs = [_make_segment(0, text="hi from Pat")]
    entries = entries_from_refinement(result, segs, only_unknown=False)
    assert len(entries) == 1
    assert entries[0].was_user_tagged is True
    assert entries[0].tag_times_seconds == [5.0, 12.0]


def test_entries_from_refinement_only_unknown_drops_named_clusters():
    """only_unknown=True excludes any cluster whose name is set, whether
    the name came from a store match or an in-meeting tag. The auto-popup
    walker is gone; this filter just preserves the simple original
    semantics for any future caller that asks for unknowns only."""
    centroid = _unit(1.0, 0.0)
    tagged = ClusterSummary(
        cluster_id=0, centroid=centroid, turn_indices=[0],
        name="Pat", was_user_tagged=True,
    )
    unknown = ClusterSummary(
        cluster_id=1, centroid=centroid, turn_indices=[1],
        name=None, was_user_tagged=False,
    )
    result = RefinementResult(
        turns=[], clusters=[tagged, unknown],
        segment_labels=[
            SegmentLabel(segment_index=0, cluster_id=0, name="Pat", confidence=None),
            SegmentLabel(segment_index=1, cluster_id=1, name=None, confidence=None),
        ],
    )
    entries = entries_from_refinement(
        result, [_make_segment(0), _make_segment(1)], only_unknown=True,
    )
    # Only the genuinely unknown cluster lands; the tagged one (now
    # named) is filtered out.
    assert len(entries) == 1
    assert entries[0].current_name is None
