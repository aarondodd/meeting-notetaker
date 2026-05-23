"""Side rail showing screenshot thumbnails anchored to transcript lines.

Lives to the right of the transcript editor in the Transcript tab's
idle layout. Each thumbnail is positioned at the y-coordinate of the
transcript line it's anchored to (closest match by recording-
relative time). The rail's vertical scroll is bound to the editor's
scroll bar so the thumbnails stay aligned with their lines.

When the user right-clicks a thumbnail, the menu mirrors the
Slides-tab menu (Copy / Open in viewer / Delete) so the affordance
is consistent across the two surfaces.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QPoint, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QImage, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QLabel,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QScrollArea,
    QWidget,
)


_THUMB_W = 140
_THUMB_H = 88
# Horizontal padding inside the rail so the thumbnail sits a few
# pixels off the left edge instead of pressed against the splitter.
_RAIL_LEFT_PAD = 6


class ScreenshotRail(QScrollArea):
    """A vertical column of screenshot thumbnails anchored to transcript blocks.

    Public API:
      * set_transcript_view(editor): bind the rail's scroll bar to
        the editor so the thumbnails track its scroll position.
      * set_anchors(items): rebuild the rail. ``items`` is a list of
        (Path, block_number); the rail looks up each block's y in
        the editor and places its thumbnail there.

    Emits delete_requested(Path) for the right-click Delete action.
    """

    delete_requested = pyqtSignal(Path)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._editor: Optional[QPlainTextEdit] = None
        # Tuples of (Path, block_number, QLabel) so we can re-layout
        # thumbnails on editor scroll / resize without rebuilding.
        self._anchors: list[tuple[Path, int, QLabel]] = []

        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Rail's own scroll bar is hidden -- scroll is driven by the
        # transcript editor. Setting the policy to AlwaysOff also
        # spares us layout reflows when the bar would otherwise show.
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self.setFixedWidth(_THUMB_W + 2 * _RAIL_LEFT_PAD)

        # Inner widget is a positioned canvas; we move each QLabel
        # explicitly rather than relying on a layout, because the
        # anchored y can be anywhere down the document.
        self._canvas = QWidget()
        self._canvas.setMinimumWidth(_THUMB_W + 2 * _RAIL_LEFT_PAD)
        self.setWidget(self._canvas)
        self.setWidgetResizable(False)

    # ------------------------------------------------------------------
    # Public API

    def set_transcript_view(self, editor: QPlainTextEdit) -> None:
        """Bind the rail's scroll position to the editor's scroll bar.

        Called once when the SessionView constructs the rail; the
        binding persists for the rail's lifetime even as transcript
        content changes.
        """
        if self._editor is not None:
            self._editor.verticalScrollBar().valueChanged.disconnect(self._on_editor_scrolled)
            self._editor.document().documentLayout().documentSizeChanged.disconnect(self._reposition_all)
        self._editor = editor
        editor.verticalScrollBar().valueChanged.connect(self._on_editor_scrolled)
        editor.document().documentLayout().documentSizeChanged.connect(self._reposition_all)

    def set_anchors(self, items: list[tuple[Path, int]]) -> None:
        """Rebuild the rail's thumbnails.

        ``items`` is (Path, block_number) in display order. Each gets
        a QLabel with a scaled thumbnail; layout y comes from the
        editor's documentLayout via _block_y_for.
        """
        # Tear down the old labels first; QLabel-on-canvas widgets
        # don't get garbage-collected automatically.
        for _path, _block, label in self._anchors:
            label.deleteLater()
        self._anchors = []
        for path, block in items:
            label = QLabel(self._canvas)
            pix = self._thumbnail_pixmap(path)
            if pix.isNull():
                # Couldn't decode; skip the label so we don't leave a
                # broken empty rectangle on the rail.
                label.deleteLater()
                continue
            label.setPixmap(pix)
            label.setFixedSize(_THUMB_W, _THUMB_H)
            label.setStyleSheet("border: 1px solid #888;")
            label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            label.customContextMenuRequested.connect(
                lambda pos, p=path, lbl=label: self._popup_actions_menu(lbl, pos, p),
            )
            label.show()
            self._anchors.append((path, block, label))
        self._reposition_all()

    # ------------------------------------------------------------------
    # Layout

    def _on_editor_scrolled(self, value: int) -> None:
        # Mirror the editor's scrollbar position so the rail's
        # canvas slides in lockstep with the document.
        self.verticalScrollBar().setValue(value)
        self._reposition_all()

    def _reposition_all(self, *_args) -> None:
        if self._editor is None:
            return
        # Canvas height matches the document's full layout so the
        # rail can be scrolled to the same range as the editor.
        doc_height = int(
            self._editor.document().documentLayout().documentSize().height()
        )
        self._canvas.setFixedHeight(max(doc_height, self.viewport().height()))
        for path, block_number, label in self._anchors:
            y = self._block_y_for(block_number)
            if y is None:
                label.hide()
                continue
            label.move(_RAIL_LEFT_PAD, y)
            label.show()

    def _block_y_for(self, block_number: int) -> Optional[int]:
        """Return the document-space y-coord of a transcript block."""
        if self._editor is None:
            return None
        doc = self._editor.document()
        block = doc.findBlockByNumber(block_number)
        if not block.isValid():
            return None
        rect = doc.documentLayout().blockBoundingRect(block)
        return int(rect.y())

    # ------------------------------------------------------------------
    # Right-click menu (mirror of SlidesWidget's actions)

    def _popup_actions_menu(self, label: QLabel, pos: QPoint, path: Path) -> None:
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
        menu.exec(label.mapToGlobal(pos))

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

    def _thumbnail_pixmap(self, path: Path) -> QPixmap:
        pix = QPixmap(str(path))
        if pix.isNull():
            return QPixmap()
        return pix.scaled(
            _THUMB_W, _THUMB_H,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
