"""Typeahead assignment popup tests (#82, v0.7.8).

Covers the shared base + the Topics multi-select variant + the
Series single-select variant. Plus the ClassificationBar wiring
that translates popup signals into the existing outward-facing
add / accept / remove / set_series signals MainApp already
listens to.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QKeyEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.models.classification import (  # noqa: E402
    SOURCE_AUTO,
    SOURCE_MANUAL,
    SessionClassification,
    SessionTopic,
    Topic,
)
from meeting_notetaker.ui.assignment_popup import (  # noqa: E402
    AssignmentRow,
    SeriesAssignmentPopup,
    TopicsAssignmentPopup,
)
from meeting_notetaker.ui.classification_bar import ClassificationBar  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


# ----------------------------------------------------------------------
# Base behavior (exercised through TopicsAssignmentPopup)


def test_popup_filter_narrows_visible_rows(qt_app):
    po = TopicsAssignmentPopup()
    try:
        po.set_rows([
            AssignmentRow("backend"),
            AssignmentRow("frontend"),
            AssignmentRow("design"),
        ])
        assert po._list.count() == 3  # noqa: SLF001
        po._filter.setText("end")  # noqa: SLF001
        # "backend" + "frontend" survive; "design" filtered out.
        visible = [
            po._list.item(i).data(Qt.ItemDataRole.UserRole)  # noqa: SLF001
            for i in range(po._list.count())  # noqa: SLF001
        ]
        assert set(visible) == {"backend", "frontend"}
    finally:
        po.close()


def test_popup_create_new_row_visible_for_non_matching_filter(qt_app):
    """When the filter text doesn't match any existing row and
    allow_create is on, the popup surfaces the "+ Create '...'"
    affordance."""
    po = TopicsAssignmentPopup()
    try:
        po.set_rows([AssignmentRow("backend"), AssignmentRow("frontend")])
        po._filter.setText("storage")  # noqa: SLF001
        assert po._create_btn.isHidden() is False  # noqa: SLF001
        assert "storage" in po._create_btn.text()  # noqa: SLF001
    finally:
        po.close()


def test_popup_create_new_hidden_when_filter_matches_exactly(qt_app):
    """No "+ Create 'X'" when there's already a row named exactly X
    -- prevents accidental duplicate creation."""
    po = TopicsAssignmentPopup()
    try:
        po.set_rows([AssignmentRow("backend")])
        po._filter.setText("backend")  # noqa: SLF001
        assert po._create_btn.isHidden() is True  # noqa: SLF001
    finally:
        po.close()


def test_popup_escape_closes(qt_app):
    """The base Qt.Popup window flag handles click-outside dismiss,
    but Escape needs an explicit keyPressEvent override since
    QFrame doesn't inherit QDialog's Escape behavior."""
    po = TopicsAssignmentPopup()
    po.show()
    qt_app.processEvents()
    assert po.isVisible() is True
    event = QKeyEvent(
        QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier,
    )
    po.keyPressEvent(event)
    qt_app.processEvents()
    assert po.isVisible() is False


# ----------------------------------------------------------------------
# Topics popup (multi-select)


def test_topics_popup_stays_open_after_toggle(qt_app):
    """Headline regression -- the v0.7.7 QMenu closed after every
    toggle. The popup must stay open so the user can add multiple
    topics in one visit."""
    po = TopicsAssignmentPopup()
    po.set_rows([AssignmentRow("backend"), AssignmentRow("frontend")])
    po.show()
    qt_app.processEvents()
    captured: list[tuple[str, bool]] = []
    po.toggle_requested.connect(lambda n, a: captured.append((n, a)))
    try:
        # Activate the first row.
        po._list.setCurrentRow(0)  # noqa: SLF001
        po._on_item_clicked(po._list.item(0))  # noqa: SLF001
        # Popup is still visible after the toggle.
        assert po.isVisible() is True
        # And the second toggle still works without re-opening.
        po._on_item_clicked(po._list.item(1))  # noqa: SLF001
        assert po.isVisible() is True
        assert len(captured) == 2
    finally:
        po.close()


def test_topics_popup_toggle_flips_assigned_state(qt_app):
    """The popup optimistically flips its own row state so the
    user sees the check change without waiting for the host's
    round-trip. The emitted bool reports the state AFTER toggle
    so the host knows what to do."""
    po = TopicsAssignmentPopup()
    po.set_rows([AssignmentRow("backend", assigned=False)])
    captured: list[tuple[str, bool]] = []
    po.toggle_requested.connect(lambda n, a: captured.append((n, a)))
    try:
        po._on_item_clicked(po._list.item(0))  # noqa: SLF001
        assert captured == [("backend", True)]
        # Second click toggles off.
        po._on_item_clicked(po._list.item(0))  # noqa: SLF001
        assert captured == [("backend", True), ("backend", False)]
    finally:
        po.close()


def test_topics_popup_create_appends_row_and_emits(qt_app):
    po = TopicsAssignmentPopup()
    po.set_rows([AssignmentRow("backend")])
    captured: list[tuple[str, bool]] = []
    po.toggle_requested.connect(lambda n, a: captured.append((n, a)))
    try:
        po._filter.setText("storage")  # noqa: SLF001
        po._on_create_clicked()  # noqa: SLF001
        # The new row is appended assigned=True; the emit reflects.
        assert captured == [("storage", True)]
        # The filter was cleared so the new row shows in the list.
        # Two rows now: backend + storage.
        assert po._list.count() == 2  # noqa: SLF001
    finally:
        po.close()


def test_topics_popup_filter_return_creates_when_no_match(qt_app):
    po = TopicsAssignmentPopup()
    po.set_rows([AssignmentRow("backend")])
    captured: list[tuple[str, bool]] = []
    po.toggle_requested.connect(lambda n, a: captured.append((n, a)))
    try:
        po._filter.setText("storage")  # noqa: SLF001
        # Empty list, create button visible. Enter triggers create.
        po._on_filter_return()  # noqa: SLF001
        assert captured == [("storage", True)]
    finally:
        po.close()


def test_topics_popup_filter_return_activates_first_match(qt_app):
    """When the filter narrows to one or more rows, Enter activates
    the highlighted row (the first visible) instead of creating
    a new one."""
    po = TopicsAssignmentPopup()
    po.set_rows([AssignmentRow("backend"), AssignmentRow("backbone")])
    captured: list[tuple[str, bool]] = []
    po.toggle_requested.connect(lambda n, a: captured.append((n, a)))
    try:
        po._filter.setText("back")  # noqa: SLF001
        po._on_filter_return()  # noqa: SLF001
        # First visible row (backbone alphabetically? or backend?)
        # Either way it MUST be a toggle, not a create.
        assert len(captured) == 1
        assert captured[0][0] in ("backend", "backbone")
        assert captured[0][1] is True
    finally:
        po.close()


# ----------------------------------------------------------------------
# Series popup (single-select)


def test_series_popup_close_then_emit_on_pick(qt_app):
    """Single-select semantics: clicking a row dismisses the popup
    and emits series_chosen with the name."""
    po = SeriesAssignmentPopup()
    po.set_rows_and_current(["Daily Standup", "Weekly 1:1"], current="")
    captured: list[str] = []
    po.series_chosen.connect(lambda n: captured.append(n))
    po.show()
    qt_app.processEvents()
    try:
        # Find the "Daily Standup" row (index depends on the
        # "(none)" sentinel at the top).
        target = next(
            i for i in range(po._list.count())  # noqa: SLF001
            if po._list.item(i).data(Qt.ItemDataRole.UserRole) == "Daily Standup"  # noqa: SLF001
        )
        po._on_item_clicked(po._list.item(target))  # noqa: SLF001
        qt_app.processEvents()
        assert captured == ["Daily Standup"]
        # Popup closes itself before emit.
        assert po.isVisible() is False
    finally:
        po.close()


def test_series_popup_none_sentinel_emits_empty_string(qt_app):
    """Clicking the "(none)" sentinel reports as the clear/unfile
    action via empty string."""
    po = SeriesAssignmentPopup()
    po.set_rows_and_current(["Daily Standup"], current="Daily Standup")
    captured: list[str] = []
    po.series_chosen.connect(lambda n: captured.append(n))
    try:
        # (none) is at index 0 with name == SeriesAssignmentPopup.NONE_SENTINEL.
        po._on_item_clicked(po._list.item(0))  # noqa: SLF001
        assert captured == [""]
    finally:
        po.close()


def test_series_popup_create_from_typed_text(qt_app):
    """Typing a name not in the list + Enter creates + selects + closes."""
    po = SeriesAssignmentPopup()
    po.set_rows_and_current(["Existing Series"], current="")
    captured: list[str] = []
    po.series_chosen.connect(lambda n: captured.append(n))
    po.show()
    qt_app.processEvents()
    try:
        po._filter.setText("Brand New Series")  # noqa: SLF001
        po._on_filter_return()  # noqa: SLF001
        qt_app.processEvents()
        assert captured == ["Brand New Series"]
        assert po.isVisible() is False
    finally:
        po.close()


def test_series_popup_current_pick_marked_via_check_state(qt_app):
    """Visual contract (v0.7.8 followup): single-select rows carry
    native Qt CheckState. The current pick is Checked; others are
    Unchecked. A custom delegate paints a radio button at the
    indicator position (visual smoke is its own concern; this test
    pins the model contract the click handlers and the delegate
    both key off)."""
    po = SeriesAssignmentPopup()
    po.set_rows_and_current(["A", "B", "C"], current="B")
    try:
        states = {
            po._list.item(i).data(Qt.ItemDataRole.UserRole):  # noqa: SLF001
                po._list.item(i).checkState()  # noqa: SLF001
            for i in range(po._list.count())  # noqa: SLF001
        }
        # "B" is the current pick; checked. "A", "C", and the
        # "(none)" sentinel are unchecked.
        assert states["B"] == Qt.CheckState.Checked
        assert states["A"] == Qt.CheckState.Unchecked
        assert states["C"] == Qt.CheckState.Unchecked
        assert states[po.NONE_SENTINEL] == Qt.CheckState.Unchecked
        # All rows are checkable (regardless of state); the delegate
        # paints the indicator as a radio button.
        for i in range(po._list.count()):  # noqa: SLF001
            flags = po._list.item(i).flags()  # noqa: SLF001
            assert bool(flags & Qt.ItemFlag.ItemIsUserCheckable)
    finally:
        po.close()


def test_topics_popup_rows_use_native_checkable_flag(qt_app):
    """Multi-select rows are checkable items so Qt renders a real
    native checkbox at the indicator position. Assigned rows are
    Checked; unassigned are Unchecked."""
    po = TopicsAssignmentPopup()
    po.set_rows([
        AssignmentRow("backend", assigned=True),
        AssignmentRow("frontend", assigned=False),
        AssignmentRow("postgres", assigned=True, suggested=True),
    ])
    try:
        for i in range(po._list.count()):  # noqa: SLF001
            item = po._list.item(i)  # noqa: SLF001
            assert bool(item.flags() & Qt.ItemFlag.ItemIsUserCheckable)
        states = {
            po._list.item(i).data(Qt.ItemDataRole.UserRole):  # noqa: SLF001
                po._list.item(i).checkState()  # noqa: SLF001
            for i in range(po._list.count())  # noqa: SLF001
        }
        assert states["backend"] == Qt.CheckState.Checked
        assert states["frontend"] == Qt.CheckState.Unchecked
        assert states["postgres"] == Qt.CheckState.Checked
        # Suggestion rows carry the badge text + italic font so the
        # user can distinguish a confirmed topic from a candidate.
        suggested = next(
            po._list.item(i)  # noqa: SLF001
            for i in range(po._list.count())  # noqa: SLF001
            if po._list.item(i).data(Qt.ItemDataRole.UserRole) == "postgres"  # noqa: SLF001
        )
        assert "(suggested)" in suggested.text()
        assert suggested.font().italic() is True
    finally:
        po.close()


def test_topics_popup_indicator_click_path_emits_toggle(qt_app):
    """The user can click the checkbox indicator directly (Qt
    flips checkState and fires itemChanged) or anywhere on the
    row body (we toggle manually via itemClicked). Both paths
    must route to the same toggle_requested emit."""
    po = TopicsAssignmentPopup()
    po.set_rows([AssignmentRow("backend", assigned=False)])
    captured: list[tuple[str, bool]] = []
    po.toggle_requested.connect(lambda n, a: captured.append((n, a)))
    try:
        # Simulate the indicator-click path: Qt has already toggled
        # checkState by the time itemChanged fires.
        item = po._list.item(0)  # noqa: SLF001
        item.setCheckState(Qt.CheckState.Checked)
        # itemChanged fires synchronously from setCheckState.
        assert captured == [("backend", True)]
        # The internal row model also flipped to assigned=True so
        # subsequent rebuilds render the correct check state.
        assert po._rows[0].assigned is True  # noqa: SLF001
    finally:
        po.close()


# ----------------------------------------------------------------------
# ClassificationBar wiring (the host that owns the popups)


def _make_classification_with_topics(
    *,
    accepted: list[str],
    suggested: list[str],
) -> SessionClassification:
    """Build a SessionClassification with the given accepted +
    suggestion topics. Each name maps to a stable Topic id."""
    topics: list[SessionTopic] = []
    next_id = 1
    for n in accepted:
        topics.append(SessionTopic(
            topic=Topic(id=next_id, name=n, created_at=""),
            source=SOURCE_MANUAL,
            accepted=True,
        ))
        next_id += 1
    for n in suggested:
        topics.append(SessionTopic(
            topic=Topic(id=next_id, name=n, created_at=""),
            source=SOURCE_AUTO,
            accepted=False,
        ))
        next_id += 1
    return SessionClassification(topics=topics)


def test_bar_topics_popup_opens_with_session_state(qt_app):
    """The bar's Topics button opens the popup populated with
    the session's accepted + suggested + non-session catalog
    items."""
    bar = ClassificationBar()
    try:
        cls = _make_classification_with_topics(
            accepted=["backend"],
            suggested=["postgres"],
        )
        bar.set_known_lists(topics=["backend", "frontend", "postgres", "design"])
        bar.set_session("sess-1", cls)
        bar._on_topics_clicked()  # noqa: SLF001
        popup = bar._topics_popup  # noqa: SLF001
        assert popup is not None
        rows_by_name = {r.name: r for r in popup._rows}  # noqa: SLF001
        assert rows_by_name["backend"].assigned is True
        assert rows_by_name["backend"].suggested is False
        assert rows_by_name["postgres"].assigned is True
        assert rows_by_name["postgres"].suggested is True
        assert rows_by_name["frontend"].assigned is False
        assert rows_by_name["design"].assigned is False
    finally:
        bar.deleteLater()


def test_bar_topics_toggle_emits_add_for_new_assignment(qt_app):
    """The Topics popup signals (name, assigned-after-toggle); the
    bar translates that to add_topic_requested when the topic
    wasn't on the session before."""
    bar = ClassificationBar()
    captured: list[tuple[str, str]] = []
    bar.add_topic_requested.connect(
        lambda sid, name: captured.append((sid, name)),
    )
    try:
        bar.set_known_lists(topics=["backend", "frontend"])
        bar.set_session("sess-1", SessionClassification())
        bar._on_topics_clicked()  # noqa: SLF001
        # Simulate the popup's toggle for "backend" (not on session
        # -> assigned-after-toggle = True).
        bar._on_topic_toggle_requested("backend", True)  # noqa: SLF001
        assert captured == [("sess-1", "backend")]
    finally:
        bar.deleteLater()


