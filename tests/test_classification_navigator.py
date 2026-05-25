"""ClassificationNavigator -- selection persistence + filter wiring.

The headline regression: prior to the fix, every set_topics /
set_people / set_series push from MainApp wiped the user's chosen
filter value (clear() reset the combo to index 0). Aaron caught it
after picking a topic to filter by: as soon as he selected a
session, the filter pulldown snapped back to "Pick a topic..." and
the session list went back to "All".

This module pins:

* preserve_selection=True (the default for known-list pushes)
  re-selects the prior value_id when it still exists in the new
  list.
* preserve_selection=False (the case when the user changes view)
  starts fresh -- a series_id has no meaning under By-Person.
* signals stay suppressed across the rebuild so no spurious
  filter_changed fires.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.ui.classification_navigator import (  # noqa: E402
    VIEW_ALL,
    VIEW_BY_PERSON,
    VIEW_BY_SERIES,
    VIEW_BY_TOPIC,
    ClassificationNavigator,
)


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


def _select_view(nav: ClassificationNavigator, view: str) -> None:
    idx = nav._view_combo.findData(view)  # noqa: SLF001
    assert idx >= 0
    nav._view_combo.setCurrentIndex(idx)  # noqa: SLF001


def _select_value(nav: ClassificationNavigator, value_id: int) -> None:
    idx = nav._value_combo.findData(value_id)  # noqa: SLF001
    assert idx >= 0, f"value_id {value_id} not in combo"
    nav._value_combo.setCurrentIndex(idx)  # noqa: SLF001


def test_set_topics_preserves_current_value_when_id_still_present(qt_app):
    nav = ClassificationNavigator()
    try:
        nav.set_topics([(1, "MDM"), (2, "Informatica"), (3, "ETL")])
        _select_view(nav, VIEW_BY_TOPIC)
        _select_value(nav, 2)  # Informatica
        # Simulate the MainApp push that happens on every classification
        # mutation (e.g. user adds a tag on an unrelated session).
        nav.set_topics([(1, "MDM"), (2, "Informatica"), (3, "ETL"), (4, "API")])
        # Combo still on Informatica -- the bug was that it snapped
        # to index 0.
        assert nav.current_state().value_id == 2
    finally:
        nav.deleteLater()


def test_set_topics_clears_selection_when_id_removed(qt_app):
    """If the previously-selected topic gets deleted between pushes
    (user removes the last session it was on, in-use filter then
    drops it), the combo correctly stops selecting it."""
    nav = ClassificationNavigator()
    try:
        nav.set_topics([(1, "MDM"), (2, "Informatica")])
        _select_view(nav, VIEW_BY_TOPIC)
        _select_value(nav, 2)
        # Push without Informatica (id=2) -- e.g. its last session
        # was deleted, in_use filter pruned it.
        nav.set_topics([(1, "MDM")])
        # Combo falls back to whatever's first (placeholder text or
        # MDM); value_id should NOT still be 2.
        assert nav.current_state().value_id != 2
    finally:
        nav.deleteLater()


def test_set_series_preserves_current_value(qt_app):
    nav = ClassificationNavigator()
    try:
        nav.set_series([(10, "Platform Sync"), (11, "1:1 with Alice")])
        _select_view(nav, VIEW_BY_SERIES)
        _select_value(nav, 11)
        nav.set_series([(10, "Platform Sync"), (11, "1:1 with Alice"), (12, "Standup")])
        assert nav.current_state().value_id == 11
    finally:
        nav.deleteLater()


def test_set_people_preserves_current_value(qt_app):
    nav = ClassificationNavigator()
    try:
        nav.set_people([(20, "Alice"), (21, "Bob")])
        _select_view(nav, VIEW_BY_PERSON)
        _select_value(nav, 21)
        nav.set_people([(20, "Alice"), (21, "Bob"), (22, "Carol")])
        assert nav.current_state().value_id == 21
    finally:
        nav.deleteLater()


def test_changing_view_resets_value_selection(qt_app):
    """Switching from By-Topic to By-Person must NOT carry over the
    topic_id -- it'd point at something meaningless."""
    nav = ClassificationNavigator()
    try:
        nav.set_topics([(1, "MDM")])
        nav.set_people([(20, "Alice")])
        _select_view(nav, VIEW_BY_TOPIC)
        _select_value(nav, 1)
        assert nav.current_state().value_id == 1
        _select_view(nav, VIEW_BY_PERSON)
        # Combo populated with people; value_id should be the
        # first one (or None if empty), NOT the lingering topic id.
        assert nav.current_state().value_id != 1
    finally:
        nav.deleteLater()


def test_set_topics_does_not_re_emit_filter_changed_when_selection_preserved(qt_app):
    """Selection-preserving rebuild must not fire a spurious
    filter_changed (would re-render the session list pointlessly)."""
    nav = ClassificationNavigator()
    try:
        emitted: list[tuple[str, object]] = []
        nav.filter_changed.connect(lambda v, vid: emitted.append((v, vid)))
        nav.set_topics([(1, "MDM")])
        _select_view(nav, VIEW_BY_TOPIC)
        _select_value(nav, 1)
        # Count emissions so far -- ignore the setup chatter.
        baseline = len(emitted)
        # The classification-refresh push that previously stomped
        # the selection.
        nav.set_topics([(1, "MDM"), (2, "ETL")])
        # No new emission -- the user's filter is unchanged.
        assert len(emitted) == baseline
        assert nav.current_state().value_id == 1
    finally:
        nav.deleteLater()
