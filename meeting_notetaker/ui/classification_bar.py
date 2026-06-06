"""Compact classification bar for the session view.

Two buttons above the tabs -- Series and Topics -- each showing a
count of associated items rather than rendering them inline.
Clicking opens a popup menu with the full list and add/remove/accept
actions.

This was a three-button bar (Series / People / Topics) through
v0.7.1. The People button was removed in v0.7.2 once the Attendee
Details drawer above My Notes + Synthesis covered the same data
surface and the live-notes # Attendees list became the canonical
add/remove path.

Series stays a single-value picker (one click -> change dialog).
Topics uses QToolButton's InstantPopup mode with a menu rebuilt
on each render so it reflects current state without manually
wiring per-action updates.

Auto-extracted topic suggestions (source=auto, accepted=False)
render in the Topics menu under a "Suggestions" separator with a
"+" prefix -- clicking the action accepts the suggestion. Manually
accepting a suggestion through the menu has the same semantics as
the old in-bar chip accept button.

Mutations emit signals upward; persistence is handled by MainApp
(which owns the ClassificationStore + threads the session lifecycle).
"""
from __future__ import annotations

from typing import Iterable, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QWidget,
)

from ..models.classification import SessionClassification
from .assignment_popup import (
    AssignmentRow,
    SeriesAssignmentPopup,
    TopicsAssignmentPopup,
)