def test_bar_topics_toggle_emits_remove_for_existing_accepted(qt_app):
    bar = ClassificationBar()
    captured: list[tuple[str, int]] = []
    bar.remove_topic_requested.connect(
        lambda sid, tid: captured.append((sid, tid)),
    )
    try:
        cls = _make_classification_with_topics(
            accepted=["backend"], suggested=[],
        )
        bar.set_known_lists(topics=["backend"])
        bar.set_session("sess-1", cls)
        bar._on_topics_clicked()  # noqa: SLF001
        # User flips backend OFF.
        bar._on_topic_toggle_requested("backend", False)  # noqa: SLF001
        # Topic id 1 from the helper.
        assert captured == [("sess-1", 1)]
    finally:
        bar.deleteLater()


def test_bar_topics_toggle_emits_accept_for_suggestion_confirm(qt_app):
    """The headline new wiring: when a suggestion row (already on
    the session, but accepted=False) is toggled on, the bar emits
    accept_topic_requested, NOT add."""
    bar = ClassificationBar()
    captured_add: list[tuple[str, str]] = []
    captured_accept: list[tuple[str, int]] = []
    bar.add_topic_requested.connect(
        lambda sid, name: captured_add.append((sid, name)),
    )
    bar.accept_topic_requested.connect(
        lambda sid, tid: captured_accept.append((sid, tid)),
    )
    try:
        cls = _make_classification_with_topics(
            accepted=[], suggested=["postgres"],
        )
        bar.set_known_lists(topics=["postgres"])
        bar.set_session("sess-1", cls)
        bar._on_topics_clicked()  # noqa: SLF001
        bar._on_topic_toggle_requested("postgres", True)  # noqa: SLF001
        # The suggestion's topic id from the helper is 1 (only one item).
        assert captured_accept == [("sess-1", 1)]
        assert captured_add == []
    finally:
        bar.deleteLater()


