"""Main window -- session list (left) + SessionView (right)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

from PyQt6.QtCore import QDateTime, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMenuBar,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..models.session import (
    STATE_COMPLETE,
    STATE_ERROR,
    STATE_NEW,
    STATE_PAUSED,
    STATE_PROCESSING,
    STATE_RECORDING,
    Session,
)
from ..utils.icons import app_icon
from ..utils.paths import has_retained_audio
from .session_view import SessionView
from .status_indicators import SegmentState, StatusSegment


# Status-bar segment keys, in left-to-right display order. v0.6.5
# drops the informational Mic / Sys pills (no actionable state to
# show); device names live in Settings now.
_STATUS_SEGMENT_KEYS = ("cal", "voice", "det", "syn")


# Per-state cell content + tooltip for the transcription-state column.
# The column communicates the full pipeline: live capture, refinement
# (the long faster-whisper pass after Stop), and final state.
_STATE_BADGE: dict[str, tuple[str, str]] = {
    STATE_NEW:        ("",   "Ready -- not yet started"),
    STATE_RECORDING:  ("🔴", "Recording"),
    STATE_PAUSED:     ("⏸",  "Paused"),
    STATE_PROCESSING: ("🟡", "Refining transcript (final faster-whisper pass)"),
    STATE_COMPLETE:   ("🟢", "Transcript refined"),
    STATE_ERROR:      ("❌", "Error -- partial transcript may exist"),
}

_COL_AUDIO = 0
_COL_SLIDES = 1
_COL_STATE = 2
_COL_DATE = 3
_COL_TITLE = 4


class MainWindow(QMainWindow):
    new_session_requested = pyqtSignal()
    open_settings_requested = pyqtSignal()
    open_devices_dialog_requested = pyqtSignal()
    open_outlook_diagnostic_requested = pyqtSignal()
    open_log_viewer_requested = pyqtSignal()
    open_dependency_check_requested = pyqtSignal()
    open_about_requested = pyqtSignal()
    open_user_guide_requested = pyqtSignal()
    check_for_updates_requested = pyqtSignal()
    upgrade_requested = pyqtSignal()
    quit_requested = pyqtSignal()
    delete_sessions_requested = pyqtSignal(list)   # list of session_ids
    rename_session_requested = pyqtSignal(str, str)  # session_id, new_title
    edit_session_timestamp_requested = pyqtSignal(str, str)  # session_id, new_created_at_iso (UTC)
    open_recording_requested = pyqtSignal(str)     # session_id
    export_recording_requested = pyqtSignal(str)   # session_id
    delete_recording_requested = pyqtSignal(str)   # session_id
    session_selected = pyqtSignal(str)             # session_id

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Meeting Notetaker")
        self.setWindowIcon(app_icon())
        self.resize(1024, 720)

        # Menu bar
        menubar = self.menuBar()
        file_menu = menubar.addMenu("&File")
        action_new = QAction("&New Session...", self)
        action_new.setShortcut("Ctrl+N")
        action_new.triggered.connect(self.new_session_requested.emit)
        file_menu.addAction(action_new)
        action_settings = QAction("&Settings...", self)
        action_settings.setShortcut("Ctrl+,")
        action_settings.triggered.connect(self.open_settings_requested.emit)
        file_menu.addAction(action_settings)
        file_menu.addSeparator()
        action_quit = QAction("&Quit", self)
        action_quit.setShortcut("Ctrl+Q")
        action_quit.triggered.connect(self.quit_requested.emit)
        file_menu.addAction(action_quit)

        help_menu = menubar.addMenu("&Help")
        debug_menu = help_menu.addMenu("&Debug")
        action_devices = QAction("&Audio Devices...", self)
        action_devices.triggered.connect(self.open_devices_dialog_requested.emit)
        debug_menu.addAction(action_devices)
        action_outlook = QAction("Diagnose &Outlook...", self)
        action_outlook.triggered.connect(self.open_outlook_diagnostic_requested.emit)
        debug_menu.addAction(action_outlook)
        action_log = QAction("View &Log...", self)
        action_log.triggered.connect(self.open_log_viewer_requested.emit)
        debug_menu.addAction(action_log)
        action_depcheck = QAction("&Check Dependencies...", self)
        action_depcheck.triggered.connect(self.open_dependency_check_requested.emit)
        debug_menu.addAction(action_depcheck)
        help_menu.addSeparator()
        action_user_guide = QAction("&User Guide...", self)
        action_user_guide.triggered.connect(self.open_user_guide_requested.emit)
        help_menu.addAction(action_user_guide)
        help_menu.addSeparator()
        action_check_updates = QAction("Check for &Updates...", self)
        action_check_updates.triggered.connect(self.check_for_updates_requested.emit)
        help_menu.addAction(action_check_updates)
        action_upgrade = QAction("&Upgrade...", self)
        action_upgrade.triggered.connect(self.upgrade_requested.emit)
        help_menu.addAction(action_upgrade)
        help_menu.addSeparator()
        action_about = QAction("&About Meeting Notetaker...", self)
        action_about.triggered.connect(self.open_about_requested.emit)
        help_menu.addAction(action_about)

        # Body: splitter
        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Orientation.Horizontal, central)
        layout.addWidget(splitter)

        # Left pane: session list + buttons
        left = QWidget(splitter)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 8, 8)
        header_row = QHBoxLayout()
        header_row.addWidget(QLabel("Sessions", left))
        header_row.addStretch(1)
        self._new_btn = QPushButton("+ New", left)
        self._new_btn.clicked.connect(self.new_session_requested.emit)
        header_row.addWidget(self._new_btn)
        left_layout.addLayout(header_row)
        self._list = QTreeWidget(left)
        self._list.setColumnCount(5)
        self._list.setHeaderHidden(True)
        self._list.setRootIsDecorated(False)
        self._list.setUniformRowHeights(True)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._list.itemSelectionChanged.connect(self._on_selection_changed)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._show_list_menu)
        self._list.itemDoubleClicked.connect(self._on_item_double_clicked)
        rename_shortcut = QShortcut(QKeySequence(Qt.Key.Key_F2), self._list)
        rename_shortcut.activated.connect(self._rename_selected)
        header = self._list.header()
        # Audio + slides + state are narrow glyph columns; date is
        # fixed-width to fit "YYYY-MM-DD HH:MM"; title takes the
        # remaining space.
        header.setSectionResizeMode(_COL_AUDIO, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(_COL_SLIDES, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(_COL_STATE, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(_COL_DATE, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(_COL_TITLE, QHeaderView.ResizeMode.Stretch)
        self._list.setColumnWidth(_COL_AUDIO, 28)
        self._list.setColumnWidth(_COL_SLIDES, 28)
        self._list.setColumnWidth(_COL_STATE, 28)
        self._list.setColumnWidth(_COL_DATE, 110)
        left_layout.addWidget(self._list, 1)
        bulk_row = QHBoxLayout()
        bulk_row.addStretch(1)
        self._delete_btn = QPushButton("Delete Selected", left)
        self._delete_btn.clicked.connect(self._delete_selected)
        bulk_row.addWidget(self._delete_btn)
        left_layout.addLayout(bulk_row)
        splitter.addWidget(left)

        # Right pane: SessionView
        self.session_view = SessionView(splitter)
        splitter.addWidget(self.session_view)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([340, 660])

        self.setStatusBar(QStatusBar(self))
        self.statusBar().showMessage("Ready")

        # Right-side indicators: a row of StatusSegment widgets (each is
        # a painted colored dot + short label + optional payload) plus a
        # plain "v0.6.4" version label on the left of the row. Painted
        # dots dodge the Windows emoji-font sizing inconsistency that an
        # earlier draft using unicode bullets had to work around.
        self._status_segments_widget = QWidget(self)
        seg_layout = QHBoxLayout(self._status_segments_widget)
        seg_layout.setContentsMargins(8, 0, 8, 0)
        seg_layout.setSpacing(14)
        self._version_label = QLabel("", self._status_segments_widget)
        seg_layout.addWidget(self._version_label)
        self._status_segments: dict[str, StatusSegment] = {}
        for key in _STATUS_SEGMENT_KEYS:
            seg = StatusSegment(self._status_segments_widget)
            seg.hide()  # Hidden until set_status_indicators makes it visible.
            seg_layout.addWidget(seg)
            self._status_segments[key] = seg
        self.statusBar().addPermanentWidget(self._status_segments_widget)

    def set_status_indicators(
        self,
        *,
        version: str = "",
        indicators: Optional[dict[str, SegmentState]] = None,
    ) -> None:
        """Update the right-side status-bar pills.

        `indicators` is a dict keyed by segment id (mic, sys, cal, spk,
        voice, det, syn). Missing keys are treated as visible=False so
        callers only have to pass entries for segments that should
        render this update. The version label is always shown on the
        left of the row.
        """
        if version:
            self._version_label.setText(f"v{version}")
            self._version_label.setToolTip(f"Running version: v{version}")
            self._version_label.show()
        else:
            self._version_label.hide()
        indicators = indicators or {}
        for key, segment in self._status_segments.items():
            state = indicators.get(key)
            if state is None:
                segment.hide()
            else:
                segment.apply(state)

    def set_sessions(self, sessions: Iterable[Session]) -> None:
        self._list.blockSignals(True)
        self._list.clear()
        for s in sessions:
            self._add_item(s)
        self._list.blockSignals(False)

    def select_session(self, session_id: str) -> None:
        root = self._list.invisibleRootItem()
        for i in range(root.childCount()):
            item = root.child(i)
            if item.data(_COL_TITLE, Qt.ItemDataRole.UserRole) == session_id:
                self._list.setCurrentItem(item)
                return

    def selected_session_ids(self) -> list[str]:
        return [
            item.data(_COL_TITLE, Qt.ItemDataRole.UserRole)
            for item in self._list.selectedItems()
        ]

    def status(self, message: str, *, timeout_ms: int = 0) -> None:
        if timeout_ms:
            self.statusBar().showMessage(message, timeout_ms)
        else:
            self.statusBar().showMessage(message)

    def _add_item(self, s: Session) -> None:
        audio_glyph = "🔊" if s.has_audio else ""
        audio_tooltip = (
            "Audio status: recording kept on disk"
            if s.has_audio
            else "Audio status: not retained (recording deleted after refinement)"
        )
        # Camera glyph if any screenshots are on disk for this session.
        # has_retained_audio mirrors this pattern for audio; we use the
        # same path helper here.
        from ..utils.paths import list_screenshots  # noqa: PLC0415
        has_slides = bool(list_screenshots(s.id))
        slides_glyph = "📷" if has_slides else ""
        slides_tooltip = (
            "Screenshots saved on disk for this session"
            if has_slides
            else "No screenshots captured for this session"
        )
        state_glyph, state_tooltip = _STATE_BADGE.get(s.state, ("", s.state))
        state_tooltip = f"Transcription state: {state_tooltip}"

        when, title = _session_date_and_title(s)
        item = QTreeWidgetItem([
            audio_glyph,
            slides_glyph,
            state_glyph,
            when,
            title,
        ])
        item.setTextAlignment(_COL_AUDIO, Qt.AlignmentFlag.AlignCenter)
        item.setTextAlignment(_COL_SLIDES, Qt.AlignmentFlag.AlignCenter)
        item.setTextAlignment(_COL_STATE, Qt.AlignmentFlag.AlignCenter)
        item.setToolTip(_COL_AUDIO, audio_tooltip)
        item.setToolTip(_COL_SLIDES, slides_tooltip)
        item.setToolTip(_COL_STATE, state_tooltip)
        item.setData(_COL_TITLE, Qt.ItemDataRole.UserRole, s.id)
        # Stash the full ISO created_at so Edit Timestamp can seed the
        # dialog without losing sub-minute precision (the visible "YYYY-MM-DD
        # HH:MM" column drops seconds + timezone).
        item.setData(_COL_DATE, Qt.ItemDataRole.UserRole, s.created_at)
        self._list.addTopLevelItem(item)

    def _on_selection_changed(self) -> None:
        selected = self._list.selectedItems()
        if len(selected) == 1:
            self.session_selected.emit(
                selected[0].data(_COL_TITLE, Qt.ItemDataRole.UserRole)
            )

    def _show_list_menu(self, pos) -> None:
        selected = self._list.selectedItems()
        if not selected:
            return
        menu = QMenu(self._list)
        action_rename = menu.addAction("Rename...")
        # Rename targets exactly one session -- multi-rename would be
        # an awkward UX (whose title applies?). Disable when the user
        # has multi-selected.
        action_rename.setEnabled(len(selected) == 1)
        action_edit_timestamp = menu.addAction("Edit timestamp...")
        action_edit_timestamp.setEnabled(len(selected) == 1)
        # Recording actions only make sense on a single session that
        # actually has audio retained on disk -- check the disk rather
        # than the session's has_audio flag since the flag could lag
        # behind a manual delete.
        single_id = selected[0].data(_COL_TITLE, Qt.ItemDataRole.UserRole) \
            if len(selected) == 1 else None
        has_audio = bool(single_id) and has_retained_audio(single_id)
        action_open_recording = menu.addAction("Open recording in media player")
        action_open_recording.setEnabled(has_audio)
        action_export_recording = menu.addAction("Export recording as...")
        action_export_recording.setEnabled(has_audio)
        action_delete_recording = menu.addAction("Delete recording...")
        action_delete_recording.setEnabled(has_audio)
        menu.addSeparator()
        action_delete = menu.addAction("Delete...")
        action = menu.exec(self._list.viewport().mapToGlobal(pos))
        if action is action_rename:
            self._rename_selected()
        elif action is action_edit_timestamp:
            self._edit_timestamp_selected()
        elif action is action_open_recording and single_id:
            self.open_recording_requested.emit(single_id)
        elif action is action_export_recording and single_id:
            self.export_recording_requested.emit(single_id)
        elif action is action_delete_recording and single_id:
            self._confirm_delete_recording(single_id)
        elif action is action_delete:
            self._delete_selected()

    def _confirm_delete_recording(self, session_id: str) -> None:
        confirm = QMessageBox.question(
            self,
            "Delete recording",
            "Delete the saved audio for this session?\n\n"
            "The transcript and notes are kept. Only the recording "
            "(mic + system audio files) is removed. This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm == QMessageBox.StandardButton.Yes:
            self.delete_recording_requested.emit(session_id)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        # Treat any double-click as "rename this session". Other columns
        # (audio/state glyphs, date) double-click into rename too -- the
        # whole row is one logical thing.
        self._rename_selected()

    def _rename_selected(self) -> None:
        ids = self.selected_session_ids()
        if len(ids) != 1:
            return
        session_id = ids[0]
        item = self._list.currentItem()
        if item is None:
            return
        current_title = item.text(_COL_TITLE)
        new_title, ok = QInputDialog.getText(
            self,
            "Rename Session",
            "Title:",
            QLineEdit.EchoMode.Normal,
            current_title,
        )
        if not ok:
            return
        new_title = new_title.strip()
        if not new_title:
            QMessageBox.warning(
                self,
                "Rename Session",
                "Title cannot be empty.",
            )
            return
        if new_title == current_title:
            return
        self.rename_session_requested.emit(session_id, new_title)

    def _edit_timestamp_selected(self) -> None:
        ids = self.selected_session_ids()
        if len(ids) != 1:
            return
        session_id = ids[0]
        item = self._list.currentItem()
        if item is None:
            return
        stored_iso = item.data(_COL_DATE, Qt.ItemDataRole.UserRole) or ""
        current_local = _parse_iso_to_local(stored_iso) or datetime.now()
        dialog = _EditTimestampDialog(initial=current_local, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        new_iso = dialog.result_utc_iso()
        if not new_iso or new_iso == stored_iso:
            return
        self.edit_session_timestamp_requested.emit(session_id, new_iso)

    def _delete_selected(self) -> None:
        ids = self.selected_session_ids()
        if not ids:
            return
        confirm = QMessageBox.question(
            self,
            "Delete Sessions",
            f"Delete {len(ids)} session(s)? This removes audio, transcripts, and notes from disk.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm == QMessageBox.StandardButton.Yes:
            self.delete_sessions_requested.emit(ids)


def _session_date_and_title(s: Session) -> tuple[str, str]:
    """Return ('YYYY-MM-DD HH:MM' in local time, title) for the list's two
    main columns. Timestamps are stored as UTC ISO; the list shows them in
    the user's local timezone so the column matches what they'd see in any
    other calendar UI."""
    try:
        when = (
            datetime.fromisoformat(s.created_at.replace("Z", "+00:00"))
            .astimezone()
            .strftime("%Y-%m-%d %H:%M")
        )
    except ValueError:
        when = s.created_at
    return when, s.title


def _parse_iso_to_local(iso_str: str) -> Optional[datetime]:
    """Parse a stored UTC ISO timestamp back into a local naive datetime
    suitable for seeding QDateTimeEdit. Returns None on parse failure."""
    if not iso_str:
        return None
    try:
        utc_aware = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError:
        return None
    return utc_aware.astimezone().replace(tzinfo=None)


class _EditTimestampDialog(QDialog):
    """Tiny dialog wrapping a QDateTimeEdit. Edits the session's local
    timestamp; result_utc_iso() returns the UTC ISO form the store wants."""

    def __init__(self, *, initial: datetime, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Session Timestamp")
        self.setModal(True)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Session date and time (your local timezone):", self
        ))
        self._editor = QDateTimeEdit(self)
        self._editor.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self._editor.setCalendarPopup(True)
        self._editor.setDateTime(QDateTime(
            initial.year, initial.month, initial.day,
            initial.hour, initial.minute, initial.second,
        ))
        layout.addWidget(self._editor)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def result_utc_iso(self) -> str:
        qdt = self._editor.dateTime().toPyDateTime()  # naive local
        local_tz = datetime.now().astimezone().tzinfo
        aware_local = qdt.replace(tzinfo=local_tz)
        return aware_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
