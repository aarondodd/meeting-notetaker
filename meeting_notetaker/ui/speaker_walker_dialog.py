"""Walker dialog for labeling and reviewing diarized speakers.

Drives both flows:

- **Label Unknown Speakers** -- pops automatically after the post-meeting
  refinement pass when one or more clusters didn't match a known speaker.
  The user assigns names; the embeddings get written to the speaker
  store so the next meeting auto-recognizes them.

- **Review Speakers** -- a session-view button. Walks every detected
  cluster (known + unknown) so the user can correct mistakes
  ("this was actually Alice, not Bob") and feed the correction back
  into the store.

Both flows share the same widget tree -- the differences are which
clusters are shown (unknown-only vs all) and the action label
("Skip" vs "Confirm"). The dialog reads the per-cluster context
(example transcript lines, centroid, optional sample audio span)
from a list of `SpeakerWalkerEntry` records the caller builds.

The dialog does not mutate the speaker store or the transcript directly.
On accept it returns a list of `(cluster_id, name | None)` decisions,
plus the centroid for each decided cluster, so the caller can do the
follow-up writes inside a single transaction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


@dataclass
class SpeakerWalkerEntry:
    """One row in the walker.

    `centroid` is the cluster's embedding centroid; the caller uses it
    to feed the speaker store when the user confirms a name.
    """

    cluster_id: int
    current_name: Optional[str]      # None for unknown clusters
    example_lines: list[str]         # 1-5 short transcript lines
    centroid: np.ndarray
    match_similarity: Optional[float] = None
    # Suggested names for the combo box, typically derived from Outlook
    # attendees + known speakers.
    suggestions: list[str] = field(default_factory=list)
    # In-meeting click-to-tag provenance. When True, the card renders a
    # "tagged during meeting at ..." badge so the reviewer knows the
    # name came from the user's own clicks, not a cosine match.
    was_user_tagged: bool = False
    tag_times_seconds: list[float] = field(default_factory=list)


@dataclass
class SpeakerWalkerDecision:
    """User's choice for one entry.

    `name`:
      - None -> Skip (or Forget for known); leave the cluster as it is.
      - "" (empty string) -> caller treats this as "no change".
      - any non-empty string -> Apply this name to the cluster; the
        caller writes the centroid to the speaker store under this name.
    `should_forget` is True only in Review mode when the user explicitly
    asked to remove a previously-assigned name from this cluster.
    """

    cluster_id: int
    name: Optional[str]
    centroid: np.ndarray
    should_forget: bool = False


_SKIP_TEXT_LABEL = "Skip -- leave this speaker unlabeled for now"
_FORGET_TEXT_LABEL = "Forget -- remove this name from the speaker store"


class _SpeakerCard(QFrame):
    """One cluster's labeling card. Held inside the scroll area."""

    def __init__(self, entry: SpeakerWalkerEntry, *, mode: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.entry = entry
        self.mode = mode
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFrameShadow(QFrame.Shadow.Raised)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)

        # Header: cluster id + match confidence + current name.
        header = QHBoxLayout()
        title_font = QFont()
        title_font.setBold(True)
        title_label = QLabel(self._header_text(), self)
        title_label.setFont(title_font)
        header.addWidget(title_label, 1)
        if entry.was_user_tagged and entry.tag_times_seconds:
            sim_label = QLabel(
                f"tagged during meeting at {_format_times(entry.tag_times_seconds)}",
                self,
            )
            sim_label.setStyleSheet("color: #4a8;")
            header.addWidget(sim_label)
        elif entry.match_similarity is not None and entry.current_name:
            sim_label = QLabel(
                f"matched at {entry.match_similarity * 100:.0f}% similarity",
                self,
            )
            sim_label.setStyleSheet("color: #888;")
            header.addWidget(sim_label)
        layout.addLayout(header)

        # Example transcript lines.
        if entry.example_lines:
            sample = QPlainTextEdit(self)
            sample.setReadOnly(True)
            sample.setMaximumHeight(120)
            sample.setPlainText("\n".join(entry.example_lines))
            mono = QFont("Consolas")
            mono.setStyleHint(QFont.StyleHint.Monospace)
            mono.setPointSize(9)
            sample.setFont(mono)
            layout.addWidget(sample)
        else:
            note = QLabel("(no example transcript lines for this cluster)", self)
            note.setStyleSheet("color: #888; font-style: italic;")
            layout.addWidget(note)

        # Name picker: editable combo box seeded with suggestions.
        form = QFormLayout()
        self._name_combo = QComboBox(self)
        self._name_combo.setEditable(True)
        seen = set()
        for s in entry.suggestions:
            if s and s not in seen:
                self._name_combo.addItem(s)
                seen.add(s)
        if entry.current_name and entry.current_name not in seen:
            self._name_combo.insertItem(0, entry.current_name)
        # Default to the existing label in Review mode (so Enter == Confirm);
        # empty in Label mode so the user types fresh.
        if mode == "review" and entry.current_name:
            self._name_combo.setCurrentText(entry.current_name)
        else:
            self._name_combo.setCurrentText("")
        form.addRow("Name:", self._name_combo)
        layout.addLayout(form)

        # Action buttons. Skip and (review-only) Forget; the dialog-level
        # OK/Cancel handles wholesale accept/reject.
        actions = QHBoxLayout()
        self._skip_btn = QPushButton(_SKIP_TEXT_LABEL, self)
        self._skip_btn.setToolTip("Clear the name field for this cluster.")
        self._skip_btn.clicked.connect(self._on_skip_clicked)
        actions.addWidget(self._skip_btn)
        if mode == "review" and entry.current_name:
            self._forget_btn = QPushButton(_FORGET_TEXT_LABEL, self)
            self._forget_btn.setToolTip(
                "Remove this name from the cluster. Does not affect the "
                "speaker store; use Settings > Speakers to delete a "
                "stored speaker."
            )
            self._forget_btn.clicked.connect(self._on_forget_clicked)
            actions.addWidget(self._forget_btn)
        else:
            self._forget_btn = None
        actions.addStretch(1)
        layout.addLayout(actions)

        self._should_forget = False

    def _header_text(self) -> str:
        if self.entry.current_name:
            return f"Cluster {self.entry.cluster_id + 1}: {self.entry.current_name}"
        return f"Cluster {self.entry.cluster_id + 1}: unlabeled"

    def _on_skip_clicked(self) -> None:
        self._name_combo.setCurrentText("")
        self._should_forget = False

    def _on_forget_clicked(self) -> None:
        self._name_combo.setCurrentText("")
        self._should_forget = True

    def decision(self) -> SpeakerWalkerDecision:
        raw = self._name_combo.currentText().strip()
        name: Optional[str] = raw or None
        return SpeakerWalkerDecision(
            cluster_id=self.entry.cluster_id,
            name=name,
            centroid=self.entry.centroid,
            should_forget=self._should_forget and not name,
        )


