"""Speakers store management dialog (Settings > Speakers > Manage).

Lists every known speaker with sample count + last seen date. Per-row
Rename and Forget actions. Bottom-bar Forget All. Mutates the speaker
store directly (atomic per row); no separate apply step.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..diarization.store import SpeakerStore


class SpeakersManageDialog(QDialog):
    """Edit the speakers.db identity store.

    Constructed with a SpeakerStore the caller has already opened.
    All mutations write through directly; closing the dialog with X
    or Close commits nothing extra.
    """

    def __init__(self, store: SpeakerStore, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._store = store
        self.setWindowTitle("Manage Speakers")
        self.resize(560, 420)

        layout = QVBoxLayout(self)

        blurb = QLabel(
            "Known voices learned during post-meeting refinement. Each "
            "row's centroid embedding gets updated as a running average "
            "every time you confirm the same speaker again, so the "
            "match quality improves over meetings. Rename a row to "
            "correct a name typo; Forget removes the row so future "
            "meetings stop auto-recognizing that voice.",
            self,
        )
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        self._table = QTableWidget(self)
        self._table.setColumnCount(4)
        self._table.setHorizontalHeaderLabels(["Name", "Samples", "Last seen", "Actions"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self._table, 1)

        # Forget All + Close.
        button_row = QHBoxLayout()
        self._forget_all_btn = QPushButton("Forget All...", self)
        self._forget_all_btn.setToolTip(
            "Remove every stored speaker. Next meeting starts from a "
            "fresh slate; existing diarization.json files keep their "
            "current names but won't be reinforced."
        )
        self._forget_all_btn.clicked.connect(self._on_forget_all)
        button_row.addWidget(self._forget_all_btn)
        button_row.addStretch(1)
        close_btn = QPushButton("Close", self)
        close_btn.clicked.connect(self.accept)
        button_row.addWidget(close_btn)
        layout.addLayout(button_row)

        self._refresh_table()

    def _refresh_table(self) -> None:
        records = self._store.list_all()
        self._table.setRowCount(len(records))
        for row, rec in enumerate(records):
            name_item = QTableWidgetItem(rec.name)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, 0, name_item)

            sample_item = QTableWidgetItem(str(rec.sample_count))
            sample_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 1, sample_item)

            last_seen_item = QTableWidgetItem(self._friendly_date(rec.last_seen_at))
            self._table.setItem(row, 2, last_seen_item)

            actions_widget = QWidget(self)
            actions_layout = QHBoxLayout(actions_widget)
            actions_layout.setContentsMargins(2, 2, 2, 2)
            actions_layout.setSpacing(4)
            rename_btn = QPushButton("Rename", self)
            rename_btn.clicked.connect(lambda _, n=rec.name: self._on_rename(n))
            forget_btn = QPushButton("Forget", self)
            forget_btn.clicked.connect(lambda _, n=rec.name: self._on_forget(n))
            actions_layout.addWidget(rename_btn)
            actions_layout.addWidget(forget_btn)
            self._table.setCellWidget(row, 3, actions_widget)

        if not records:
            # No rows yet; show a single empty-state pseudo-row.
            self._table.setRowCount(1)
            empty = QTableWidgetItem("(no speakers stored yet)")
            empty.setForeground(Qt.GlobalColor.gray)
            font = empty.font()
            font.setItalic(True)
            empty.setFont(font)
            empty.setFlags(empty.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(0, 0, empty)
            for col in range(1, 4):
                self._table.setItem(0, col, QTableWidgetItem(""))
            self._forget_all_btn.setEnabled(False)
        else:
            self._forget_all_btn.setEnabled(True)

    def _on_rename(self, name: str) -> None:
        new_name, ok = QInputDialog.getText(
            self,
            "Rename speaker",
            f"Rename '{name}' to:",
            text=name,
        )
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == name:
            return
        if self._store.get_by_name(new_name) is not None:
            QMessageBox.warning(
                self,
                "Rename speaker",
                f"A speaker named '{new_name}' already exists. "
                "Pick a different name or forget the existing one first.",
            )
            return
        self._store.rename(name, new_name)
        self._refresh_table()

    def _on_forget(self, name: str) -> None:
        confirm = QMessageBox.question(
            self,
            "Forget speaker",
            f"Forget '{name}'? Future meetings will treat this voice "
            "as a new unknown speaker until you label it again.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._store.forget(name)
        self._refresh_table()

    def _on_forget_all(self) -> None:
        confirm = QMessageBox.question(
            self,
            "Forget all speakers",
            "Remove every stored speaker? This cannot be undone. "
            "Existing transcripts keep their current labels; only "
            "future auto-recognition is affected.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._store.forget_all()
        self._refresh_table()

    @staticmethod
    def _friendly_date(iso: str) -> str:
        if not iso:
            return ""
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except ValueError:
            return iso
        return dt.strftime("%Y-%m-%d")
