"""Classification chips row for the session view.

Displays the current session's Series, People, and Topics as a single
horizontal row above the tabs. Each chip carries an X to remove the
association; per-dimension `+` buttons let the user add new ones via
a small input dialog. The bar is read-only when no session is
selected.

Auto-extracted topic suggestions (source=auto, accepted=False) render
in a distinct dimmed style with Accept + Reject affordances, so the
user can confirm or drop them in-place rather than via a separate
panel.

Mutations emit signals upward; persistence is handled by MainApp
(which owns the ClassificationStore + threads the session lifecycle).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QWidget,
)

from ..models.classification import SessionClassification, SessionTopic


class _Chip(QWidget):
    """One chip = label + close X. Click X to emit removed()."""

    removed = pyqtSignal()
    accepted = pyqtSignal()  # only used by suggestion chips

    def __init__(
        self,
        text: str,
        *,
        suggestion: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 4, 2)
        layout.setSpacing(4)
        self._label = QLabel(text, self)
        if suggestion:
            # Dimmed font + italic so the eye distinguishes
            # suggestions from confirmed chips without needing a
            # secondary widget.
            font = QFont(self._label.font())
            font.setItalic(True)
            self._label.setFont(font)
            self._label.setStyleSheet("color: gray;")
        layout.addWidget(self._label)
        if suggestion:
            accept_btn = QToolButton(self)
            accept_btn.setText("+")
            accept_btn.setToolTip("Accept this topic suggestion")
            accept_btn.setAutoRaise(True)
            accept_btn.clicked.connect(self.accepted.emit)
            layout.addWidget(accept_btn)
        remove_btn = QToolButton(self)
        remove_btn.setText("X")
        remove_btn.setToolTip("Remove")
        remove_btn.setAutoRaise(True)
        remove_btn.clicked.connect(self.removed.emit)
        layout.addWidget(remove_btn)
        self.setSizePolicy(
            QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed,
        )


@dataclass
class _PendingMutation:
    """A user-driven add/remove the controller (MainApp) must persist."""
    kind: str          # "add_topic" | "remove_topic" | "accept_topic" |
                       # "add_person" | "remove_person" |
                       # "set_series" | "clear_series"
    payload: object    # str (name) or int (topic_id / person_id) or None


class ClassificationBar(QWidget):
    """Read+write surface over a session's classification.

    Provides display + light editing only. The actual data lives in
    a ClassificationStore the parent app owns; this widget signals
    intentions and lets the parent apply them.
    """

    # Each user click maps to one of these signals. Payload semantics
    # in _PendingMutation above. session_id is always passed for
    # routing -- the bar carries the id alongside the classification
    # snapshot to avoid stale-id race conditions when the user
    # double-clicks rapidly across sessions.
    add_topic_requested = pyqtSignal(str, str)              # session_id, topic_name
    remove_topic_requested = pyqtSignal(str, int)           # session_id, topic_id
    accept_topic_requested = pyqtSignal(str, int)           # session_id, topic_id
    add_person_requested = pyqtSignal(str, str)             # session_id, display_name
    remove_person_requested = pyqtSignal(str, int)          # session_id, person_id
    set_series_requested = pyqtSignal(str, str)             # session_id, series_name ("" clears)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._session_id: str = ""
        self._classification = SessionClassification()

        outer = QHBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(8)

        self._series_label = QLabel("Series:", self)
        self._series_label.setStyleSheet("color: gray;")
        outer.addWidget(self._series_label)
        self._series_value = QLabel("(none)", self)
        self._series_value.setStyleSheet("font-weight: 600;")
        outer.addWidget(self._series_value)
        self._series_btn = QPushButton("Change", self)
        self._series_btn.setFlat(True)
        self._series_btn.clicked.connect(self._on_change_series)
        outer.addWidget(self._series_btn)

        self._sep1 = QLabel("|", self)
        self._sep1.setStyleSheet("color: gray;")
        outer.addWidget(self._sep1)

        outer.addWidget(QLabel("People:", self))
        self._people_container = QWidget(self)
        self._people_layout = QHBoxLayout(self._people_container)
        self._people_layout.setContentsMargins(0, 0, 0, 0)
        self._people_layout.setSpacing(4)
        outer.addWidget(self._people_container)
        self._add_person_btn = QToolButton(self)
        self._add_person_btn.setText("+ Person")
        self._add_person_btn.setAutoRaise(True)
        self._add_person_btn.clicked.connect(self._on_add_person)
        outer.addWidget(self._add_person_btn)

        self._sep2 = QLabel("|", self)
        self._sep2.setStyleSheet("color: gray;")
        outer.addWidget(self._sep2)

        outer.addWidget(QLabel("Topics:", self))
        self._topics_container = QWidget(self)
        self._topics_layout = QHBoxLayout(self._topics_container)
        self._topics_layout.setContentsMargins(0, 0, 0, 0)
        self._topics_layout.setSpacing(4)
        outer.addWidget(self._topics_container)
        self._add_topic_btn = QToolButton(self)
        self._add_topic_btn.setText("+ Topic")
        self._add_topic_btn.setAutoRaise(True)
        self._add_topic_btn.clicked.connect(self._on_add_topic)
        outer.addWidget(self._add_topic_btn)

        outer.addStretch(1)
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
        for w in (
            self._series_btn, self._add_person_btn, self._add_topic_btn,
        ):
            w.setEnabled(enabled)
        self._render()

    def _render(self) -> None:
        # Series.
        if self._classification.series is not None:
            self._series_value.setText(self._classification.series.name)
        else:
            self._series_value.setText("(none)")
        # People.
        self._clear_layout(self._people_layout)
        for sp in self._classification.people:
            chip = _Chip(sp.person.display_name, parent=self._people_container)
            pid = sp.person.id
            chip.removed.connect(lambda pid=pid: self._emit_remove_person(pid))
            self._people_layout.addWidget(chip)
        if not self._classification.people:
            placeholder = QLabel("(none)", self._people_container)
            placeholder.setStyleSheet("color: gray;")
            self._people_layout.addWidget(placeholder)
        # Topics -- show accepted first, then suggestions dimmed.
        self._clear_layout(self._topics_layout)
        accepted = [t for t in self._classification.topics if t.accepted]
        suggestions = [t for t in self._classification.topics if not t.accepted]
        for st in accepted:
            chip = _Chip(st.topic.name, parent=self._topics_container)
            tid = st.topic.id
            chip.removed.connect(lambda tid=tid: self._emit_remove_topic(tid))
            self._topics_layout.addWidget(chip)
        for st in suggestions:
            chip = _Chip(
                st.topic.name, suggestion=True, parent=self._topics_container,
            )
            tid = st.topic.id
            chip.accepted.connect(lambda tid=tid: self._emit_accept_topic(tid))
            chip.removed.connect(lambda tid=tid: self._emit_remove_topic(tid))
            self._topics_layout.addWidget(chip)
        if not self._classification.topics:
            placeholder = QLabel("(none)", self._topics_container)
            placeholder.setStyleSheet("color: gray;")
            self._topics_layout.addWidget(placeholder)

    # ---- handlers ----
    def _on_change_series(self) -> None:
        if not self._session_id:
            return
        current = (
            self._classification.series.name
            if self._classification.series else ""
        )
        new_name, ok = QInputDialog.getText(
            self, "Set Series",
            "Series name (leave blank to unfile):",
            QLineEdit.EchoMode.Normal,
            current,
        )
        if not ok:
            return
        # Empty string clears the assignment; the parent sees "" and
        # calls assign_series(None).
        self.set_series_requested.emit(self._session_id, new_name.strip())

    def _on_add_person(self) -> None:
        if not self._session_id:
            return
        name, ok = QInputDialog.getText(
            self, "Add Person",
            "Person name:",
            QLineEdit.EchoMode.Normal,
        )
        if not ok or not name.strip():
            return
        self.add_person_requested.emit(self._session_id, name.strip())

    def _on_add_topic(self) -> None:
        if not self._session_id:
            return
        name, ok = QInputDialog.getText(
            self, "Add Topic",
            "Topic name:",
            QLineEdit.EchoMode.Normal,
        )
        if not ok or not name.strip():
            return
        self.add_topic_requested.emit(self._session_id, name.strip())

    def _emit_remove_person(self, person_id: int) -> None:
        if not self._session_id:
            return
        self.remove_person_requested.emit(self._session_id, person_id)

    def _emit_remove_topic(self, topic_id: int) -> None:
        if not self._session_id:
            return
        self.remove_topic_requested.emit(self._session_id, topic_id)

    def _emit_accept_topic(self, topic_id: int) -> None:
        if not self._session_id:
            return
        self.accept_topic_requested.emit(self._session_id, topic_id)

    @staticmethod
    def _clear_layout(layout: QHBoxLayout) -> None:
        """Remove every child widget. Used between renders to wipe
        prior chips before drawing the fresh set."""
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
