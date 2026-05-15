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

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QAction, QKeySequence, QTextCursor
from PyQt6.QtWidgets import (
    QPlainTextEdit,
    QStackedWidget,
    QTextBrowser,
    QToolBar,
    QVBoxLayout,
    QWidget,
)


class LiveNotesWidget(QWidget):
    """Markdown-aware editor with formatting toolbar and preview toggle."""

    textChanged = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._toolbar = QToolBar(self)
        self._toolbar.setMovable(False)
        self._toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)

        self._editor = QPlainTextEdit(self)
        self._editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._editor.textChanged.connect(self.textChanged.emit)

        self._preview = QTextBrowser(self)
        self._preview.setOpenExternalLinks(True)

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
            self._preview.setMarkdown(text)

    def setPlaceholderText(self, text: str) -> None:
        self._editor.setPlaceholderText(text)

    def is_in_preview(self) -> bool:
        return self._stack.currentWidget() is self._preview

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
            self._a_link, self._a_hr,
        ]

    # ---- preview toggle ----------------------------------------------------

    def _on_toggle_preview(self, checked: bool) -> None:
        if checked:
            self._preview.setMarkdown(self._editor.toPlainText())
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
