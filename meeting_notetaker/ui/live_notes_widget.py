"""Markdown editor widget for the My Notes tab.

Wraps a QPlainTextEdit (edit mode) and a QTextBrowser (preview mode) in a
QStackedWidget, with a QToolBar above for common Markdown formatting
actions and a Preview/Edit toggle.

The widget intentionally treats the buffer as Markdown source text, not a
WYSIWYG document. Toolbar actions insert Markdown syntax at the current
cursor / selection. Preview mode renders the current source via
QTextBrowser.setMarkdown.

Exposes textChanged signal, toPlainText(), setPlainText(),
setPlaceholderText(), and is_in_preview() for callers (SessionView) to
treat it as a drop-in replacement for QPlainTextEdit plus a couple of
extras.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QAction, QImage, QKeySequence, QTextCursor
from PyQt6.QtWidgets import (
    QFileDialog,
    QMessageBox,
    QPlainTextEdit,
    QStackedWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ..utils.images import (
    IMAGES_SUBDIR,
    copy_image_file,
    has_supported_extension,
    join_relative,
    markdown_image_ref,
    save_qimage,
)
from .markdown_preview import MarkdownPreview
from .preview_with_toc import PreviewWithToc


class _MarkdownEditor(QPlainTextEdit):
    """QPlainTextEdit that hands off image paste events to the parent widget.

    Standard QPlainTextEdit ignores image MIME data on paste (drops it
    silently). This subclass detects an image payload and emits
    `image_pasted`; the parent (`LiveNotesWidget`) handles save-to-disk
    and markdown-insert. All non-image paste paths fall through to the
    default implementation.
    """

    image_pasted = pyqtSignal(object)  # QImage; `object` keeps Qt's type-check loose

    def insertFromMimeData(self, source) -> None:  # type: ignore[override]
        if source is not None and source.hasImage():
            data = source.imageData()
            if data is not None:
                self.image_pasted.emit(data)
                return
        super().insertFromMimeData(source)

    def canInsertFromMimeData(self, source) -> bool:  # type: ignore[override]
        if source is not None and source.hasImage():
            return True
        return super().canInsertFromMimeData(source)


class LiveNotesWidget(QWidget):
    """Markdown-aware editor with formatting toolbar and preview toggle."""

    textChanged = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._session_dir: Optional[Path] = None
        # Session-linked Contacts list. When at least one contact has
        # rich-field data (title / company / email / phone), the
        # preview renders the Attendees section as a Markdown table
        # matching the PDF export. The source text isn't touched --
        # editing stays bullet-based (#56).
        self._session_contacts: list = []
        # Pre-parsed AppendixData for the preview transform (#64).
        # SessionView's _refresh_appendix_trays builds this from the
        # current notes + live_notes + session attachments and
        # hands it here. None disables the appendix injection (e.g.
        # before a session is loaded).
        self._appendix_data = None
        self._toolbar = QToolBar(self)
        self._toolbar.setMovable(False)
        self._toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)

        self._editor = _MarkdownEditor(self)
        self._editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._editor.textChanged.connect(self.textChanged.emit)
        self._editor.image_pasted.connect(self._on_image_pasted)

        # v0.7.0 tweak #7: My Notes + Synthesis preview gains a
        # clickable table-of-contents sidebar on the right. The
        # PreviewWithToc wrapper exposes the same setMarkdown /
        # setSearchPaths / setPlaceholderText API as MarkdownPreview
        # so the rest of this widget doesn't need to know.
        self._preview = PreviewWithToc(self)

        self._stack = QStackedWidget(self)
        self._stack.addWidget(self._editor)   # index 0
        self._stack.addWidget(self._preview)  # index 1

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._toolbar)
        layout.addWidget(self._stack, 1)

        self._build_actions()

    # ---- public API --------------------------------------------------------

    def toPlainText(self) -> str:
        return self._editor.toPlainText()

    def setPlainText(self, text: str) -> None:
        self._editor.setPlainText(text)
        if self.is_in_preview():
            self._preview.setMarkdown(self._preview_text(text))

    def setPlaceholderText(self, text: str) -> None:
        self._editor.setPlaceholderText(text)

    def set_session_contacts(self, contacts) -> None:
        """Drive the preview-side Attendees table substitution (#56).

        SessionView calls this whenever the session's resolved Contact
        list changes. The source markdown buffer is untouched -- the
        substitution happens only inside `setMarkdown` for the preview
        pane. Edit mode always shows the user's actual buffer.
        """
        self._session_contacts = list(contacts or [])
        # If we're currently in preview, re-render with the new
        # contacts so the table appears immediately.
        if self.is_in_preview():
            self._preview.setMarkdown(
                self._preview_text(self._editor.toPlainText())
            )

    def set_appendix_data(self, data) -> None:
        """Drive the preview-time Appendix injection (#64).

        SessionView's _refresh_appendix_trays parses the current
        notes + live_notes + session attachments and hands an
        AppendixData payload here. The preview pane then renders
        the unified ``## Appendix (auto-extracted)`` section in
        place of the raw JSON appendix blocks.
        """
        self._appendix_data = data
        if self.is_in_preview():
            self._preview.setMarkdown(
                self._preview_text(self._editor.toPlainText())
            )

    def _preview_text(self, source: str) -> str:
        """Apply preview-time transformations.

        Currently two layered transforms, applied in order:
          1. Attendees bullet list -> rich-fields table (#56)
          2. Raw JSON appendices -> rendered ``## Appendix`` (#64)

        Source-text mutation lives here -- callers always pass the
        unmodified user buffer.
        """
        out = source
        # #56: Attendees table swap.
        if self._session_contacts:
            from ..utils.live_notes import (  # noqa: PLC0415
                replace_attendees_section_with_table,
                should_render_attendees_as_table,
            )
            if should_render_attendees_as_table(self._session_contacts):
                out = replace_attendees_section_with_table(
                    out, self._session_contacts,
                )
        # #64: Appendix injection.
        if self._appendix_data is not None:
            from ..utils.appendix_transform import inject_appendix  # noqa: PLC0415
            out = inject_appendix(out, self._appendix_data)
        return out

    def find_target(self) -> QWidget:
        """Return the active widget for Ctrl+F to bind to.

        Edit mode -> the QPlainTextEdit. Preview mode -> the inner
        MarkdownPreview (PreviewWithToc.find_target() unwraps past
        the TOC sidebar so Ctrl+F lands on the text, not the
        heading list).
        """
        current = self._stack.currentWidget()
        if hasattr(current, "find_target") and current is not self._editor:
            return current.find_target()
        return current

    def is_in_preview(self) -> bool:
        return self._stack.currentWidget() is self._preview

    def set_preview_mode(self, on: bool) -> None:
        """Programmatically flip to Preview (`on=True`) or Edit (`on=False`).

        Used by the Synthesis tab where preview-first is the right default
        (the user is reading LLM output and only sometimes editing). The
        QAction triggers the existing `_on_toggle_preview` path so the
        toolbar checkbox state and the stack stay in sync.
        """
        if on != self._a_preview.isChecked():
            self._a_preview.setChecked(on)

    def set_session_dir(self, path: Optional[Path]) -> None:
        """Bind the editor to a session folder so images can be saved alongside.

        Drives two behaviours:
          - The "Image..." toolbar action and clipboard image-paste both
            write into <path>/images/.
          - The Preview pane resolves relative image refs (images/foo.png)
            against <path> so they render correctly.

        Passing None disables image insertion (the toolbar action shows a
        guard message instead).
        """
        self._session_dir = path
        if path is not None:
            self._preview.setSearchPaths([str(path)])
        else:
            self._preview.setSearchPaths([])

    # ---- toolbar setup -----------------------------------------------------

    def _build_actions(self) -> None:
        tb = self._toolbar

        self._a_bold = QAction("B", self)
        self._a_bold.setToolTip("Bold (Ctrl+B) -- wraps selection in **...**")
        self._a_bold.setShortcut(QKeySequence("Ctrl+B"))
        self._a_bold.triggered.connect(lambda: self._wrap_selection("**"))
        tb.addAction(self._a_bold)

        self._a_italic = QAction("I", self)
        self._a_italic.setToolTip("Italic (Ctrl+I) -- wraps selection in *...*")
        self._a_italic.setShortcut(QKeySequence("Ctrl+I"))
        self._a_italic.triggered.connect(lambda: self._wrap_selection("*"))
        tb.addAction(self._a_italic)

        tb.addSeparator()

        self._a_h1 = QAction("H1", self)
        self._a_h1.setToolTip("Heading 1 -- prefixes the current line with #")
        self._a_h1.triggered.connect(lambda: self._set_line_prefix("# ", heading=True))
        tb.addAction(self._a_h1)

        self._a_h2 = QAction("H2", self)
        self._a_h2.setToolTip("Heading 2 -- prefixes the current line with ##")
        self._a_h2.triggered.connect(lambda: self._set_line_prefix("## ", heading=True))
        tb.addAction(self._a_h2)

        self._a_h3 = QAction("H3", self)
        self._a_h3.setToolTip("Heading 3 -- prefixes the current line with ###")
        self._a_h3.triggered.connect(lambda: self._set_line_prefix("### ", heading=True))
        tb.addAction(self._a_h3)

        tb.addSeparator()

        self._a_bullet = QAction("List", self)
        self._a_bullet.setToolTip("Bulleted list -- prefixes selected lines with -")
        self._a_bullet.triggered.connect(lambda: self._set_line_prefix("- "))
        tb.addAction(self._a_bullet)

        self._a_number = QAction("1. List", self)
        self._a_number.setToolTip("Numbered list -- prefixes selected lines with 1., 2., ...")
        self._a_number.triggered.connect(self._numbered_list)
        tb.addAction(self._a_number)

        self._a_task = QAction("Task", self)
        self._a_task.setToolTip("Task list -- prefixes selected lines with - [ ]")
        self._a_task.triggered.connect(lambda: self._set_line_prefix("- [ ] "))
        tb.addAction(self._a_task)

        tb.addSeparator()

        self._a_quote = QAction("Quote", self)
        self._a_quote.setToolTip("Blockquote -- prefixes selected lines with >")
        self._a_quote.triggered.connect(lambda: self._set_line_prefix("> "))
        tb.addAction(self._a_quote)

        self._a_code = QAction("Code", self)
        self._a_code.setToolTip("Inline code (Ctrl+`) -- wraps selection in `...`")
        self._a_code.setShortcut(QKeySequence("Ctrl+`"))
        self._a_code.triggered.connect(lambda: self._wrap_selection("`"))
        tb.addAction(self._a_code)

        self._a_codeblock = QAction("Code Block", self)
        self._a_codeblock.setToolTip("Fenced code block -- wraps selection in triple backticks")
        self._a_codeblock.triggered.connect(self._insert_code_block)
        tb.addAction(self._a_codeblock)

        self._a_link = QAction("Link", self)
        self._a_link.setToolTip("Link (Ctrl+K) -- inserts [text](url) at the cursor")
        self._a_link.setShortcut(QKeySequence("Ctrl+K"))
        self._a_link.triggered.connect(self._insert_link)
        tb.addAction(self._a_link)

        self._a_image = QAction("Image", self)
        self._a_image.setToolTip(
            "Insert an image -- copies the file into the session's images/ "
            "folder and inserts a Markdown reference. Clipboard paste of an "
            "image works too."
        )
        self._a_image.triggered.connect(self._insert_image_action)
        tb.addAction(self._a_image)

        self._a_hr = QAction("HR", self)
        self._a_hr.setToolTip("Horizontal rule -- inserts a --- divider on its own line")
        self._a_hr.triggered.connect(self._insert_hr)
        tb.addAction(self._a_hr)

        tb.addSeparator()

        self._a_preview = QAction("Preview", self)
        self._a_preview.setToolTip("Toggle between Markdown edit mode and rendered preview")
        self._a_preview.setCheckable(True)
        self._a_preview.toggled.connect(self._on_toggle_preview)
        tb.addAction(self._a_preview)

        # All formatting actions are sourced from this list so preview-mode
        # disabling stays in one place.
        self._formatting_actions = [
            self._a_bold, self._a_italic,
            self._a_h1, self._a_h2, self._a_h3,
            self._a_bullet, self._a_number, self._a_task,
            self._a_quote, self._a_code, self._a_codeblock,
            self._a_link, self._a_image, self._a_hr,
        ]

    # ---- preview toggle ----------------------------------------------------

    def _on_toggle_preview(self, checked: bool) -> None:
        if checked:
            self._preview.setMarkdown(
                self._preview_text(self._editor.toPlainText())
            )
            self._stack.setCurrentWidget(self._preview)
            self._a_preview.setText("Edit")
        else:
            self._stack.setCurrentWidget(self._editor)
            self._a_preview.setText("Preview")
            self._editor.setFocus()
        for action in self._formatting_actions:
            action.setEnabled(not checked)

    # ---- formatting helpers ------------------------------------------------

    def _wrap_selection(self, marker: str, end_marker: Optional[str] = None) -> None:
        cursor = self._editor.textCursor()
        end_marker = end_marker or marker
        if cursor.hasSelection():
            text = cursor.selectedText()
            cursor.insertText(f"{marker}{text}{end_marker}")
        else:
            insert = f"{marker}{end_marker}"
            cursor.insertText(insert)
            new_pos = cursor.position() - len(end_marker)
            cursor.setPosition(new_pos)
            self._editor.setTextCursor(cursor)

    def _set_line_prefix(self, prefix: str, *, heading: bool = False) -> None:
        """Apply a per-line prefix to the selected lines (or the current line).

        If `heading` is True, any existing leading `#` markers are stripped
        before the new prefix is applied -- so clicking H2 on a line that is
        already H3 replaces the heading level cleanly.
        """
        cursor = self._editor.textCursor()
        start = cursor.selectionStart()
        end = cursor.selectionEnd()
        cursor.setPosition(start)
        cursor.movePosition(QTextCursor.MoveOperation.StartOfLine)
        line_start = cursor.position()
        cursor.setPosition(end)
        cursor.movePosition(QTextCursor.MoveOperation.EndOfLine)
        line_end = cursor.position()
        cursor.setPosition(line_start)
        cursor.setPosition(line_end, QTextCursor.MoveMode.KeepAnchor)
        block = cursor.selectedText()
        # QTextCursor.selectedText() uses U+2029 (paragraph separator) for newlines.
        lines = block.split(chr(0x2029))
        new_lines: list[str] = []
        for line in lines:
            stripped = line
            if heading:
                # Drop any existing heading prefix so toggling levels is clean.
                lstripped = stripped.lstrip()
                if lstripped.startswith("#"):
                    rest = lstripped.lstrip("#").lstrip()
                    stripped = rest
                else:
                    stripped = lstripped
            else:
                # Preserve indentation when prefixing for non-heading actions.
                pass
            new_lines.append(f"{prefix}{stripped}")
        cursor.insertText("\n".join(new_lines))

    def _numbered_list(self) -> None:
        cursor = self._editor.textCursor()
        start = cursor.selectionStart()
        end = cursor.selectionEnd()
        cursor.setPosition(start)
        cursor.movePosition(QTextCursor.MoveOperation.StartOfLine)
        line_start = cursor.position()
        cursor.setPosition(end)
        cursor.movePosition(QTextCursor.MoveOperation.EndOfLine)
        line_end = cursor.position()
        cursor.setPosition(line_start)
        cursor.setPosition(line_end, QTextCursor.MoveMode.KeepAnchor)
        block = cursor.selectedText()
        lines = block.split(chr(0x2029))
        new_lines = [f"{i + 1}. {line}" for i, line in enumerate(lines)]
        cursor.insertText("\n".join(new_lines))

    def _insert_code_block(self) -> None:
        cursor = self._editor.textCursor()
        if cursor.hasSelection():
            text = cursor.selectedText().replace(chr(0x2029), chr(10))
            cursor.insertText(f"```\n{text}\n```")
        else:
            cursor.insertText("```\n\n```")
            new_pos = cursor.position() - 4  # position before the closing fence
            cursor.setPosition(new_pos)
            self._editor.setTextCursor(cursor)

    def _insert_link(self) -> None:
        cursor = self._editor.textCursor()
        if cursor.hasSelection():
            text = cursor.selectedText()
            cursor.insertText(f"[{text}](url)")
        else:
            cursor.insertText("[text](url)")

    def _insert_hr(self) -> None:
        cursor = self._editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.EndOfLine)
        cursor.insertText("\n\n---\n\n")
        self._editor.setTextCursor(cursor)

    # ---- image handlers ----------------------------------------------------

    def _on_image_pasted(self, image) -> None:
        """Handle a QImage payload pasted from the clipboard.

        Saves directly with a default alt of 'image'; the user can edit
        the markdown afterward. The interactive alt/caption dialog was
        removed in v0.6.5 as a clarification step that added friction
        without commensurate value.
        """
        if not isinstance(image, QImage) or image.isNull():
            return
        if self._session_dir is None:
            QMessageBox.information(
                self,
                "Image paste",
                "Select or create a session first so the image can be saved "
                "alongside the notes.",
            )
            return
        try:
            saved = save_qimage(image, self._session_dir / IMAGES_SUBDIR)
        except OSError as exc:
            QMessageBox.warning(self, "Image paste", f"Could not save image: {exc}")
            return
        self._insert_image_markdown(saved.name, "image", "")

    def _insert_image_action(self) -> None:
        """Toolbar handler: copy a file from disk into the session images dir.

        Uses the source file's stem as the alt text so the markdown
        carries something descriptive without prompting the user. Edit
        in source mode if a different alt is wanted.
        """
        if self._session_dir is None:
            QMessageBox.information(
                self,
                "Insert image",
                "Select or create a session first so the image lives with the notes.",
            )
            return
        path_str, _filter = QFileDialog.getOpenFileName(
            self,
            "Insert image",
            "",
            "Images (*.png *.jpg *.jpeg *.gif *.webp *.bmp)",
        )
        if not path_str:
            return
        src = Path(path_str)
        if not src.exists() or not has_supported_extension(src.name):
            QMessageBox.warning(
                self,
                "Insert image",
                "Unsupported image type or missing file.",
            )
            return
        try:
            saved = copy_image_file(src, self._session_dir / IMAGES_SUBDIR)
        except OSError as exc:
            QMessageBox.warning(self, "Insert image", f"Could not copy image: {exc}")
            return
        self._insert_image_markdown(saved.name, saved.stem, "")

    def _insert_image_markdown(self, filename: str, alt: str, caption: str) -> None:
        """Insert the Markdown image ref on its own line at the cursor."""
        markdown = markdown_image_ref(join_relative(IMAGES_SUBDIR, filename), alt, caption)
        cursor = self._editor.textCursor()
        block = cursor.block()
        # Push to the start of the next line if the cursor is mid-line.
        if cursor.position() > block.position():
            cursor.movePosition(QTextCursor.MoveOperation.EndOfLine)
            cursor.insertText("\n")
        cursor.insertText(markdown + "\n")
        self._editor.setTextCursor(cursor)
        self._editor.setFocus()
