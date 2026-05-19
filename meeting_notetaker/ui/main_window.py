"""Main window -- session list (left) + SessionView (right)."""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Iterable, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QAction, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView,
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
from .session_view import SessionView


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
_COL_STATE = 1
_COL_DATE = 2
_COL_TITLE = 3


class MainWindow(QMainWindow):
    new_session_requested = pyqtSignal()
    open_settings_requested = pyqtSignal()
    open_devices_dialog_requested = pyqtSignal()
    open_outlook_diagnostic_requested = pyqtSignal()
    open_log_viewer_requested = pyqtSignal()
    check_for_updates_requested = pyqtSignal()
    upgrade_requested = pyqtSignal()
    quit_requested = pyqtSignal()
    delete_sessions_requested = pyqtSignal(list)   # list of session_ids
    rename_session_requested = pyqtSignal(str, str)  # session_id, new_title
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
        action_devices = QAction("&Audio Devices...", self)
        action_devices.triggered.connect(self.open_devices_dialog_requested.emit)
        help_menu.addAction(action_devices)
        action_outlook = QAction("Diagnose &Outlook...", self)
        action_outlook.triggered.connect(self.open_outlook_diagnostic_requested.emit)
        help_menu.addAction(action_outlook)
        action_log = QAction("View &Log...", self)
        action_log.triggered.connect(self.open_log_viewer_requested.emit)
        help_menu.addAction(action_log)
        help_menu.addSeparator()
        action_check_updates = QAction("Check for &Updates...", self)
        action_check_updates.triggered.connect(self.check_for_updates_requested.emit)
        help_menu.addAction(action_check_updates)
        action_upgrade = QAction("&Upgrade...", self)
        action_upgrade.triggered.connect(self.upgrade_requested.emit)
        help_menu.addAction(action_upgrade)

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
        self._list.setColumnCount(4)
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
        # Audio + state are narrow glyph columns; date is fixed-width
        # to fit "YYYY-MM-DD HH:MM"; title takes the remaining space.
        header.setSectionResizeMode(_COL_AUDIO, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(_COL_STATE, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(_COL_DATE, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(_COL_TITLE, QHeaderView.ResizeMode.Stretch)
        self._list.setColumnWidth(_COL_AUDIO, 28)
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

        # Single right-aligned permanent indicator that joins all sub-items
        # with " | " explicitly. Using one QLabel rather than multiple
        # addPermanentWidget() calls avoids Qt's Windows-native style
        # painting a separator line *after* the last permanent widget
        # (which read as a trailing pipe).
        self._indicators_label = QLabel("", self)
        self._indicators_label.setContentsMargins(8, 0, 8, 0)
        self.statusBar().addPermanentWidget(self._indicators_label)

    def set_status_indicators(
        self,
        *,
        version: str = "",
        mic_label: str,
        mic_tooltip: str = "",
        loopback_label: str,
        loopback_tooltip: str = "",
        calendar_label: str,
        calendar_tooltip: str = "",
        speakers_label: str = "",
        speakers_tooltip: str = "",
        voice_label: str = "",
        voice_tooltip: str = "",
        detect_label: str = "",
        detect_tooltip: str = "",
    ) -> None:
        """Update the bottom status bar's right-side indicator string.

        Sub-items are joined with " | "; we explicitly avoid trailing
        the string with a separator. The full per-sub-item tooltip text
        is joined into a single multi-line tooltip so the user can still
        hover the indicator to see the long form (the QLabel doesn't
        expose per-character tooltips).

        `speakers_label`, `voice_label`, and `detect_label` only render
        when non-empty -- clean installs and not-applicable states leave
        them out.
        """
        parts: list[str] = []
        tooltip_parts: list[str] = []
        if version:
            parts.append(f"v{version}")
            tooltip_parts.append(f"Running version: v{version}")
        parts.append(mic_label)
        tooltip_parts.append(mic_tooltip or mic_label)
        parts.append(loopback_label)
        tooltip_parts.append(loopback_tooltip or loopback_label)
        parts.append(calendar_label)
        tooltip_parts.append(calendar_tooltip or calendar_label)
        if speakers_label:
            parts.append(speakers_label)
            tooltip_parts.append(speakers_tooltip or speakers_label)
        if voice_label:
            parts.append(voice_label)
            tooltip_parts.append(voice_tooltip or voice_label)
        if detect_label:
            parts.append(detect_label)
            tooltip_parts.append(detect_tooltip or detect_label)
        self._indicators_label.setText(" | ".join(parts))
        self._indicators_label.setToolTip("\n".join(tooltip_parts))

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
        state_glyph, state_tooltip = _STATE_BADGE.get(s.state, ("", s.state))
        state_tooltip = f"Transcription state: {state_tooltip}"

        when, title = _session_date_and_title(s)
        item = QTreeWidgetItem([
            audio_glyph,
            state_glyph,
            when,
            title,
        ])
        item.setTextAlignment(_COL_AUDIO, Qt.AlignmentFlag.AlignCenter)
        item.setTextAlignment(_COL_STATE, Qt.AlignmentFlag.AlignCenter)
        item.setToolTip(_COL_AUDIO, audio_tooltip)
        item.setToolTip(_COL_STATE, state_tooltip)
        item.setData(_COL_TITLE, Qt.ItemDataRole.UserRole, s.id)
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
        action_delete = menu.addAction("Delete...")
        action = menu.exec(self._list.viewport().mapToGlobal(pos))
        if action is action_rename:
            self._rename_selected()
        elif action is action_delete:
            self._delete_selected()

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
    """Return ('YYYY-MM-DD HH:MM', title) for the list's two main columns."""
    try:
        when = datetime.fromisoformat(s.created_at.replace("Z", "+00:00")).strftime(
            "%Y-%m-%d %H:%M"
        )
    except ValueError:
        when = s.created_at
    return when, s.title
