"""Right-side click-to-tag attendee list shown during recording.

Visible only while the active session is in STATE_RECORDING or
STATE_PAUSED *and* the user is on the Transcript or My Notes tab. The
widget is owned by SessionView and tucked into a horizontal layout
next to the QTabWidget, so toggling its visibility resizes the editor
pane on the left without resizing the main window.

Each row is a QPushButton (left-click -> emit `tag_clicked(name)`,
right-click -> emit `remove_last_requested(name)`) plus a small badge
showing the per-meeting tag count for that name. Names are sorted
alphabetically; the source of truth is the My Notes `# Attendees`
section, parsed by `utils.live_notes.parse_attendees`.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


SIDEBAR_WIDTH = 200


class _AttendeeRow(QFrame):
    """One clickable attendee row with a tag-count badge."""

    tag_clicked = pyqtSignal(str)
    remove_last_requested = pyqtSignal(str)

    def __init__(self, name: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._name = name
        self.setFrameShape(QFrame.Shape.NoFrame)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(4)
        self._button = QPushButton(name, self)
        self._button.setToolTip(
            "Left-click to tag this speaker at the current moment in the "
            "recording. Right-click to undo the most recent tag."
        )
        self._button.setStyleSheet("text-align: left; padding: 4px 8px;")
        self._button.clicked.connect(lambda: self.tag_clicked.emit(self._name))
        self._button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._button.customContextMenuRequested.connect(self._on_right_click)
        layout.addWidget(self._button, 1)
        self._count_label = QLabel("", self)
        self._count_label.setMinimumWidth(28)
        self._count_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._count_label.setStyleSheet("color: #888; padding-right: 4px;")
        layout.addWidget(self._count_label, 0)

    @property
    def name(self) -> str:
        return self._name

    def set_count(self, count: int) -> None:
        self._count_label.setText(f"×{count}" if count > 0 else "")

    def _on_right_click(self, _pos) -> None:
        # No context menu -- a right-click directly undoes the most
        # recent tag for this name. Keeping it one-click for speed.
        self.remove_last_requested.emit(self._name)


class AttendeeSidebar(QWidget):
    """Vertical list of attendee tag buttons.

    Exposes two signals the SessionView wires up:

    - `tag_clicked(name)` -- user wants to record a new tag for `name`.
    - `remove_last_requested(name)` -- user wants to undo the most
      recent tag for `name` (right-click on the row).
    """

    tag_clicked = pyqtSignal(str)
    remove_last_requested = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedWidth(SIDEBAR_WIDTH)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(4)
        title = QLabel("Tag Speaker", self)
        title.setStyleSheet("font-weight: bold;")
        outer.addWidget(title)
        hint = QLabel(
            "Click an attendee when they start talking. Right-click to "
            "undo. Used by post-meeting speaker identification.",
            self,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888; font-size: 11px;")
        outer.addWidget(hint)

        self._empty_label = QLabel(
            "(no attendees yet -- add names under '# Attendees' on My Notes)",
            self,
        )
        self._empty_label.setWordWrap(True)
        self._empty_label.setStyleSheet("color: #888; font-style: italic;")
        outer.addWidget(self._empty_label)

        self._rows_container = QWidget(self)
        self._rows_layout = QVBoxLayout(self._rows_container)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(2)
        self._rows_layout.addStretch(1)

        scroll = QScrollArea(self)
        scroll.setWidget(self._rows_container)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll, 1)

        self._rows: dict[str, _AttendeeRow] = {}
        self._counts: dict[str, int] = {}

    def set_attendees(self, names: list[str]) -> None:
        """Rebuild the row list. Counts for names that survive the
        rebuild are preserved; counts for removed names are dropped."""
        clean = sorted(
            {n.strip() for n in names if n and n.strip()},
            key=lambda s: s.lower(),
        )
        # Tear down existing rows.
        for row in list(self._rows.values()):
            self._rows_layout.removeWidget(row)
            row.tag_clicked.disconnect()
            row.remove_last_requested.disconnect()
            row.deleteLater()
        self._rows.clear()
        # Rebuild.
        for name in clean:
            row = _AttendeeRow(name, self._rows_container)
            row.tag_clicked.connect(self.tag_clicked.emit)
            row.remove_last_requested.connect(self.remove_last_requested.emit)
            row.set_count(self._counts.get(name, 0))
            self._rows[name] = row
            self._rows_layout.insertWidget(self._rows_layout.count() - 1, row)
        # Drop counts for names that are no longer in the list.
        self._counts = {k: v for k, v in self._counts.items() if k in self._rows}
        self._empty_label.setVisible(not clean)

    def set_counts(self, counts: dict[str, int]) -> None:
        """Refresh the badge counts. Names not in `counts` reset to 0.

        Counts come from the controller's `speaker_tags_changed` signal
        and may include names that aren't in the current attendee list
        (the user typed a name into My Notes, removed it, and the count
        was already non-zero). Those orphan counts are kept internally
        so a subsequent attendee-list update restores their badge.
        """
        self._counts = dict(counts)
        for name, row in self._rows.items():
            row.set_count(self._counts.get(name, 0))

    def attendee_names(self) -> list[str]:
        return sorted(self._rows.keys(), key=lambda s: s.lower())