class SpeakerWalkerDialog(QDialog):
    """Dialog that walks the user through cluster labeling.

    `mode`:
        - "label"  -- only unlabeled clusters shown; suitable for the
          auto-pop after post-meeting refinement.
        - "review" -- all clusters shown; suitable for manual correction.
    """

    def __init__(
        self,
        entries: list[SpeakerWalkerEntry],
        *,
        mode: str = "label",
        session_title: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        if mode not in ("label", "review"):
            raise ValueError(f"unknown walker mode: {mode!r}")
        self.mode = mode
        if mode == "label":
            self.setWindowTitle("Label Unknown Speakers")
            blurb_text = (
                "The post-meeting refinement found "
                f"{len(entries)} unrecognized voice(s) in this recording. "
                "Pick a name for each; the app will remember them so "
                "future meetings auto-recognize the same speaker. Skip "
                "any you don't want to label."
            )
        else:
            self.setWindowTitle("Review Speakers")
            blurb_text = (
                f"Reviewing {len(entries)} detected speaker(s). "
                "Rename a mislabeled cluster, forget one to clear its "
                "name, or leave the field as-is and click OK to keep "
                "everything unchanged."
            )

        if session_title:
            self.setWindowTitle(f"{self.windowTitle()} -- {session_title}")

        layout = QVBoxLayout(self)
        blurb = QLabel(blurb_text, self)
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        # Empty state.
        if not entries:
            empty = QLabel(
                "No speakers to label."
                if mode == "label"
                else "No speakers detected for this session yet.",
                self,
            )
            empty.setStyleSheet("color: #888; padding: 12px;")
            layout.addWidget(empty)
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok, parent=self
            )
            buttons.accepted.connect(self.accept)
            layout.addWidget(buttons)
            self._cards: list[_SpeakerCard] = []
            self.resize(560, 280)
            return

        # Cards in a scroll area so 10+ clusters don't blow out the window.
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll_content = QWidget(scroll)
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(4, 4, 4, 4)
        scroll_layout.setSpacing(8)
        self._cards: list[_SpeakerCard] = []
        for entry in entries:
            card = _SpeakerCard(entry, mode=mode, parent=scroll_content)
            scroll_layout.addWidget(card)
            self._cards.append(card)
        scroll_layout.addStretch(1)
        scroll.setWidget(scroll_content)
        layout.addWidget(scroll, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(640, 560)

    def decisions(self) -> list[SpeakerWalkerDecision]:
        """Return the user's choices after the dialog accepts."""
        return [card.decision() for card in self._cards]


def _format_times(seconds: list[float]) -> str:
    """Render a list of WAV-elapsed seconds as a comma-separated HH:MM:SS list."""
    parts = []
    for s in seconds:
        s = max(0.0, float(s))
        hours = int(s // 3600)
        minutes = int((s % 3600) // 60)
        secs = int(s % 60)
        if hours:
            parts.append(f"{hours}:{minutes:02d}:{secs:02d}")
        else:
            parts.append(f"{minutes}:{secs:02d}")
    return ", ".join(parts)
