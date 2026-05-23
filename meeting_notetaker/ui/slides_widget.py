"""Slides tab: thumbnail grid + full-image view of captured screenshots.

Two-mode QStackedWidget:

* **Grid mode** (default): QListWidget in IconMode, thumbnails sized
  160x100. Click a thumbnail to switch to full mode for that image;
  right-click for Copy / Delete / Open in default viewer.
* **Full mode**: scaled-to-fit QLabel + Prev / Next / Back controls.
  Right-click hands the same menu the grid does.

Loads its list via list_screenshots(session_id). Refreshes when the
SessionView's set_session() runs and when MainApp emits the
screenshot_added signal (after Capture / Insert).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QIcon, QImage, QPixmap
from .scaled_image_label import ScaledImageLabel
from .transcript_player_bar import TranscriptPlayerBar

from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)


_THUMB_W = 160
_THUMB_H = 100


class SlidesWidget(QWidget):
    """Thumbnail + full-view UI plus an audio player bar.

    The player bar at the bottom is the same TranscriptPlayerBar
    component the Transcript tab uses. MainApp drives both bars from
    a single AudioPlayer instance so playback state stays in sync
    across the two tabs.

    While playback is active, the full-view auto-advances to the
    screenshot whose recording-relative offset is the latest <=
    current playback position. Clicking a thumbnail (or its full-view)
    while audio is loaded seeks the player to that screenshot's
    timestamp -- the metaphor is "jump to the moment this slide was
    being shown".
    """

    delete_requested = pyqtSignal(Path)  # MainApp does the unlink
    play_clicked = pyqtSignal()
    pause_clicked = pyqtSignal()
    seek_ms_requested = pyqtSignal(int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._paths: list[Path] = []
        self._current_index: int = 0  # used while in full mode
        # (path, offset_ms) per screenshot. Empty until MainApp pushes
        # the offsets via set_screenshot_offsets. The player-driven
        # auto-advance bisects this list on each position tick.
        self._offsets: list[tuple[Path, int]] = []
        # Whether MainApp's AudioPlayer has audio loaded for this
        # session. Drives the click-thumbnail-seeks-audio behavior.
        self._audio_available = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._stack = QStackedWidget(self)

        # --- grid page -----------------------------------------------------
        grid_page = QWidget(self._stack)
        grid_layout = QVBoxLayout(grid_page)
        grid_layout.setContentsMargins(8, 8, 8, 8)
        grid_layout.setSpacing(6)
        self._grid_heading = QLabel("No screenshots yet for this session.", grid_page)
        self._grid_heading.setStyleSheet("color: gray;")
        grid_layout.addWidget(self._grid_heading)
        self._list = QListWidget(grid_page)
        self._list.setViewMode(QListWidget.ViewMode.IconMode)
        self._list.setIconSize(QSize(_THUMB_W, _THUMB_H))
        self._list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self._list.setSpacing(8)
        self._list.setMovement(QListWidget.Movement.Static)
        self._list.setUniformItemSizes(True)
        self._list.itemDoubleClicked.connect(self._on_thumb_double_clicked)
        # Single-click also opens; matches the rest of the app's
        # one-click navigation pattern.
        self._list.itemClicked.connect(self._on_thumb_clicked)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_thumb_context_menu)
        grid_layout.addWidget(self._list, 1)

        # --- full-view page ------------------------------------------------
        full_page = QWidget(self._stack)
        full_layout = QVBoxLayout(full_page)
        full_layout.setContentsMargins(8, 8, 8, 8)
        full_layout.setSpacing(6)

        nav_row = QHBoxLayout()
        self._back_btn = QPushButton("< Back to thumbnails", full_page)
        self._back_btn.clicked.connect(self._on_back_clicked)
        nav_row.addWidget(self._back_btn)
        nav_row.addStretch(1)
        self._prev_btn = QPushButton("Previous", full_page)
        self._prev_btn.clicked.connect(self._on_prev_clicked)
        nav_row.addWidget(self._prev_btn)
        self._next_btn = QPushButton("Next", full_page)
        self._next_btn.clicked.connect(self._on_next_clicked)
        nav_row.addWidget(self._next_btn)
        full_layout.addLayout(nav_row)

        self._caption = QLabel("", full_page)
        self._caption.setStyleSheet("color: gray; font-size: 11px;")
        full_layout.addWidget(self._caption)

        self._full_view = ScaledImageLabel(full_page)
        self._full_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._full_view.customContextMenuRequested.connect(
            self._on_full_context_menu
        )
        full_layout.addWidget(self._full_view, 1)

        self._stack.addWidget(grid_page)
        self._stack.addWidget(full_page)
        outer.addWidget(self._stack)

        # Player bar at the bottom. Disabled by default; MainApp
        # flips it on via set_player_enabled when retained audio is
        # present.
        self._player_bar = TranscriptPlayerBar(self)
        self._player_bar.play_clicked.connect(self.play_clicked.emit)
        self._player_bar.pause_clicked.connect(self.pause_clicked.emit)
        self._player_bar.seek_ms_requested.connect(self.seek_ms_requested.emit)
        outer.addWidget(self._player_bar)

    # ------------------------------------------------------------------
    # Public API

    def set_screenshot_offsets(self, offsets: list[tuple[Path, int]]) -> None:
        """Pin (path, recording_offset_ms) for each screenshot.

        Used to drive the position-tick auto-advance (latest screenshot
        with offset <= position) and the click-thumbnail-to-seek
        action.
        """
        self._offsets = list(offsets)

    def set_player_enabled(self, enabled: bool) -> None:
        self._player_bar.set_enabled_state(enabled)
        self._audio_available = bool(enabled)

    def set_player_total_ms(self, total_ms: int) -> None:
        self._player_bar.set_total_ms(total_ms)

    def set_player_position_ms(self, ms: int) -> None:
        self._player_bar.set_position_ms(ms)
        # Auto-advance the displayed thumbnail / full-view to track
        # the playback position. Only kicks in when there are offsets
        # to consult; without them the slides tab is a passive grid.
        if not self._offsets:
            return
        from ..screencap.timestamps import current_screenshot_for_position  # noqa: PLC0415
        match = current_screenshot_for_position(self._offsets, ms)
        if match is None:
            return
        try:
            idx = self._paths.index(match)
        except ValueError:
            return
        # In full-view mode, advance to the matched image; in grid
        # mode, select it (visual highlight). Don't open full-view
        # automatically from grid -- that would steal focus mid-
        # browse.
        if self._stack.currentIndex() == 1:  # full view
            if idx != self._current_index:
                self._current_index = idx
                self._refresh_full_view()
        else:
            if self._list.currentRow() != idx:
                self._list.blockSignals(True)
                try:
                    self._list.setCurrentRow(idx)
                finally:
                    self._list.blockSignals(False)

    def set_player_is_playing(self, playing: bool) -> None:
        self._player_bar.set_is_playing(playing)

    def set_screenshots(self, paths: list[Path]) -> None:
        """Repopulate the grid + reset to grid mode."""
        self._paths = list(paths)
        self._list.clear()
        for p in self._paths:
            item = QListWidgetItem(QIcon(self._thumb_pixmap(p)), _pretty_label(p), self._list)
            item.setData(Qt.ItemDataRole.UserRole, str(p))
            item.setToolTip(str(p))
            item.setSizeHint(QSize(_THUMB_W + 16, _THUMB_H + 32))
        self._grid_heading.setVisible(not self._paths)
        self._list.setVisible(bool(self._paths))
        if not self._paths and self._stack.currentIndex() == 1:
            # No images left; flip back to grid so the user isn't
            # staring at an empty full-view.
            self._stack.setCurrentIndex(0)
        else:
            # Preserve full-view position if the user was viewing an
            # image that still exists; otherwise step back into bounds.
            if self._stack.currentIndex() == 1:
                self._current_index = min(
                    self._current_index, len(self._paths) - 1,
                )
                self._refresh_full_view()

    # ------------------------------------------------------------------
    # Grid handlers

    def _on_thumb_clicked(self, item: QListWidgetItem) -> None:
        idx = self._list.row(item)
        self._show_full_for(idx)
        # If audio is loaded for this session AND we know this
        # thumbnail's recording-relative offset, seek the player to
        # that moment. Lines up with the metaphor: clicking a slide
        # in a recording playback means "jump to when this slide
        # was being shown".
        self._maybe_seek_to_thumb(idx)

    def _maybe_seek_to_thumb(self, idx: int) -> None:
        if not self._audio_available or not self._offsets:
            return
        if not (0 <= idx < len(self._paths)):
            return
        target_path = self._paths[idx]
        for path, offset_ms in self._offsets:
            if path == target_path:
                self.seek_ms_requested.emit(max(0, int(offset_ms)))
                return

    def _on_thumb_double_clicked(self, item: QListWidgetItem) -> None:
        # Single-click already opens; double-click is intentionally a
        # no-op-on-top so users who default to double-click don't end
        # up flipping back and forth.
        self._show_full_for(self._list.row(item))

    def _on_thumb_context_menu(self, pos) -> None:
        item = self._list.itemAt(pos)
        if item is None:
            return
        path = Path(item.data(Qt.ItemDataRole.UserRole))
        self._popup_actions_menu(self._list.viewport().mapToGlobal(pos), path)

    # ------------------------------------------------------------------
    # Full-view handlers

    def _show_full_for(self, index: int) -> None:
        if not (0 <= index < len(self._paths)):
            return
        self._current_index = index
        self._refresh_full_view()
        self._stack.setCurrentIndex(1)

    def _refresh_full_view(self) -> None:
        if not self._paths:
            return
        path = self._paths[self._current_index]
        self._full_view.set_image_path(path)
        self._caption.setText(
            f"{path.name}  --  {self._current_index + 1} of {len(self._paths)}",
        )
        self._prev_btn.setEnabled(self._current_index > 0)
        self._next_btn.setEnabled(self._current_index < len(self._paths) - 1)

    def _on_prev_clicked(self) -> None:
        if self._current_index > 0:
            self._current_index -= 1
            self._refresh_full_view()

    def _on_next_clicked(self) -> None:
        if self._current_index < len(self._paths) - 1:
            self._current_index += 1
            self._refresh_full_view()

    def _on_back_clicked(self) -> None:
        self._stack.setCurrentIndex(0)
        # Restore selection to the image the user was viewing so the
        # grid feels continuous.
        if 0 <= self._current_index < self._list.count():
            self._list.setCurrentRow(self._current_index)

    def _on_full_context_menu(self, pos) -> None:
        if not self._paths:
            return
        path = self._paths[self._current_index]
        self._popup_actions_menu(
            self._full_view.mapToGlobal(pos), path,
        )

    # ------------------------------------------------------------------
    # Shared menu

    def _popup_actions_menu(self, global_pos, path: Path) -> None:
        menu = QMenu(self)
        copy_action = QAction("Copy image to clipboard", menu)
        copy_action.triggered.connect(lambda: self._copy_to_clipboard(path))
        menu.addAction(copy_action)
        open_action = QAction("Open in default viewer", menu)
        open_action.triggered.connect(lambda: self._open_in_viewer(path))
        menu.addAction(open_action)
        menu.addSeparator()
        delete_action = QAction("Delete...", menu)
        delete_action.triggered.connect(lambda: self._confirm_delete(path))
        menu.addAction(delete_action)
        menu.exec(global_pos)

    def _copy_to_clipboard(self, path: Path) -> None:
        img = QImage(str(path))
        if img.isNull():
            return
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setImage(img)

    def _open_in_viewer(self, path: Path) -> None:
        from PyQt6.QtCore import QUrl  # noqa: PLC0415
        from PyQt6.QtGui import QDesktopServices  # noqa: PLC0415
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _confirm_delete(self, path: Path) -> None:
        confirm = QMessageBox.question(
            self,
            "Delete screenshot",
            f"Permanently delete this screenshot?\n\n{path.name}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm == QMessageBox.StandardButton.Yes:
            self.delete_requested.emit(path)

    # ------------------------------------------------------------------
    # Thumbnail helpers

    def _thumb_pixmap(self, path: Path) -> QPixmap:
        pix = QPixmap(str(path))
        if pix.isNull():
            return QPixmap()
        return pix.scaled(
            _THUMB_W, _THUMB_H,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )


def _pretty_label(path: Path) -> str:
    """Render NNNN-YYYYMMDDTHHMMSSZ.png as a human-friendly date."""
    stem = path.stem
    parts = stem.split("-", 1)
    if len(parts) == 2:
        seq, ts = parts
        # ts is e.g. 20260523T143200Z
        if len(ts) == 16 and ts.endswith("Z"):
            try:
                from datetime import datetime, timezone  # noqa: PLC0415
                dt = datetime.strptime(ts, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                return f"{seq}: {dt.astimezone().strftime('%H:%M:%S')}"
            except ValueError:
                pass
    return path.name
