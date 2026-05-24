"""Cross-session search dialog.

Reached via Ctrl+Shift+F from MainWindow. Wraps SearchIndex with a
plain Qt list + filters: query input, source-scope checkboxes (the
four content types -- Transcript / My Notes / Synthesis / Previous
Notes), and a results pane that shows session title + date + source
badge + a snippet with the match highlighted.

Double-clicking (or Enter on) a result emits `result_chosen` with
(session_id, source, archive_name); MainApp routes that to the
right session + tab. Snippet highlighting comes from FTS5's own
snippet() function -- we replace the configured `STX`/`ETX` markers
with HTML `<b>` for Qt rich-text rendering.

The dialog is non-modal so the user can keep it open while clicking
through results; closing it doesn't tear down the underlying
SearchIndex (callers manage that lifecycle).
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..models.search_index import (
    SOURCE_LIVE_NOTES,
    SOURCE_NOTES,
    SOURCE_NOTES_ARCHIVE,
    SOURCE_TRANSCRIPT,
    SNIPPET_END_MARKER,
    SNIPPET_START_MARKER,
    SearchHit,
    SearchIndex,
    escape_fts5_query,
)


_SOURCE_DISPLAY: dict[str, str] = {
    SOURCE_TRANSCRIPT:    "Transcript",
    SOURCE_LIVE_NOTES:    "My Notes",
    SOURCE_NOTES:         "Synthesis",
    SOURCE_NOTES_ARCHIVE: "Previous Notes",
}


@dataclass
class SessionSummary:
    """Minimal session metadata the dialog needs to render a result.

    Provided via a session_lookup callable so the dialog stays
    decoupled from SessionStore -- tests pass a dict; MainApp wires
    a lookup that goes through the store.
    """
    session_id: str
    title: str
    created_at: str       # UTC ISO; rendered to local in the dialog


def format_snippet_html(snippet: str) -> str:
    """Convert FTS5 snippet() output into safe Qt rich-text HTML.

    Strategy: escape the entire snippet, then replace the (also-
    escaped, since they're ASCII control chars and html.escape
    passes them through) STX / ETX markers with `<b>` tags. Order
    matters -- escaping first means a user query like "<script>"
    can't smuggle markup into the snippet.
    """
    if not snippet:
        return ""
    escaped = html.escape(snippet)
    # html.escape leaves ASCII control characters (STX = 0x02,
    # ETX = 0x03) untouched, so the unicode markers we sprinkled in
    # before encoding still match here.
    escaped = escaped.replace(SNIPPET_START_MARKER, "<b>")
    escaped = escaped.replace(SNIPPET_END_MARKER, "</b>")
    return escaped


def format_result_row(
    hit: SearchHit,
    summary: Optional[SessionSummary],
) -> str:
    """Render one result row as Qt rich text.

    Layout:
        <b>Session title</b>  -  YYYY-MM-DD HH:MM  [Source]
        <snippet with <b> highlights>
        <archive filename if a previous-notes hit>
    """
    if summary is None:
        # Result for a session no longer in the store (orphan index
        # row). Show the raw id so the user can recover state.
        title_html = f"<i>Session {html.escape(hit.session_id[:8])}</i>"
        date_html = ""
    else:
        title_html = f"<b>{html.escape(summary.title or 'Untitled')}</b>"
        date_html = _format_local_date(summary.created_at)
    source_label = _SOURCE_DISPLAY.get(hit.source, hit.source)
    header_parts = [title_html]
    if date_html:
        header_parts.append(date_html)
    header_parts.append(f"<span style='color: gray;'>[{source_label}]</span>")
    header = " &nbsp; ".join(header_parts)
    snippet_html = format_snippet_html(hit.snippet)
    body = f"{header}<br/>{snippet_html}"
    if hit.source == SOURCE_NOTES_ARCHIVE and hit.archive_name:
        body += (
            f"<br/><span style='color: gray; font-size: 90%;'>"
            f"{html.escape(hit.archive_name)}</span>"
        )
    return body


def _format_local_date(created_at_utc_iso: str) -> str:
    """UTC ISO -> 'YYYY-MM-DD HH:MM' in local time, defensive on
    bad input."""
    if not created_at_utc_iso:
        return ""
    try:
        utc_aware = datetime.fromisoformat(
            created_at_utc_iso.replace("Z", "+00:00")
        )
    except ValueError:
        return ""
    return utc_aware.astimezone().strftime("%Y-%m-%d %H:%M")


class SearchDialog(QDialog):
    """Non-modal cross-session search dialog."""

    # session_id, source, archive_name (None for non-archive hits).
    result_chosen = pyqtSignal(str, str, object)

    def __init__(
        self,
        index: SearchIndex,
        session_lookup: Callable[[str], Optional[SessionSummary]],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._index = index
        self._session_lookup = session_lookup

        self.setWindowTitle("Search Sessions")
        self.setModal(False)
        self.resize(720, 560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        # Query input.
        query_row = QHBoxLayout()
        query_row.addWidget(QLabel("Search:", self))
        self._input = QLineEdit(self)
        self._input.setPlaceholderText(
            "Type a word or phrase. Bare words match by prefix; "
            "use quotes for phrase match."
        )
        self._input.textChanged.connect(self._on_query_changed)
        query_row.addWidget(self._input, 1)
        layout.addLayout(query_row)

        # Scope checkboxes.
        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("Search in:", self))
        self._source_checks: dict[str, QCheckBox] = {}
        for src in (SOURCE_TRANSCRIPT, SOURCE_LIVE_NOTES, SOURCE_NOTES, SOURCE_NOTES_ARCHIVE):
            cb = QCheckBox(_SOURCE_DISPLAY[src], self)
            cb.setChecked(True)
            cb.stateChanged.connect(self._on_scope_changed)
            scope_row.addWidget(cb)
            self._source_checks[src] = cb
        scope_row.addStretch(1)
        layout.addLayout(scope_row)

        # Results list. setItemWidget would let us put rich-text
        # QLabels in each row, but QListWidget's built-in rich-text
        # support via Qt::TextFormat (set via item.setData
        # + a delegate) is simpler. The QLabel route gives the
        # cleanest wrapping behavior.
        self._results = QListWidget(self)
        self._results.setWordWrap(True)
        self._results.setUniformItemSizes(False)
        self._results.itemDoubleClicked.connect(self._on_result_activated)
        self._results.itemActivated.connect(self._on_result_activated)
        layout.addWidget(self._results, 1)

        # Status footer.
        self._status = QLabel("", self)
        self._status.setStyleSheet("color: gray;")
        layout.addWidget(self._status)

        # Debounce typing so we don't re-query on every keystroke.
        # 180ms feels instant but absorbs touch-typed bursts.
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(180)
        self._debounce.timeout.connect(self._run_search)

    # ---- public ----
    def focus_input(self) -> None:
        self._input.setFocus(Qt.FocusReason.OtherFocusReason)
        self._input.selectAll()

    # ---- key events ----
    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt API)
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    # ---- search ----
    def _on_query_changed(self, _text: str) -> None:
        self._debounce.start()

    def _on_scope_changed(self, _state: int) -> None:
        # Source change re-runs immediately; the user already paused
        # typing if they're toggling checkboxes.
        self._run_search()

    def _active_sources(self) -> list[str]:
        return [src for src, cb in self._source_checks.items() if cb.isChecked()]

    def _run_search(self) -> None:
        self._results.clear()
        query = self._input.text().strip()
        if not query:
            self._status.setText("")
            return
        sources = self._active_sources()
        if not sources:
            self._status.setText("Pick at least one source above.")
            return
        try:
            hits = self._index.search(
                escape_fts5_query(query), sources=sources, limit=200,
            )
        except Exception as exc:
            self._status.setText(f"Search error: {exc}")
            return
        if not hits:
            self._status.setText("No matches.")
            return
        self._status.setText(f"{len(hits)} match(es).")
        for hit in hits:
            self._add_hit_row(hit)

    def _add_hit_row(self, hit: SearchHit) -> None:
        summary = self._session_lookup(hit.session_id)
        text_html = format_result_row(hit, summary)
        # Carry the routing payload on the item itself so the
        # double-click handler doesn't need a parallel list.
        item = QListWidgetItem("", self._results)
        item.setData(Qt.ItemDataRole.UserRole, (
            hit.session_id, hit.source, hit.archive_name,
        ))
        self._results.addItem(item)
        label = QLabel(text_html, self._results)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setWordWrap(True)
        label.setContentsMargins(6, 4, 6, 4)
        self._results.setItemWidget(item, label)
        item.setSizeHint(label.sizeHint())

    def _on_result_activated(self, item: QListWidgetItem) -> None:
        payload = item.data(Qt.ItemDataRole.UserRole)
        if not payload:
            return
        session_id, source, archive_name = payload
        self.result_chosen.emit(session_id, source, archive_name)
