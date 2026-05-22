"""Markdown preview pane shared by the My Notes and Previous Notes tabs.

A thin QTextBrowser subclass with two additions:

1. Right-click on an image offers "Copy Image" -- puts the QImage on the
   system clipboard so the user can paste into Outlook, Word, Teams, etc.
   The stock QTextBrowser context menu has no image affordance; "Copy"
   is greyed out unless there is a text selection, which makes it look
   like the action is unsupported.

2. Images that would render wider than the visible viewport are clamped
   to viewport width on each setMarkdown() and on every resize, keeping
   their aspect ratio. The clamp queries the loaded resource for the
   image's natural size each pass so growing the window restores native
   size when there is room.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QUrl, Qt
from PyQt6.QtGui import (
    QAction,
    QImage,
    QPixmap,
    QTextCursor,
    QTextDocument,
    QTextImageFormat,
)
from PyQt6.QtWidgets import QApplication, QTextBrowser, QWidget


# Pixel breathing room subtracted from the viewport width when clamping.
# QTextBrowser's documentMargin defaults to 4px on each side; we leave a
# few extra pixels so a clamped image never butts up against the scroll
# bar gutter or the frame border.
_VIEWPORT_PADDING = 16


class MarkdownPreview(QTextBrowser):
    """QTextBrowser with image-aware context menu + viewport-width clamping."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setOpenExternalLinks(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)

    # ------------------------------------------------------------------
    # Public API

    def setMarkdown(self, text: str) -> None:  # type: ignore[override]
        super().setMarkdown(text)
        self._clamp_images_to_viewport()

    def setHtml(self, text: str) -> None:  # type: ignore[override]
        super().setHtml(text)
        self._clamp_images_to_viewport()

    # ------------------------------------------------------------------
    # Resize hook

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._clamp_images_to_viewport()

    # ------------------------------------------------------------------
    # Image clamping

    def _clamp_images_to_viewport(self) -> None:
        max_width = max(0, self.viewport().width() - _VIEWPORT_PADDING)
        if max_width <= 0:
            return
        clamp_image_widths(self.document(), max_width)

    # ------------------------------------------------------------------
    # Context menu

    def contextMenuEvent(self, event) -> None:  # type: ignore[override]
        menu = self.createStandardContextMenu(event.pos())
        image_name = self._image_name_at(event.pos())
        if image_name:
            image = self._resolve_image(image_name)
            if image is not None and not image.isNull():
                action = QAction("Copy Image", menu)
                action.triggered.connect(lambda _checked=False, img=image: self._copy_image_to_clipboard(img))
                # Prepend so Copy Image sits at the top of the menu where
                # the user expects an image-specific action.
                first = menu.actions()[0] if menu.actions() else None
                if first is not None:
                    menu.insertAction(first, action)
                    menu.insertSeparator(first)
                else:
                    menu.addAction(action)
        menu.exec(event.globalPos())

    def _image_name_at(self, pos) -> str:
        """Return the image's source URL string at viewport position `pos`.

        Returns "" when the position is not over an image. Qt stores the
        image as a single character whose char format reports
        isImageFormat(); we have to look at the character that follows
        the cursor, not the one before it, because cursorForPosition
        sits between glyphs.
        """
        cursor = self.cursorForPosition(pos)
        cursor.movePosition(
            QTextCursor.MoveOperation.NextCharacter,
            QTextCursor.MoveMode.KeepAnchor,
        )
        fmt = cursor.charFormat()
        if fmt.isImageFormat():
            return fmt.toImageFormat().name() or ""
        return ""

    def _resolve_image(self, name: str) -> Optional[QImage]:
        resource = self.document().resource(
            QTextDocument.ResourceType.ImageResource,
            QUrl(name),
        )
        return _coerce_to_qimage(resource)

    def _copy_image_to_clipboard(self, image: QImage) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setImage(image)


def clamp_image_widths(doc: QTextDocument, max_width: float) -> int:
    """Clamp every image in `doc` to at most `max_width` document units.

    Returns the number of fragments whose width was adjusted. Walks the
    document block-by-block, fragment-by-fragment; for each image
    fragment, asks the document for the loaded QImage so we know the
    natural pixel size, then merges a new QTextImageFormat that either
    pins the width down (natural > max) or releases any prior pin
    (natural <= max). Releasing on grow means a wide window restores
    the image's native size.

    Pure-function entry point so PDF export, print, and the live preview
    can share the same walk; testable without a top-level widget.
    """
    if max_width <= 0:
        return 0
    adjustments = 0
    block = doc.begin()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            if frag.isValid():
                fmt = frag.charFormat()
                if fmt.isImageFormat():
                    img_fmt = fmt.toImageFormat()
                    natural_w = _natural_width(doc, img_fmt)
                    if natural_w > 0:
                        target = (
                            float(max_width) if natural_w > max_width else 0.0
                        )
                        current = img_fmt.width()
                        # 0 means "use natural" -- skip the merge when
                        # we'd be writing 0 over an already-0 width, or
                        # writing the same clamp twice in a row.
                        if not _widths_equivalent(current, target):
                            cursor = QTextCursor(doc)
                            cursor.setPosition(frag.position())
                            cursor.setPosition(
                                frag.position() + frag.length(),
                                QTextCursor.MoveMode.KeepAnchor,
                            )
                            new_fmt = QTextImageFormat(img_fmt)
                            new_fmt.setWidth(target)
                            cursor.mergeCharFormat(new_fmt)
                            adjustments += 1
            it += 1
        block = block.next()
    return adjustments


def _natural_width(doc: QTextDocument, img_fmt: QTextImageFormat) -> float:
    """Return the image's natural pixel width.

    Asks the document for the cached image resource; falls back to the
    format's own width if the document can't resolve it (e.g. a missing
    file). Returning 0 signals "unknown" and the caller skips the clamp.

    Qt's default QTextDocument hands back QPixmap for image resources;
    our PrintTextDocument subclass hands back QImage. _coerce_to_qimage
    handles both so the clamp works against either.
    """
    name = img_fmt.name()
    if not name:
        return 0.0
    resource = doc.resource(QTextDocument.ResourceType.ImageResource, QUrl(name))
    image = _coerce_to_qimage(resource)
    if image is not None and not image.isNull():
        return float(image.width())
    # No loadable resource. The format's own width is a last-resort hint;
    # if the user pasted an image with an explicit width attribute the
    # markdown converter records it here.
    return float(img_fmt.width() or 0)


def _coerce_to_qimage(resource) -> Optional[QImage]:
    """Return a QImage view of an ImageResource, or None.

    Accepts the values QTextDocument.resource() can hand back: QImage,
    QPixmap, or None / invalid. Converts QPixmap to QImage so the rest
    of the code has one shape to reason about.
    """
    if isinstance(resource, QImage):
        return resource if not resource.isNull() else None
    if isinstance(resource, QPixmap):
        if resource.isNull():
            return None
        return resource.toImage()
    return None


def _widths_equivalent(a: float, b: float) -> bool:
    return abs(a - b) < 0.5