class ClassificationBar(QWidget):
    """Compact, fixed-width-friendly classification surface.

    Public mutation signals (re-emitted from MainApp into the store):
    """

    add_topic_requested = pyqtSignal(str, str)              # session_id, topic_name
    remove_topic_requested = pyqtSignal(str, int)           # session_id, topic_id
    accept_topic_requested = pyqtSignal(str, int)           # session_id, topic_id
    set_series_requested = pyqtSignal(str, str)             # session_id, series_name ("" clears)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._session_id: str = ""
        self._classification = SessionClassification()
        # Alphabetical known-name pools (pushed from MainApp). Drive
        # the dropdown half of the Add/Change pickers.
        self._known_series: list[str] = []
        self._known_topics: list[str] = []
        # Cap the bar's own height so it never grows with content;
        # menus do the heavy lifting.
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed,
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(10)

        # Series: shown as a single QPushButton labelled with the
        # current series name (or "(none)"). Click -> change dialog.
        layout.addWidget(QLabel("Series:", self))
        self._series_btn = QPushButton("(none)", self)
        self._series_btn.setToolTip(
            "Set or change this session's series. Pick from existing "
            "or type a new name; clear to unfile."
        )
        self._series_btn.clicked.connect(self._on_change_series)
        layout.addWidget(self._series_btn)

        self._sep1 = QLabel("|", self)
        self._sep1.setStyleSheet("color: gray;")
        layout.addWidget(self._sep1)

        # Topics: QPushButton that opens the typeahead multi-select
        # popup (#82). The popup is lazily constructed on first use
        # and reused across opens so its filter input retains its
        # natural reset behavior between invocations.
        self._topics_btn = QPushButton("Topics", self)
        self._topics_btn.setToolTip(
            "View, add, accept, or remove topic tags for this session."
        )
        self._topics_btn.clicked.connect(self._on_topics_clicked)
        layout.addWidget(self._topics_btn)

        # Popups are lazy. Created on the first open.
        self._topics_popup: Optional[TopicsAssignmentPopup] = None
        self._series_popup: Optional[SeriesAssignmentPopup] = None

        layout.addStretch(1)
        self.set_session(None, SessionClassification())

    # ---- public API ----
    def set_session(
        self,
        session_id: Optional[str],
        classification: SessionClassification,
    ) -> None:
        self._session_id = session_id or ""
        self._classification = classification
        enabled = bool(session_id)
        for w in (self._series_btn, self._topics_btn):
            w.setEnabled(enabled)
        self._refresh_button_labels()

    def set_known_lists(
        self,
        *,
        series: Optional[list[str]] = None,
        people: Optional[list[str]] = None,
        topics: Optional[list[str]] = None,
    ) -> None:
        """Push alphabetically-sorted known names from MainApp.

        Drives the dropdown half of the Add/Change pickers. Idempotent.
        ``people`` is accepted but ignored (the People button was
        removed in v0.7.2); callers can keep passing it for API
        compatibility without effect.
        """
        if series is not None:
            self._known_series = list(series)
        if topics is not None:
            self._known_topics = list(topics)

    # ---- label refresh ----

    # Series-button label cap (in chars). Longer series names get
    # elided with "..." so the bar width stays bounded; the full
    # name lives in the button's tooltip for hover lookup. 30 chars
    # fits "Platform Team Sync -- Weekly" comfortably and ellides
    # only obviously long titles.
    _SERIES_LABEL_CAP = 30

    def _refresh_button_labels(self) -> None:
        # Series. Elide overlong names to keep the bar's width
        # constant regardless of how long a user names a series.
        if self._classification.series is not None:
            full = self._classification.series.name
            if len(full) > self._SERIES_LABEL_CAP:
                display = full[: self._SERIES_LABEL_CAP - 3] + "..."
            else:
                display = full
            self._series_btn.setText(display)
            self._series_btn.setToolTip(
                "Set or change this session's series.\n"
                f"Current: {full}"
            )
        else:
            self._series_btn.setText("(none)")
            self._series_btn.setToolTip(
                "Set this session's series. Pick from existing or "
                "type a new name."
            )
        # Topics count -- accepted + suggestions shown separately.
        accepted = [t for t in self._classification.topics if t.accepted]
        suggestions = [t for t in self._classification.topics if not t.accepted]
        if not accepted and not suggestions:
            self._topics_btn.setText("Topics")
        elif suggestions:
            self._topics_btn.setText(
                f"Topics ({len(accepted)}, {len(suggestions)} suggested)"
            )
        else:
            self._topics_btn.setText(f"Topics ({len(accepted)})")

    # ---- popup builders (#82, v0.7.8) ------------------------------

    def _build_topic_rows(self) -> list[AssignmentRow]:
        """Construct the rows the Topics popup shows: every known
        topic with its accepted + suggested state for this session.

        Mental model: ``assigned`` means "the user has confirmed
        this topic for this session." Suggestions (LLM auto-extract
        output not yet confirmed) are NOT assigned -- they're a
        third state, signalled by ``suggested=True`` and rendered
        with an italicized "(suggested)" badge in the popup, with
        the checkbox left unchecked. Conflating "linked-but-
        unaccepted" with "user-confirmed" inside the checkbox
        state misled the user (#82 followup, v0.7.8); the badge
        carries the suggestion semantic on its own.

        Sort (three buckets, alphabetical within each):
          1. Currently assigned (accepted) topics
          2. Suggestions
          3. The rest of the catalog
        The list doubles as an "at a glance" view of what's already
        on the session AND the place to add/accept more, so the
        confirmed picks belong at the top.

        Topics that aren't on the session and aren't suggestions
        appear with ``assigned=False, suggested=False`` -- clicking
        adds them.
        """
        # accepted_keys: names the user has confirmed on this
        # session. suggestion_id_by_key: names linked to the session
        # via the LLM auto-extract path that haven't been confirmed
        # yet (accepted=False on the SessionTopic link).
        accepted_keys: set[str] = set()
        suggestion_id_by_key: dict[str, int] = {}
        for st in self._classification.topics:
            key = st.topic.name.casefold()
            if st.accepted:
                accepted_keys.add(key)
            else:
                suggestion_id_by_key[key] = st.topic.id

        # Catalog is the known-topics universe plus any topics on the
        # session that aren't in the catalog yet (so the user can
        # still see + toggle / reject a session-only entry).
        catalog: list[str] = list(self._known_topics)
        catalog_keys = {n.casefold() for n in catalog}
        for st in self._classification.topics:
            if st.topic.name.casefold() not in catalog_keys:
                catalog.append(st.topic.name)
                catalog_keys.add(st.topic.name.casefold())

        rows: list[AssignmentRow] = []
        for name in catalog:
            key = name.casefold()
            is_suggested = key in suggestion_id_by_key
            is_accepted = key in accepted_keys
            rows.append(AssignmentRow(
                name=name,
                assigned=is_accepted,
                suggested=is_suggested,
            ))
        # Bucket: 0 = assigned (top), 1 = suggested (middle),
        # 2 = neither (bottom). Within each bucket, alphabetical.
        def _bucket(r: AssignmentRow) -> int:
            if r.assigned:
                return 0
            if r.suggested:
                return 1
            return 2
        rows.sort(key=lambda r: (_bucket(r), r.name.casefold()))
        return rows

    def _ensure_topics_popup(self) -> TopicsAssignmentPopup:
        if self._topics_popup is None:
            popup = TopicsAssignmentPopup(parent=self)
            popup.toggle_requested.connect(self._on_topic_toggle_requested)
            self._topics_popup = popup
        return self._topics_popup

    def _ensure_series_popup(self) -> SeriesAssignmentPopup:
        if self._series_popup is None:
            popup = SeriesAssignmentPopup(parent=self)
            popup.series_chosen.connect(self._on_series_chosen)
            self._series_popup = popup
        return self._series_popup

    # ---- click handlers -------------------------------------------

    def _on_topics_clicked(self) -> None:
        if not self._session_id:
            return
        popup = self._ensure_topics_popup()
        popup.set_rows(self._build_topic_rows())
        popup.show_for_widget(self._topics_btn)

    def _on_change_series(self) -> None:
        if not self._session_id:
            return
        popup = self._ensure_series_popup()
        current = (
            self._classification.series.name
            if self._classification.series else ""
        )
        popup.set_rows_and_current(sorted(self._known_series), current)
        popup.show_for_widget(self._series_btn)

    def _on_topic_toggle_requested(
        self, name: str, currently_assigned: bool,
    ) -> None:
        """Translate the popup's name+after-toggle-state signal into
        the existing add / accept / remove signals MainApp already
        wires."""
        if not self._session_id:
            return
        # Look up the session-side entry by name. Three cases:
        #   1. Was a suggestion (link exists, accepted=False) and
        #      user flipped it on -> accept_topic_requested
        #   2. Was already accepted and user flipped it off
        #      -> remove_topic_requested
        #   3. Wasn't on the session at all and user flipped it on
        #      -> add_topic_requested (creates the link)
        existing = next(
            (st for st in self._classification.topics
             if st.topic.name.casefold() == name.casefold()),
            None,
        )
        if existing is None:
            # Not on session before; user added it.
            if currently_assigned:
                self.add_topic_requested.emit(self._session_id, name)
            return
        if currently_assigned:
            # Toggled on while existing -> must have been a
            # suggestion previously. Confirm-accept.
            if not existing.accepted:
                self.accept_topic_requested.emit(
                    self._session_id, existing.topic.id,
                )
        else:
            # Toggled off -> remove the link.
            self.remove_topic_requested.emit(
                self._session_id, existing.topic.id,
            )

    def _on_series_chosen(self, name: str) -> None:
        if not self._session_id:
            return
        # name == "" is the "(none)" sentinel emitted by the popup.
        self.set_series_requested.emit(self._session_id, name)