def test_bar_series_popup_emits_set_series_with_chosen_name(qt_app):
    bar = ClassificationBar()
    captured: list[tuple[str, str]] = []
    bar.set_series_requested.connect(
        lambda sid, name: captured.append((sid, name)),
    )
    try:
        bar.set_known_lists(series=["Daily Standup", "Weekly 1:1"])
        bar.set_session("sess-1", SessionClassification())
        bar._on_change_series()  # noqa: SLF001
        bar._on_series_chosen("Daily Standup")  # noqa: SLF001
        assert captured == [("sess-1", "Daily Standup")]
    finally:
        bar.deleteLater()


def test_bar_series_popup_emits_empty_string_for_clear(qt_app):
    """The "(none)" sentinel translates to empty string on the
    set_series_requested signal, matching the prior contract that
    "" clears the assignment."""
    bar = ClassificationBar()
    captured: list[tuple[str, str]] = []
    bar.set_series_requested.connect(
        lambda sid, name: captured.append((sid, name)),
    )
    try:
        bar.set_known_lists(series=["Daily Standup"])
        bar.set_session("sess-1", SessionClassification())
        bar._on_change_series()  # noqa: SLF001
        bar._on_series_chosen("")  # noqa: SLF001
        assert captured == [("sess-1", "")]
    finally:
        bar.deleteLater()
