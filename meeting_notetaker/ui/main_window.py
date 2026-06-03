"""Main window -- session list (left) + SessionView (right)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

from PyQt6.QtCore import QDateTime, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QCloseEvent, QKeySequence, QShortcut
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
from .classification_navigator import ClassificationNavigator
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

# Session list column order: human-relevant columns (Date + Title)
# lead; narrow indicator glyphs trail. Sortable columns are Date
# and Title only -- clicking any indicator column snaps back to
# the active sort. Indicator order matches the chronology of the
# session lifecycle: audio captured -> screen captures taken ->
# attachments added -> processing state.
_COL_DATE = 0
_COL_TITLE = 1
_COL_AUDIO = 2
_COL_SLIDES = 3
_COL_ATTACHMENTS = 4
_COL_STATE = 5

_INDICATOR_COLUMNS = (
    _COL_AUDIO, _COL_SLIDES, _COL_ATTACHMENTS, _COL_STATE,
)


# Sort-spec serialization. Stored verbatim in config.toml under
# ui.session_list_sort; values that don't match snap to "date_desc".
_DEFAULT_SORT_SPEC = "date_desc"


def _sort_spec_to_column_order(spec: str) -> tuple[int, Qt.SortOrder]:
    """Translate a persisted sort spec string into (column, order).

    Unknown specs fall back to the default (date descending).
    """
    table: dict[str, tuple[int, Qt.SortOrder]] = {
        "date_desc":  (_COL_DATE,  Qt.SortOrder.DescendingOrder),
        "date_asc":   (_COL_DATE,  Qt.SortOrder.AscendingOrder),
        "title_asc":  (_COL_TITLE, Qt.SortOrder.AscendingOrder),
        "title_desc": (_COL_TITLE, Qt.SortOrder.DescendingOrder),
    }
    return table.get(spec, table[_DEFAULT_SORT_SPEC])


def _column_order_to_sort_spec(column: int, order: Qt.SortOrder) -> str:
    """Inverse of _sort_spec_to_column_order. Indicator columns return
    the default spec (callers use this to snap a click on an indicator
    column back to a real sort)."""
    if column == _COL_DATE:
        return "date_asc" if order == Qt.SortOrder.AscendingOrder else "date_desc"
    if column == _COL_TITLE:
        return "title_asc" if order == Qt.SortOrder.AscendingOrder else "title_desc"
    return _DEFAULT_SORT_SPEC


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
    export_video_requested = pyqtSignal(str)       # session_id
    export_package_requested = pyqtSignal(str)     # session_id (issue #30)
    delete_recording_requested = pyqtSignal(str)   # session_id
    session_selected = pyqtSignal(str)             # session_id
    session_list_sort_changed = pyqtSignal(str)    # one of VALID_SESSION_LIST_SORTS
    manage_series_requested = pyqtSignal()
    manage_classification_requested = pyqtSignal()
    address_book_requested = pyqtSignal()
    classification_filter_changed = pyqtSignal(str, object)
    # view (str -- one of VIEW_*), value_id (Optional[int]); emitted by
    # the navigator when the user picks a different filter.
    open_search_requested = pyqtSignal()           # Ctrl+Shift+F
    rebuild_search_index_requested = pyqtSignal()  # Help > Debug
    # Tools menu (#67): manual + restore entry points for the backup
    # feature. Settings > Backups still owns folder + schedule +
    # retention; the menu is the one-click action surface.
    backup_now_requested = pyqtSignal()
    restore_backup_requested = pyqtSignal()
    show_session_tab_requested = pyqtSignal(str, str, object)
    # session_id, tab_id ('transcript' | 'live_notes' | 'notes' | 'previous'),
    # optional archive_path (str | None); emitted by the cross-session
    # search dialog after the user double-clicks a hit.

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Meeting Notetaker")
        self.setWindowIcon(app_icon())
        self.resize(1024, 720)
        # Optional close handler. MainApp installs one for the
        # backup-in-progress wait flow (#67); when set, closeEvent
        # gives it first refusal over the event before super().
        self._close_handler: Optional[Callable[[QCloseEvent], None]] = None

        # Global shortcut: Ctrl+Shift+F opens the cross-session search
        # dialog. Window-scoped so it fires no matter which pane has
        # focus (session list, session view, or the find bar itself).
        search_shortcut = QShortcut(
            QKeySequence("Ctrl+Shift+F"), self,
        )
        search_shortcut.activated.connect(self.open_search_requested.emit)

        # Menu bar
        menubar = self.menuBar()
        file_menu = menubar.addMenu("&File")
        action_new = QAction("&New Session...", self)
        action_new.setShortcut("Ctrl+N")
        action_new.triggered.connect(self.new_session_requested.emit)
        file_menu.addAction(action_new)
        file_menu.addSeparator()
        # Per-session actions mirroring the right-click context menu so
        # the menu bar is also a friendly entry point (Aaron's 2026-06-02
        # menu-reorg ask). Enabled state tracks the session-list
        # selection via _refresh_session_actions, fired from
        # _on_selection_changed below.
        self._action_rename_session = QAction("&Rename Session...", self)
        self._action_rename_session.triggered.connect(self._rename_selected)
        file_menu.addAction(self._action_rename_session)
        self._action_edit_timestamp = QAction("Edit &Timestamp...", self)
        self._action_edit_timestamp.triggered.connect(
            self._edit_timestamp_selected,
        )
        file_menu.addAction(self._action_edit_timestamp)
        # Export submenu mirrors the right-click Export-* entries so a
        # mouse-averse user has parity from the menu bar.
        export_menu = file_menu.addMenu("&Export")
        self._action_export_recording = QAction("Recording As...", self)
        self._action_export_recording.triggered.connect(
            self._emit_export_recording,
        )
        export_menu.addAction(self._action_export_recording)
        self._action_export_video = QAction("Session as Video...", self)
        self._action_export_video.triggered.connect(self._emit_export_video)
        export_menu.addAction(self._action_export_video)
        self._action_export_package = QAction("&Full Session...", self)
        self._action_export_package.triggered.connect(self._emit_export_package)
        export_menu.addAction(self._action_export_package)
        self._file_export_menu = export_menu
        # Delete submenu: recording-only vs the whole session, matching
        # the right-click split so users don't fat-finger the wrong one.
        delete_menu = file_menu.addMenu("&Delete")
        self._action_delete_recording = QAction("Recording...", self)
        self._action_delete_recording.triggered.connect(
            self._emit_delete_recording,
        )
        delete_menu.addAction(self._action_delete_recording)
        self._action_delete_session = QAction("&Session...", self)
        self._action_delete_session.triggered.connect(self._delete_selected)
        delete_menu.addAction(self._action_delete_session)
        self._file_delete_menu = delete_menu
        file_menu.addSeparator()
        action_quit = QAction("&Quit", self)
        action_quit.setShortcut("Ctrl+Q")
        action_quit.triggered.connect(self.quit_requested.emit)
        file_menu.addAction(action_quit)

        # Tools menu (#67): manual backup + restore + the catalog
        # editors + Settings. Settings + Manage Classification +
        # Address Book moved here from File on 2026-06-02 because
        # they're configuration / catalog actions, not file actions.
        tools_menu = menubar.addMenu("&Tools")
        action_backup_now = QAction("&Backup Now...", self)
        action_backup_now.triggered.connect(self.backup_now_requested.emit)
        tools_menu.addAction(action_backup_now)
        action_restore = QAction("&Restore from Backup...", self)
        action_restore.triggered.connect(self.restore_backup_requested.emit)
        tools_menu.addAction(action_restore)
        tools_menu.addSeparator()
        # Manage Classification covers Series + Topics (tabbed);
        # Address Book covers Contacts (formerly People) -- separated
        # because Contacts also link to the Speaker store and have
        # alias / merge-suggestion surfaces the simpler Series/Topics
        # tabs don't need.
        action_manage_classification = QAction(
            "&Manage Classification...", self,
        )
        action_manage_classification.triggered.connect(
            self.manage_classification_requested.emit,
        )
        tools_menu.addAction(action_manage_classification)
        action_address_book = QAction("&Address Book...", self)
        action_address_book.triggered.connect(
            self.address_book_requested.emit,
        )
        tools_menu.addAction(action_address_book)
        tools_menu.addSeparator()
        action_settings = QAction("&Settings...", self)
        action_settings.setShortcut("Ctrl+,")
        action_settings.triggered.connect(self.open_settings_requested.emit)
        tools_menu.addAction(action_settings)
        # Per-session File actions all start disabled; the
        # itemSelectionChanged slot calls _refresh_session_actions to
        # toggle them as the user picks rows. Setting initial state
        # here so _refresh_session_actions doesn't have to be safe to
        # call before _list exists.
        for action in (
            self._action_rename_session,
            self._action_edit_timestamp,
            self._action_export_recording,
            self._action_export_video,
            self._action_export_package,
            self._action_delete_recording,
            self._action_delete_session,
        ):
            action.setEnabled(False)
        self._file_export_menu.setEnabled(False)
        self._file_delete_menu.setEnabled(False)

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
        # Rebuild Search Index: nuclear option for the FTS5 store
        # under app_data/search.db. Useful after a corrupt write or
        # a schema bump. Runs synchronously with a progress dialog.
        action_rebuild_search = QAction("&Rebuild Search Index", self)
        action_rebuild_search.triggered.connect(
            self.rebuild_search_index_requested.emit
        )
        debug_menu.addAction(action_rebuild_search)
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
        # Hold a reference so save_layout_state can serialize the
        # split ratio; the local `splitter` name is shadowed below.
        self._main_splitter = splitter
        layout.addWidget(splitter)

        # Left pane: session list + buttons
        left = QWidget(splitter)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 8, 8)
        header_row = QHBoxLayout()
        header_row.addWidget(QLabel("Sessions", left))
        header_row.addStretch(1)
        # Search button mirrors the Ctrl+Shift+F shortcut so users
        # who reach for the mouse first have a visible affordance.
        self._search_btn = QPushButton("Search", left)
        self._search_btn.setToolTip(
            "Search across all sessions (Ctrl+Shift+F)"
        )
        self._search_btn.clicked.connect(self.open_search_requested.emit)
        header_row.addWidget(self._search_btn)
        self._new_btn = QPushButton("+ New", left)
        self._new_btn.clicked.connect(self.new_session_requested.emit)
        header_row.addWidget(self._new_btn)
        left_layout.addLayout(header_row)
        # Classification navigator (v0.7.0+): All / By Series / By
        # Person / By Topic filter pulldown. Emits filter_changed
        # for MainApp to route into a filtered session list.
        self._navigator = ClassificationNavigator(left)
        left_layout.addWidget(self._navigator)
        self._list = QTreeWidget(left)
        self._list.setColumnCount(6)
        # Header is visible so Date + Title can be clicked to sort.
        # Indicator columns (Audio / Slides / Attachments / State)
        # carry no header text -- their headers stay blank but are
        # still focusable so the column boundary can be dragged.
        self._list.setHeaderLabels(["Date", "Title", "", "", "", ""])
        self._list.setHeaderHidden(False)
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
        # Date is fixed-width to fit "YYYY-MM-DD HH:MM" + the sort
        # arrow; title takes the remaining horizontal space; audio +
        # slides + state are narrow glyph columns trailing on the
        # right. Critically, stretchLastSection has to be disabled --
        # Qt defaults it to True, which would override the State
        # column's Fixed mode and let it expand to fill the window
        # (the regression Aaron called out post-PR-#27 since the
        # header was previously hidden and the default was moot).
        header.setStretchLastSection(False)
        # Qt's default minimum section size on a sortable header is
        # 38 px to reserve room for the sort-indicator triangle. We
        # disable sortIndicator on indicator columns via the snap-
        # back handler, so the triangle never paints there -- but
        # the minimum still applies unless we lower it explicitly.
        header.setMinimumSectionSize(20)
        header.setSectionResizeMode(_COL_DATE, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(_COL_TITLE, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(_COL_AUDIO, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(_COL_SLIDES, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(_COL_ATTACHMENTS, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(_COL_STATE, QHeaderView.ResizeMode.Fixed)
        self._list.setColumnWidth(_COL_DATE, 150)
        self._list.setColumnWidth(_COL_AUDIO, 28)
        self._list.setColumnWidth(_COL_SLIDES, 28)
        self._list.setColumnWidth(_COL_ATTACHMENTS, 28)
        self._list.setColumnWidth(_COL_STATE, 28)
        # Indicator-column headers are blank text but still get
        # informative tooltips so users learn the glyph meaning by
        # hovering. Same content the per-item tooltips carry.
        header.model().setHeaderData(
            _COL_AUDIO, Qt.Orientation.Horizontal,
            "Audio retained on disk", Qt.ItemDataRole.ToolTipRole,
        )
        header.model().setHeaderData(
            _COL_SLIDES, Qt.Orientation.Horizontal,
            "Screenshots captured for this session", Qt.ItemDataRole.ToolTipRole,
        )
        header.model().setHeaderData(
            _COL_ATTACHMENTS, Qt.Orientation.Horizontal,
            "Attachments stored with this session", Qt.ItemDataRole.ToolTipRole,
        )
        header.model().setHeaderData(
            _COL_STATE, Qt.Orientation.Horizontal,
            "Transcription pipeline state", Qt.ItemDataRole.ToolTipRole,
        )
        # Enable sortable headers. Date sort is the safe default
        # (YYYY-MM-DD HH:MM lex order = chronological); Title sort
        # is A->Z then Z->A on second click. Clicks on indicator
        # columns are snapped back via _on_sort_indicator_changed.
        self._list.setSortingEnabled(True)
        header.setSectionsClickable(True)
        self._current_sort_spec = _DEFAULT_SORT_SPEC
        self._suppress_sort_emission = False
        header.sortIndicatorChanged.connect(self._on_sort_indicator_changed)
        # Apply the default before any items are loaded so the first
        # set_sessions call respects it; set_session_list_sort()
        # called from MainApp at startup overrides with the persisted
        # value.
        col, order = _sort_spec_to_column_order(self._current_sort_spec)
        self._list.sortByColumn(col, order)
        left_layout.addWidget(self._list, 1)
        self._navigator.filter_changed.connect(self.classification_filter_changed.emit)
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
        # Disabling sorting during bulk insert is the documented Qt idiom
        # for avoiding O(N log N) re-sort per row; we re-enable + sort
        # once at the end. Also blocking selection-changed signal so a
        # mass-replace doesn't fire the per-row selection handler.
        self._list.blockSignals(True)
        was_sorting = self._list.isSortingEnabled()
        self._list.setSortingEnabled(False)
        self._list.clear()
        for s in sessions:
            self._add_item(s)
        if was_sorting:
            self._list.setSortingEnabled(True)
            col, order = _sort_spec_to_column_order(self._current_sort_spec)
            self._list.sortByColumn(col, order)
        self._list.blockSignals(False)

    def set_classification_choices(
        self,
        *,
        series: list[tuple[int, str]] | None = None,
        people: list[tuple[int, str]] | None = None,
        topics: list[tuple[int, str]] | None = None,
    ) -> None:
        """Push fresh series / people / topics into the navigator combo.

        Called from MainApp after any classification mutation so the
        pulldown shows current state. Pass only the changed dimension
        to avoid unnecessary list rebuilds.
        """
        if series is not None:
            self._navigator.set_series(series)
        if people is not None:
            self._navigator.set_people(people)
        if topics is not None:
            self._navigator.set_topics(topics)

    def reset_classification_filter(self) -> None:
        """Snap the navigator back to View=All. Used when the underlying
        store is rebuilt or when MainApp wants a clean slate after a
        bulk import."""
        self._navigator.reset()

    def save_layout_state(self) -> tuple[str, str]:
        """Serialize window geometry + main-splitter state to base64.

        Returns (geometry_b64, splitter_b64). MainApp persists both
        to config.toml on aboutToQuit so launch->resize->relaunch
        round-trips the user's preferred window size + left/right
        pane ratio.

        Empty strings on either side mean "Qt couldn't serialize"
        and the restore path will fall back to defaults.
        """
        import base64  # noqa: PLC0415
        try:
            geom = base64.b64encode(bytes(self.saveGeometry())).decode("ascii")
        except Exception:
            geom = ""
        try:
            split = base64.b64encode(
                bytes(self._main_splitter.saveState()),
            ).decode("ascii")
        except Exception:
            split = ""
        return geom, split

    def restore_layout_state(
        self,
        geometry_b64: str,
        splitter_b64: str,
    ) -> None:
        """Apply persisted geometry + splitter state from config.

        Qt's restoreGeometry returns False when the stored rect is
        off-screen (e.g. a monitor was removed since the last
        save); we ignore the result and let Qt fall back to its
        platform-default position rather than risking an invisible
        window. Empty / malformed strings short-circuit to no-op.
        """
        import base64  # noqa: PLC0415
        from PyQt6.QtCore import QByteArray  # noqa: PLC0415
        if geometry_b64:
            try:
                self.restoreGeometry(
                    QByteArray(base64.b64decode(geometry_b64))
                )
            except Exception:
                pass
        if splitter_b64:
            try:
                self._main_splitter.restoreState(
                    QByteArray(base64.b64decode(splitter_b64))
                )
            except Exception:
                pass

    def set_session_list_sort(self, spec: str) -> None:
        """Apply a persisted sort spec to the list.

        Called once from MainApp at startup with the value loaded from
        config.toml. The header click handler keeps the spec + display
        in sync after that. Invalid specs fall through to the default
        via _sort_spec_to_column_order.
        """
        self._current_sort_spec = spec or _DEFAULT_SORT_SPEC
        col, order = _sort_spec_to_column_order(self._current_sort_spec)
        # Apply the sort without re-emitting back to MainApp; this is
        # the initial-state load, not a user action.
        self._suppress_sort_emission = True
        try:
            self._list.sortByColumn(col, order)
            self._list.header().setSortIndicator(col, order)
        finally:
            self._suppress_sort_emission = False

    def _on_sort_indicator_changed(self, column: int, order: Qt.SortOrder) -> None:
        """Handle a header click.

        If the column is one of the indicator columns (Audio / Slides
        / State), snap back to the previously-active sort -- those
        columns hold emoji glyphs and sorting by them is nonsensical.
        Otherwise persist the new spec back to MainApp.
        """
        if self._suppress_sort_emission:
            return
        if column in _INDICATOR_COLUMNS:
            # Snap back. Set the indicator + actually sort to the
            # previous (column, order) under signal-suppression so we
            # don't recurse into this handler.
            prev_col, prev_order = _sort_spec_to_column_order(self._current_sort_spec)
            self._suppress_sort_emission = True
            try:
                self._list.header().setSortIndicator(prev_col, prev_order)
                self._list.sortByColumn(prev_col, prev_order)
            finally:
                self._suppress_sort_emission = False
            return
        new_spec = _column_order_to_sort_spec(column, order)
        if new_spec == self._current_sort_spec:
            return
        self._current_sort_spec = new_spec
        self.session_list_sort_changed.emit(new_spec)

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
        from ..utils.paths import has_attachments, list_screenshots  # noqa: PLC0415
        has_slides = bool(list_screenshots(s.id))
        slides_glyph = "📷" if has_slides else ""
        slides_tooltip = (
            "Screenshots saved on disk for this session"
            if has_slides
            else "No screenshots captured for this session"
        )
        # Paperclip if the session has any attachments on disk.
        # Cheap iterdir check -- avoids a sidecar parse per row.
        has_attached = has_attachments(s.id)
        attachments_glyph = "📎" if has_attached else ""
        attachments_tooltip = (
            "Attachments stored with this session"
            if has_attached
            else "No attachments stored with this session"
        )
        state_glyph, state_tooltip = _STATE_BADGE.get(s.state, ("", s.state))
        state_tooltip = f"Transcription state: {state_tooltip}"

        when, title = _session_date_and_title(s)
        item = QTreeWidgetItem([
            when,
            title,
            audio_glyph,
            slides_glyph,
            attachments_glyph,
            state_glyph,
        ])
        item.setTextAlignment(_COL_AUDIO, Qt.AlignmentFlag.AlignCenter)
        item.setTextAlignment(_COL_SLIDES, Qt.AlignmentFlag.AlignCenter)
        item.setTextAlignment(_COL_ATTACHMENTS, Qt.AlignmentFlag.AlignCenter)
        item.setTextAlignment(_COL_STATE, Qt.AlignmentFlag.AlignCenter)
        item.setToolTip(_COL_AUDIO, audio_tooltip)
        item.setToolTip(_COL_SLIDES, slides_tooltip)
        item.setToolTip(_COL_ATTACHMENTS, attachments_tooltip)
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
        self._refresh_session_actions()

    def _refresh_session_actions(self) -> None:
        """Sync the File menu's per-session actions to the live
        selection. Mirrors the enable/disable matrix the right-click
        menu uses so the two surfaces stay consistent."""
        ids = self.selected_session_ids()
        single = ids[0] if len(ids) == 1 else None
        has_audio = bool(single) and has_retained_audio(single)
        any_selected = bool(ids)
        # Rename + edit-timestamp are single-session-only.
        self._action_rename_session.setEnabled(single is not None)
        self._action_edit_timestamp.setEnabled(single is not None)
        # Export actions need a single session with retained audio
        # (except Full Session which only needs a single session).
        self._action_export_recording.setEnabled(has_audio)
        self._action_export_video.setEnabled(has_audio)
        self._action_export_package.setEnabled(single is not None)
        self._file_export_menu.setEnabled(single is not None)
        # Delete actions: recording requires audio; session works on
        # any selection (single or multi).
        self._action_delete_recording.setEnabled(has_audio)
        self._action_delete_session.setEnabled(any_selected)
        self._file_delete_menu.setEnabled(any_selected)

    # ---- File-menu emit helpers ----------------------------------------

    def _emit_export_recording(self) -> None:
        ids = self.selected_session_ids()
        if len(ids) == 1:
            self.export_recording_requested.emit(ids[0])

    def _emit_export_video(self) -> None:
        ids = self.selected_session_ids()
        if len(ids) == 1:
            self.export_video_requested.emit(ids[0])

    def _emit_export_package(self) -> None:
        ids = self.selected_session_ids()
        if len(ids) == 1:
            self.export_package_requested.emit(ids[0])

    def _emit_delete_recording(self) -> None:
        ids = self.selected_session_ids()
        if len(ids) == 1:
            self._confirm_delete_recording(ids[0])

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
        action_export_video = menu.addAction("Export session as video...")
        action_export_video.setEnabled(has_audio)
        # Issue #30: full-session ZIP export. Always available
        # when a single session is selected -- the orchestrator
        # handles missing-audio / missing-screenshots gracefully.
        action_export_package = menu.addAction("Export full session...")
        action_export_package.setEnabled(bool(single_id))
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
        elif action is action_export_video and single_id:
            self.export_video_requested.emit(single_id)
        elif action is action_export_package and single_id:
            self.export_package_requested.emit(single_id)
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

    def set_close_handler(self, handler: Callable[[QCloseEvent], None]) -> None:
        """Install a callback that gets first refusal over a window
        close. The handler can call ``event.ignore()`` to defer the
        close (e.g. while a backup is finishing) -- the window stays
        open and the close path is suppressed."""
        self._close_handler = handler

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._close_handler is not None:
            self._close_handler(event)
            if not event.isAccepted():
                return
        super().closeEvent(event)


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
