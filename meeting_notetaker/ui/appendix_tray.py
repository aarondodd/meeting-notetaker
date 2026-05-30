"""Collapsible Appendix tray below My Notes / Synthesis (#64).

Mirrors the AttendeeDetailsDrawer pattern (collapsed header +
expand chevron + per-section sub-panels) but lives at the bottom
of the editor area instead of the top. Each section renders one
data source as a friendly table or list:

* Attendee Context (LLM, JSON appendix)  -- #63
* Attendee Details (LLM, JSON appendix)  -- #51
* Suggested Topics (LLM, JSON appendix)  -- #57
* Referenced Attachments (LLM)           -- #64
* Session Attachments (live mirror)      -- #64
* Links (scraped from notes + live notes) -- #64

The tray reads the same raw appendix blocks the preview / export
transformation does. Hand it pre-parsed ``AppendixData`` via
``set_data`` -- the widget itself doesn't touch any store or
parser, which keeps it cheap to unit-test.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..utils.appendix_transform import AppendixData


class AppendixTray(QWidget):
    """Collapsible multi-section read-only summary panel."""

    # Emitted when the user clicks the header's Edit button. MainApp
    # opens the AppendixEditDialog seeded from the sidecar; the
    # dialog handles persisting back to the sidecar + regenerating
    # notes.md JSON blocks.
    edit_requested = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header row: toggle + label + count.
        header = QFrame(self)
        header.setStyleSheet(
            "QFrame { background-color: palette(alternate-base); }"
        )
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(6, 4, 6, 4)
        header_layout.setSpacing(8)
        self._toggle = QPushButton("▶", header)
        self._toggle.setFlat(True)
        self._toggle.setFixedWidth(20)
        self._toggle.clicked.connect(self._on_toggle)
        header_layout.addWidget(self._toggle)
        self._title = QLabel("Appendix (0)", header)
        font = QFont(self._title.font())
        font.setBold(True)
        self._title.setFont(font)
        header_layout.addWidget(self._title)
        header_layout.addStretch(1)
        # "Edit..." opens the AppendixEditDialog for the four LLM
        # sections. The button is enabled whenever the tray carries
        # any LLM-derived rows -- empty tray has nothing to edit.
        self._edit_btn = QPushButton("Edit...", header)
        self._edit_btn.setFlat(True)
        self._edit_btn.setEnabled(False)
        self._edit_btn.clicked.connect(self.edit_requested.emit)
        header_layout.addWidget(self._edit_btn)
        layout.addWidget(header)

        # Body: a scroll area whose viewport holds one block per
        # populated section. Hidden by default.
        self._body = QScrollArea(self)
        self._body.setWidgetResizable(True)
        self._body.setFrameShape(QFrame.Shape.NoFrame)
        self._body_inner = QWidget(self._body)
        self._body_layout = QVBoxLayout(self._body_inner)
        self._body_layout.setContentsMargins(6, 4, 6, 6)
        self._body_layout.setSpacing(8)
        self._body.setWidget(self._body_inner)
        self._body.setVisible(False)
        self._body.setMinimumHeight(140)
        layout.addWidget(self._body)

        self._is_expanded = False
        # Track total entries across sections for the header count.
        self._total_entries = 0

    # ---- public API -------------------------------------------------

    def set_data(self, data: AppendixData) -> None:
        """Refresh every section from a freshly-parsed payload."""
        # Tear down existing widgets.
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        # Re-populate.
        sections_added = 0
        total = 0
        if data.attendee_context:
            total += len(data.attendee_context)
            self._body_layout.addWidget(_build_attendee_context_section(
                data.attendee_context, self._body_inner,
            ))
            sections_added += 1
        if data.attendee_details:
            total += len(data.attendee_details)
            self._body_layout.addWidget(_build_attendee_details_section(
                data.attendee_details, self._body_inner,
            ))
            sections_added += 1
        if data.topics:
            total += len(data.topics)
            self._body_layout.addWidget(_build_topics_section(
                data.topics, self._body_inner,
            ))
            sections_added += 1
        if data.referenced_attachments:
            total += len(data.referenced_attachments)
            self._body_layout.addWidget(
                _build_referenced_attachments_section(
                    data.referenced_attachments, self._body_inner,
                )
            )
            sections_added += 1
        if data.session_attachments:
            total += len(data.session_attachments)
            self._body_layout.addWidget(
                _build_session_attachments_section(
                    data.session_attachments, self._body_inner,
                )
            )
            sections_added += 1
        if data.links:
            total += len(data.links)
            self._body_layout.addWidget(_build_links_section(
                data.links, self._body_inner,
            ))
            sections_added += 1
        # Fill bottom space so the inner widget doesn't get squished
        # when sections are short.
        self._body_layout.addStretch(1)
        self._total_entries = total
        self._title.setText(f"Appendix ({total})")
        # Edit button only makes sense when there's LLM-derived
        # content; Session Attachments + Links are read-only
        # mirrors of data that lives elsewhere.
        llm_total = (
            len(data.attendee_context)
            + len(data.attendee_details)
            + len(data.topics)
            + len(data.referenced_attachments)
        )
        self._edit_btn.setEnabled(llm_total > 0)
        # Auto-collapse when there's nothing to show so the empty
        # tray doesn't take vertical space the user can't dismiss.
        if total == 0 and self._is_expanded:
            self.set_expanded(False)
        self._sections_added = sections_added

    def is_expanded(self) -> bool:
        return self._is_expanded

    def set_expanded(self, expanded: bool) -> None:
        self._is_expanded = bool(expanded)
        self._toggle.setText("▼" if self._is_expanded else "▶")
        self._body.setVisible(self._is_expanded)

    # ---- internal ---------------------------------------------------

    def _on_toggle(self) -> None:
        self.set_expanded(not self._is_expanded)


# ---------------------------------------------------------------------
# Section builders -- each returns a self-contained QFrame the tray
# can drop into its body layout. Kept module-level so tests can poke
# them in isolation if needed.


def _section_heading(text: str, parent) -> QLabel:
    label = QLabel(text, parent)
    f = QFont(label.font())
    f.setBold(True)
    label.setFont(f)
    label.setStyleSheet("padding-top: 2px;")
    return label


def _build_attendee_context_section(entries, parent) -> QFrame:
    frame = QFrame(parent)
    v = QVBoxLayout(frame)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(2)
    v.addWidget(_section_heading(
        f"Attendee Context ({len(entries)})", frame,
    ))
    table = QTableWidget(len(entries), 2, frame)
    table.setHorizontalHeaderLabels(["Name", "Observation"])
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
    table.setAlternatingRowColors(True)
    h = table.horizontalHeader()
    h.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
    h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
    for row, e in enumerate(entries):
        table.setItem(row, 0, QTableWidgetItem(e.name))
        table.setItem(row, 1, QTableWidgetItem(e.observation))
    _size_to_contents(table)
    v.addWidget(table)
    return frame


def _build_attendee_details_section(entries, parent) -> QFrame:
    frame = QFrame(parent)
    v = QVBoxLayout(frame)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(2)
    v.addWidget(_section_heading(
        f"Attendee Details ({len(entries)})", frame,
    ))
    headers = ["Name", "Title", "Company", "Email", "Phone"]
    table = QTableWidget(len(entries), len(headers), frame)
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
    table.setAlternatingRowColors(True)
    h = table.horizontalHeader()
    for col in range(len(headers) - 1):
        h.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
    h.setSectionResizeMode(len(headers) - 1, QHeaderView.ResizeMode.Stretch)
    for row, e in enumerate(entries):
        table.setItem(row, 0, QTableWidgetItem(e.name))
        table.setItem(row, 1, QTableWidgetItem(e.title))
        table.setItem(row, 2, QTableWidgetItem(e.company))
        table.setItem(row, 3, QTableWidgetItem(e.email))
        table.setItem(row, 4, QTableWidgetItem(e.phone))
    _size_to_contents(table)
    v.addWidget(table)
    return frame


def _build_topics_section(topics, parent) -> QFrame:
    frame = QFrame(parent)
    v = QVBoxLayout(frame)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(2)
    v.addWidget(_section_heading(
        f"Suggested Topics ({len(topics)})", frame,
    ))
    for t in topics:
        v.addWidget(QLabel(f"  - {t}", frame))
    return frame


def _build_referenced_attachments_section(entries, parent) -> QFrame:
    frame = QFrame(parent)
    v = QVBoxLayout(frame)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(2)
    v.addWidget(_section_heading(
        f"Referenced Attachments ({len(entries)})", frame,
    ))
    table = QTableWidget(len(entries), 2, frame)
    table.setHorizontalHeaderLabels(["Name", "Context"])
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
    table.setAlternatingRowColors(True)
    h = table.horizontalHeader()
    h.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
    h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
    for row, e in enumerate(entries):
        table.setItem(row, 0, QTableWidgetItem(e.name))
        table.setItem(row, 1, QTableWidgetItem(e.context))
    _size_to_contents(table)
    v.addWidget(table)
    return frame


def _build_session_attachments_section(names, parent) -> QFrame:
    frame = QFrame(parent)
    v = QVBoxLayout(frame)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(2)
    v.addWidget(_section_heading(
        f"Session Attachments ({len(names)})", frame,
    ))
    for n in names:
        v.addWidget(QLabel(f"  - {n}", frame))
    return frame


def _build_links_section(links, parent) -> QFrame:
    frame = QFrame(parent)
    v = QVBoxLayout(frame)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(2)
    v.addWidget(_section_heading(
        f"Links ({len(links)})", frame,
    ))
    for link in links:
        label_text = link.label or link.url
        # Anchor tags render as clickable text in a QLabel when
        # openExternalLinks is True. Keeps the source attribution
        # in a faint trailing pill so users can tell synthesis-vs-
        # notes provenance at a glance.
        anchor = (
            f'<a href="{link.url}">{label_text}</a>'
            f' <span style="color: #888;">({link.source})</span>'
        )
        item = QLabel(f"  - {anchor}", frame)
        item.setOpenExternalLinks(True)
        item.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        v.addWidget(item)
    return frame


def _size_to_contents(table: QTableWidget) -> None:
    """Cap the table height to fit the rows without scroll.

    QTableWidget defaults to a fixed height; small per-section
    tables look cleaner when they hug their row count. The tray's
    outer scroll area handles overflow if the whole tray is too
    long.
    """
    if table.rowCount() == 0:
        table.setFixedHeight(table.horizontalHeader().height() + 4)
        return
    row_total = sum(
        table.rowHeight(r) for r in range(table.rowCount())
    )
    header_h = table.horizontalHeader().height()
    table.setFixedHeight(row_total + header_h + 8)
