"""Main window -- session list (left) + SessionView (right)."""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Iterable, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMenuBar,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from ..models.session import Session
from ..utils.icons import app_icon
from .session_view import SessionView


class MainWindow(QMainWindow):
    new_session_requested = pyqtSignal()
    open_settings_requested = pyqtSignal()
    open_devices_dialog_requested = pyqtSignal()
    quit_requested = pyqtSignal()
    delete_sessions_requested = pyqtSignal(list)   # list of session_ids
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
        self._list = QListWidget(left)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._list.itemSelectionChanged.connect(self._on_selection_changed)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._show_list_menu)
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
        splitter.setSizes([300, 700])

        self.setStatusBar(QStatusBar(self))
        self.statusBar().showMessage("Ready")

    def set_sessions(self, sessions: Iterable[Session]) -> None:
        self._list.blockSignals(True)
        self._list.clear()
        for s in sessions:
            self._add_item(s)
        self._list.blockSignals(False)

    def select_session(self, session_id: str) -> None:
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == session_id:
                self._list.setCurrentItem(item)
                return

    def selected_session_ids(self) -> list[str]:
        return [item.data(Qt.ItemDataRole.UserRole) for item in self._list.selectedItems()]

    def status(self, message: str, *, timeout_ms: int = 0) -> None:
        if timeout_ms:
            self.statusBar().showMessage(message, timeout_ms)
        else:
            self.statusBar().showMessage(message)

    def _add_item(self, s: Session) -> None:
        item = QListWidgetItem(_session_list_label(s))
        item.setData(Qt.ItemDataRole.UserRole, s.id)
        self._list.addItem(item)

    def _on_selection_changed(self) -> None:
        selected = self._list.selectedItems()
        if len(selected) == 1:
            self.session_selected.emit(selected[0].data(Qt.ItemDataRole.UserRole))

    def _show_list_menu(self, pos) -> None:
        if not self._list.selectedItems():
            return
        menu = QMenu(self._list)
        action_delete = menu.addAction("Delete...")
        action = menu.exec(self._list.viewport().mapToGlobal(pos))
        if action is action_delete:
            self._delete_selected()

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


def _session_list_label(s: Session) -> str:
    try:
        when = datetime.fromisoformat(s.created_at.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        when = s.created_at
    suffix = ""
    if s.state == "recording":
        suffix = "  *recording*"
    elif s.state == "paused":
        suffix = "  (paused)"
    elif s.state == "processing":
        suffix = "  (transcribing)"
    elif s.state == "error":
        suffix = "  (error)"
    return f"{when}  --  {s.title}{suffix}"
