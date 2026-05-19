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


def test_entries_from_refinement_includes_tagged_when_requested():
    """Tagged clusters propagate into the walker entries with name + badge
    when `include_tagged_for_confirmation=True`, and stay out when False."""
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

    # Default mode: tagged clusters shown for confirmation.
    entries = entries_from_refinement(
        result, segs, only_unknown=True,
        include_tagged_for_confirmation=True,
    )
    assert len(entries) == 1
    assert entries[0].was_user_tagged is True
    assert entries[0].tag_times_seconds == [5.0, 12.0]
    assert entries[0].current_name == "Pat"

    # Trust mode: tagged clusters excluded from the walker.
    entries_trust = entries_from_refinement(
        result, segs, only_unknown=True,
        include_tagged_for_confirmation=False,
    )
    assert entries_trust == []


def test_entries_from_refinement_still_includes_genuinely_unknown_in_trust_mode():
    """Even with trust on, unknown clusters (no anchor name) must still
    surface in the walker -- the user has to label them."""
    centroid = _unit(1.0, 0.0)
    unknown_summary = ClusterSummary(
        cluster_id=0,
        centroid=centroid,
        turn_indices=[0],
        name=None,
        was_user_tagged=False,
    )
    result = RefinementResult(
        turns=[], clusters=[unknown_summary],
        segment_labels=[
            SegmentLabel(segment_index=0, cluster_id=0, name=None, confidence=None),
        ],
    )
    entries = entries_from_refinement(
        result, [_make_segment(0)], only_unknown=True,
        include_tagged_for_confirmation=False,
    )
    assert len(entries) == 1
    assert entries[0].current_name is None
