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
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)


_THUMB_W = 160
_THUMB_H = 100


class SlidesWidget(QWidget):
    """Replaces a one-pane image dump with a thumbnail + full-view UI."""

    delete_requested = pyqtSignal(Path)  # MainApp does the unlink

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._paths: list[Path] = []
        self._current_index: int = 0  # used while in full mode

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

        self._full_view = _ScaledImageLabel(full_page)
        self._full_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._full_view.customContextMenuRequested.connect(
            self._on_full_context_menu
        )
        full_layout.addWidget(self._full_view, 1)

        self._stack.addWidget(grid_page)
        self._stack.addWidget(full_page)
        outer.addWidget(self._stack)

    # ------------------------------------------------------------------
    # Public API

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
        self._show_full_for(self._list.row(item))

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


class _ScaledImageLabel(QLabel):
    """QLabel that keeps its pixmap fit-to-width on resize.

    Plain QLabel renders the pixmap at native size, which makes the
    full-view image either spill out of the pane or render with a
    huge margin. Scaling on resize gives the user a "open this
    screenshot inline at viewport size" experience.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._source: Optional[QPixmap] = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumHeight(200)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding,
        )

    def set_image_path(self, path: Path) -> None:
        pix = QPixmap(str(path))
        self._source = None if pix.isNull() else pix
        self._refresh()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._refresh()

    def _refresh(self) -> None:
        if self._source is None:
            self.clear()
            return
        scaled = self._source.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.setPixmap(scaled)


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
