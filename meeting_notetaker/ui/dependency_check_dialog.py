"""Help > Debug > Check dependencies dialog.

Renders the result of `utils.dependency_check.run_checks()` as a
grouped tree, with colored status badges and a copy-report-to-clipboard
button so users can paste a full report when reporting a build issue.

Re-run is exposed as a button rather than auto-refresh -- the import
machinery caches modules, so a "rerun" only changes the picture if the
user just edited the venv outside the running app.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor, QFont, QGuiApplication
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..utils.dependency_check import (
    DependencyResult,
    Status,
    format_report,
    run_checks,
    summary,
)


_COLOR_OK = QColor("#2e7d32")
_COLOR_MISSING = QColor("#c62828")
_COLOR_SKIP = QColor("#777777")


_STATUS_COLOR = {
    Status.OK: _COLOR_OK,
    Status.MISSING: _COLOR_MISSING,
    Status.SKIP: _COLOR_SKIP,
}


class DependencyCheckDialog(QDialog):
    """Non-modal self-test dialog. Safe to leave open mid-meeting."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Dependency check")
        self.resize(720, 520)

        layout = QVBoxLayout(self)

        self._summary_label = QLabel("Running checks...", self)
        font = self._summary_label.font()
        font.setBold(True)
        self._summary_label.setFont(font)
        layout.addWidget(self._summary_label)

        self._intro = QLabel(
            "Verifies that every external library the app uses can be "
            "imported in this environment. Use this to confirm a frozen "
            ".exe build bundled all dependencies, or to spot a broken "
            "venv. MISSING rows indicate a feature that will fail at "
            "runtime; SKIP rows are platform-conditional (e.g. "
            "Windows-only) and expected on this OS.",
            self,
        )
        self._intro.setWordWrap(True)
        layout.addWidget(self._intro)

        self._tree = QTreeWidget(self)
        self._tree.setColumnCount(3)
        self._tree.setHeaderLabels(["Dependency", "Status", "Detail"])
        self._tree.setUniformRowHeights(True)
        self._tree.setRootIsDecorated(True)
        header = self._tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._tree, 1)

        button_row = QHBoxLayout()
        self._rerun_btn = QPushButton("Re-run", self)
        self._rerun_btn.clicked.connect(self._run)
        button_row.addWidget(self._rerun_btn)
        self._copy_btn = QPushButton("Copy report", self)
        self._copy_btn.clicked.connect(self._copy_report)
        button_row.addWidget(self._copy_btn)
        button_row.addStretch(1)
        self._close_btn = QPushButton("Close", self)
        self._close_btn.clicked.connect(self.close)
        button_row.addWidget(self._close_btn)
        layout.addLayout(button_row)

        self._last_report = ""
        self._run()

    def _run(self) -> None:
        self._tree.clear()
        grouped = run_checks()
        for group_name, results in grouped:
            group_item = QTreeWidgetItem([group_name, "", ""])
            group_font = group_item.font(0)
            group_font.setBold(True)
            group_item.setFont(0, group_font)
            group_item.setFirstColumnSpanned(False)
            self._tree.addTopLevelItem(group_item)
            group_item.setExpanded(True)
            for r in results:
                child = self._build_child(r)
                group_item.addChild(child)
            # Roll up the group's worst status into the badge column.
            statuses = {r.status for r in results}
            if Status.MISSING in statuses:
                self._tint_status(group_item, "MISSING", Status.MISSING)
            elif Status.OK in statuses:
                self._tint_status(group_item, "OK", Status.OK)
            else:
                self._tint_status(group_item, "skip", Status.SKIP)

        counts = summary(grouped)
        self._summary_label.setText(
            f"{counts[Status.OK]} OK    "
            f"{counts[Status.MISSING]} MISSING    "
            f"{counts[Status.SKIP]} skipped"
        )
        self._last_report = format_report(grouped)

    @staticmethod
    def _build_child(r: DependencyResult) -> QTreeWidgetItem:
        # Item columns: dependency name, status text, detail
        status_text = {
            Status.OK: "OK",
            Status.MISSING: "MISSING",
            Status.SKIP: "skip",
        }[r.status]
        item = QTreeWidgetItem([r.name, status_text, r.detail])
        item.setToolTip(0, r.feature)
        item.setToolTip(2, r.detail)
        DependencyCheckDialog._tint_status(item, status_text, r.status)
        # Monospace the detail column so version strings line up.
        mono = QFont("Courier")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        item.setFont(2, mono)
        return item

    @staticmethod
    def _tint_status(item: QTreeWidgetItem, text: str, status: Status) -> None:
        item.setText(1, text)
        brush = QBrush(_STATUS_COLOR[status])
        item.setForeground(1, brush)
        font = item.font(1)
        font.setBold(True)
        item.setFont(1, font)

    def _copy_report(self) -> None:
        if not self._last_report:
            return
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._last_report)
            self._copy_btn.setText("Copied")
            # Reset the label after a beat -- no QTimer dependency needed
            # since this is a transient cue; the next interaction can do it.
            self._copy_btn.repaint()
