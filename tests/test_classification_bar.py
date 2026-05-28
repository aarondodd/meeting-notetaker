"""ClassificationBar width-capping regression (Issue #24 follow-up).

v0.7.0 first cut shipped the bar as inline chips for each person /
topic / suggested-topic. Adding ~10 chips ballooned the bar's
sizeHint width past 1500 px, which the parent QMainWindow honored
by widening itself -- so users with many tagged sessions watched
their window resize horizontally on every session switch.

The rewrite replaces the inline chips with three QToolButton
popups (Series, People, Topics). The bar's width is now driven by
the labels alone ("People (N)" / "Topics (N)") and a capped Series
button, regardless of how many items the session carries.

These tests pin the property: at any realistic content level the
bar's sizeHint stays well under any window the user would have.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.models.classification import (  # noqa: E402
    Contact,
    Series,
    SessionClassification,
    SessionContact,
    SessionTopic,
    Topic,
)
from meeting_notetaker.ui.classification_bar import ClassificationBar  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


def _make_classification(*, n_people: int, n_topics: int,
                         n_suggestions: int = 0,
                         series_name: str = "") -> SessionClassification:
    """Build a SessionClassification.

    Argument names keep the "n_people" / "people" wording because
    the per-session role is still "People" in the UI; underlying
    entity is Contact post-Phase 2.
    """
    return SessionClassification(
        series=(Series(id=1, name=series_name) if series_name else None),
        contacts=[
            SessionContact(
                contact=Contact(id=i, display_name=f"Person {i:02d}"),
                source="manual",
            )
            for i in range(n_people)
        ],
        topics=[
            SessionTopic(
                topic=Topic(id=i, name=f"Topic{i:02d}"),
                source="manual",
                accepted=True,
            )
            for i in range(n_topics)
        ] + [
            SessionTopic(
                topic=Topic(id=1000 + i, name=f"Suggestion{i:02d}"),
                source="auto",
                accepted=False,
            )
            for i in range(n_suggestions)
        ],
    )


def test_bar_empty_size_is_modest(qt_app):
    bar = ClassificationBar()
    bar.set_session("s", SessionClassification())
    try:
        assert bar.sizeHint().width() < 400
    finally:
        bar.deleteLater()


def test_bar_width_independent_of_people_count(qt_app):
    """Adding 50 people must not measurably grow the bar's sizeHint
    (the People button label only carries the count)."""
    bar = ClassificationBar()
    try:
        bar.set_session("s", _make_classification(n_people=0, n_topics=0))
        empty_w = bar.sizeHint().width()
        bar.set_session("s", _make_classification(n_people=50, n_topics=0))
        loaded_w = bar.sizeHint().width()
        # Allow a few pixels for "(50)" vs "" label digit width.
        delta = loaded_w - empty_w
        assert delta < 40, (
            f"bar grew by {delta} px when 50 people were added; "
            "the chip refactor was supposed to keep this constant"
        )
    finally:
        bar.deleteLater()


def test_bar_width_independent_of_topic_count(qt_app):
    bar = ClassificationBar()
    try:
        bar.set_session("s", _make_classification(n_people=0, n_topics=0))
        empty_w = bar.sizeHint().width()
        bar.set_session("s", _make_classification(
            n_people=0, n_topics=50, n_suggestions=20,
        ))
        loaded_w = bar.sizeHint().width()
        delta = loaded_w - empty_w
        # Label is "Topics (50, 20 suggested)" -- a bit longer than
        # "Topics", so allow ~120px for the extra text.
        assert delta < 150, (
            f"bar grew by {delta} px when 50 topics + 20 suggestions "
            "were added; chip refactor should bound growth to the "
            "label digit"
        )
    finally:
        bar.deleteLater()


def test_bar_width_bounded_under_extreme_load(qt_app):
    """The headline property: even an absurd number of associations
    keeps the bar narrow enough not to widen any realistic window."""
    bar = ClassificationBar()
    try:
        bar.set_session("s", _make_classification(
            n_people=200, n_topics=200, n_suggestions=50,
            series_name="An Extremely Long Series Name That Used To "
                        "Force The Bar Way Past Any Reasonable Width",
        ))
        w = bar.sizeHint().width()
        # Default app window is 1024px wide; the bar should fit
        # comfortably inside the right pane even on a narrow
        # session-list split. 600px is roomy.
        assert w < 600, (
            f"bar sizeHint width is {w} px under heavy load -- this "
            "would push the window wider than the user wants"
        )
    finally:
        bar.deleteLater()


def test_series_button_label_elides_overlong_names(qt_app):
    bar = ClassificationBar()
    try:
        long_name = "A" * 100
        bar.set_session("s", _make_classification(
            n_people=0, n_topics=0, series_name=long_name,
        ))
        text = bar._series_btn.text()  # noqa: SLF001
        assert len(text) <= ClassificationBar._SERIES_LABEL_CAP  # noqa: SLF001
        assert text.endswith("...")
        # And the full name is preserved in the tooltip for hover lookup.
        assert long_name in bar._series_btn.toolTip()  # noqa: SLF001
    finally:
        bar.deleteLater()


def test_series_button_label_unchanged_for_short_names(qt_app):
    bar = ClassificationBar()
    try:
        bar.set_session("s", _make_classification(
            n_people=0, n_topics=0, series_name="Platform Sync",
        ))
        assert bar._series_btn.text() == "Platform Sync"  # noqa: SLF001
    finally:
        bar.deleteLater()


def test_people_button_removed_in_v0_7_2(qt_app):
    """The People button + its add/remove signals were removed in
    v0.7.2; the Attendee Details drawer + live-notes Attendees list
    cover the same data surface. This test pins the removal so a
    future refactor doesn't accidentally bring it back."""
    bar = ClassificationBar()
    try:
        assert not hasattr(bar, "_people_btn")
        assert not hasattr(bar, "_people_menu")
        assert not hasattr(bar, "_known_people")
        assert not hasattr(bar, "add_person_requested")
        assert not hasattr(bar, "remove_person_requested")
    finally:
        bar.deleteLater()


def test_topics_button_label_separates_accepted_from_suggestions(qt_app):
    bar = ClassificationBar()
    try:
        bar.set_session("s", _make_classification(
            n_people=0, n_topics=4, n_suggestions=3,
        ))
        assert bar._topics_btn.text() == "Topics (4, 3 suggested)"  # noqa: SLF001
        bar.set_session("s", _make_classification(
            n_people=0, n_topics=4, n_suggestions=0,
        ))
        assert bar._topics_btn.text() == "Topics (4)"  # noqa: SLF001
        bar.set_session("s", _make_classification(n_people=0, n_topics=0))
        assert bar._topics_btn.text() == "Topics"  # noqa: SLF001
    finally:
        bar.deleteLater()
