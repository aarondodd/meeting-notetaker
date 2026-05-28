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
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QWidget,
)

from ..models.classification import SessionClassification


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

        # Topics: QToolButton + popup menu, with an extra Suggestions
        # section in the menu for auto-extracted-but-unaccepted
        # topic candidates.
        self._topics_btn = QToolButton(self)
        self._topics_btn.setText("Topics")
        self._topics_btn.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextOnly
        )
        self._topics_btn.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self._topics_menu = QMenu(self._topics_btn)
        self._topics_btn.setMenu(self._topics_menu)
        self._topics_menu.aboutToShow.connect(self._rebuild_topics_menu)
        layout.addWidget(self._topics_btn)

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

    # ---- menu builders ----
    def _rebuild_topics_menu(self) -> None:
        menu = self._topics_menu
        menu.clear()
        add_action = QAction("+ Add Topic...", menu)
        add_action.triggered.connect(self._on_add_topic)
        menu.addAction(add_action)
        accepted = [t for t in self._classification.topics if t.accepted]
        suggestions = [t for t in self._classification.topics if not t.accepted]
        if not accepted and not suggestions:
            menu.addSeparator()
            empty = QAction("(no topics)", menu)
            empty.setEnabled(False)
            menu.addAction(empty)
            return
        if accepted:
            menu.addSeparator()
            for st in accepted:
                tid = st.topic.id
                label = f"x  {st.topic.name}"
                act = QAction(label, menu)
                act.setToolTip("Remove this topic from the session")
                act.triggered.connect(
                    lambda _checked=False, tid=tid: self._emit_remove_topic(tid)
                )
                menu.addAction(act)
        if suggestions:
            menu.addSeparator()
            header = QAction("Suggestions:", menu)
            header.setEnabled(False)
            menu.addAction(header)
            for st in suggestions:
                tid = st.topic.id
                # Accept + (separately) reject for suggestions. Two
                # menu rows per suggestion keeps the click intent
                # unambiguous: "+ accept" vs "x reject".
                accept_act = QAction(f"+  {st.topic.name}", menu)
                accept_act.setToolTip("Accept this suggested topic")
                accept_act.triggered.connect(
                    lambda _checked=False, tid=tid: self._emit_accept_topic(tid)
                )
                menu.addAction(accept_act)
                reject_act = QAction(f"     x  {st.topic.name} (reject)", menu)
                reject_act.setToolTip("Drop this suggestion without accepting")
                reject_act.triggered.connect(
                    lambda _checked=False, tid=tid: self._emit_remove_topic(tid)
                )
                menu.addAction(reject_act)

    # ---- handlers ----
    _CLEAR_SENTINEL = "-- clear (unfile) --"

    def _on_change_series(self) -> None:
        if not self._session_id:
            return
        current = (
            self._classification.series.name
            if self._classification.series else ""
        )
        items = [self._CLEAR_SENTINEL] + sorted(self._known_series)
        try:
            current_idx = items.index(current) if current else 0
        except ValueError:
            current_idx = 0
        choice, ok = QInputDialog.getItem(
            self, "Set Series",
            "Pick an existing series, type a new one, "
            "or pick \"clear (unfile)\":",
            items, current_idx, True,
        )
        if not ok:
            return
        chosen = choice.strip()
        if chosen == self._CLEAR_SENTINEL or not chosen:
            self.set_series_requested.emit(self._session_id, "")
            return
        self.set_series_requested.emit(self._session_id, chosen)

    def _on_add_topic(self) -> None:
        if not self._session_id:
            return
        already_on_session = {
            st.topic.name.casefold()
            for st in self._classification.topics
        }
        items = [
            name for name in sorted(self._known_topics)
            if name.casefold() not in already_on_session
        ]
        choice, ok = QInputDialog.getItem(
            self, "Add Topic",
            "Pick an existing topic or type a new one:",
            items, 0, True,
        )
        if not ok:
            return
        name = choice.strip()
        if not name:
            return
        self.add_topic_requested.emit(self._session_id, name)

    def _emit_remove_topic(self, topic_id: int) -> None:
        if not self._session_id:
            return
        self.remove_topic_requested.emit(self._session_id, topic_id)

    def _emit_accept_topic(self, topic_id: int) -> None:
        if not self._session_id:
            return
        self.accept_topic_requested.emit(self._session_id, topic_id)
