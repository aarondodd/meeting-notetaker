"""Insert Screenshot dialog -- pick an existing session capture.

Modal thumbnail-grid picker triggered from the My Notes editor's
'Insert Screenshot...' toolbar action (#111). Lists every PNG in
the session's screenshots/ dir as a thumbnail; single-click selects,
double-click (or OK) accepts. Returns the chosen Path via
``selected_path()`` after exec() reports Accepted.

The dialog itself does no file I/O beyond reading the thumbnail
pixmaps -- the caller (LiveNotesWidget._insert_screenshot_action)
hands it the list of paths to display and decides what to do with
the answer.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)


# Same dimensions the Slides tab uses so the dialog visually matches
# the rest of the screenshots UX.
_THUMB_W = 160
_THUMB_H = 100


class InsertScreenshotDialog(QDialog):
    """Thumbnail-grid picker over the supplied screenshot paths.

    Empty-state handling: if ``screenshots`` is empty, the list is
    hidden and a helper label explains how to capture one. The OK
    button stays disabled until a thumbnail is selected.
    """

    def __init__(
        self,
        screenshots: list[Path],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Insert Screenshot")
        self.setModal(True)
        self.resize(640, 480)
        self._screenshots: list[Path] = list(screenshots)
        self._selected: Optional[Path] = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Pick a screen capture from this session to insert at "
            "the cursor in My Notes.",
            self,
        ))

        self._list = QListWidget(self)
        self._list.setViewMode(QListWidget.ViewMode.IconMode)
        self._list.setIconSize(QSize(_THUMB_W, _THUMB_H))
        self._list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self._list.setSpacing(8)
        self._list.setMovement(QListWidget.Movement.Static)
        self._list.setUniformItemSizes(True)
        self._list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection,
        )
        self._list.itemDoubleClicked.connect(
            lambda _it: self._accept_if_selected(),
        )

        self._empty_label = QLabel(
            "No screen captures in this session yet. Capture one "
            "via the Screen Capture sidebar in the right pane while "
            "recording, then come back to insert it.",
            self,
        )
        self._empty_label.setStyleSheet("color: gray;")
        self._empty_label.setWordWrap(True)

        if not self._screenshots:
            self._list.hide()
            layout.addWidget(self._empty_label, 1)
        else:
            self._empty_label.hide()
            layout.addWidget(self._list, 1)
            for path in self._screenshots:
                item = QListWidgetItem(path.name)
                item.setIcon(QIcon(self._thumb_pixmap(path)))
                item.setData(Qt.ItemDataRole.UserRole, str(path))
                # Hide the long filename under the thumbnail visually
                # but keep it queryable via tooltip; the grid stays
                # uniform-width that way.
                item.setToolTip(path.name)
                self._list.addItem(item)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        self._ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_btn.setEnabled(False)
        self._ok_btn.setText("Insert")
        buttons.accepted.connect(self._accept_if_selected)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._list.itemSelectionChanged.connect(self._on_selection_changed)

    # ---- public API --------------------------------------------------------

    def selected_path(self) -> Optional[Path]:
        return self._selected

    # ---- internal ----------------------------------------------------------

    def _on_selection_changed(self) -> None:
        self._ok_btn.setEnabled(bool(self._list.selectedItems()))

    def _accept_if_selected(self) -> None:
        items = self._list.selectedItems()
        if not items:
            return
        self._selected = Path(items[0].data(Qt.ItemDataRole.UserRole))
        self.accept()

    def _thumb_pixmap(self, path: Path) -> QPixmap:
        pix = QPixmap(str(path))
        if pix.isNull():
            return QPixmap(_THUMB_W, _THUMB_H)
        return pix.scaled(
            _THUMB_W, _THUMB_H,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
