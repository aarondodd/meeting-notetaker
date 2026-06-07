"""Per-session prompt edit dialog (#90).

The user clicks "Edit & Send..." (or "Edit & Copy Prompt...") beside
the standard Send / Generate button. We render the synthesis prompt
the way the standard button would, hand the result to this dialog
for one-shot editing, and dispatch the edited body via the same
downstream path (automation bridge or clipboard).

Distinct from the in-app prompt editor (#89): #89 edits the
*template* (with {{placeholders}}) and persists. This dialog edits
the *rendered* prompt (placeholders already substituted with this
session's transcript / notes / attendees) and dispatches it once.

Optional "Save as new template..." button lets a one-off tweak get
promoted to a permanent template if the user discovers a pattern
worth keeping.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFontDatabase, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..utils import prompts as prompts_mod


@dataclass
class SessionPromptEditResult:
    """Payload returned to the caller when the dialog accepts."""

    edited_body: str
    action: str  # "send" or "copy"


class SessionPromptEditDialog(QDialog):
    """One-shot editor for a session's rendered synthesis prompt.

    Caller pattern:

        dlg = SessionPromptEditDialog(
            rendered_prompt=body,
            session_title="Q3 Roadmap Review",
            template_name="standup",
            automation_enabled=True,
            parent=main_window,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            result = dlg.result_payload
            # result.action is "send" or "copy"; dispatch the edited
            # body through the matching downstream path.
    """

    def __init__(
        self,
        *,
        rendered_prompt: str,
        session_title: str,
        template_name: str,
        automation_enabled: bool,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Prompt for Session")
        self.resize(820, 600)
        self._automation_enabled = bool(automation_enabled)
        self.result_payload: Optional[SessionPromptEditResult] = None

        outer = QVBoxLayout(self)

        # ---- Header ----
        header_text = (
            f"<b>Edit prompt for &ldquo;{session_title}&rdquo;</b> "
            f"(using template: <code>{template_name or 'default'}</code>)<br>"
            "<i>Placeholders are already resolved against this session's "
            "transcript, notes, and attendees. Edit the body below, then "
            "send or copy.</i>"
        )
        header = QLabel(header_text, self)
        header.setWordWrap(True)
        header.setTextFormat(Qt.TextFormat.RichText)
        outer.addWidget(header)

        # ---- Editor ----
        self._editor = QPlainTextEdit(self)
        self._editor.setPlainText(rendered_prompt)
        self._editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._set_monospace_font(self._editor)
        outer.addWidget(self._editor, stretch=1)

        # ---- Stats footer (line + char count) ----
        self._stats_label = QLabel("", self)
        self._stats_label.setStyleSheet("color: palette(mid);")
        self._update_stats()
        self._editor.textChanged.connect(self._update_stats)
        outer.addWidget(self._stats_label)

        # ---- Action row ----
        action_row = QHBoxLayout()
        self._save_template_btn = QPushButton("Save as new template...", self)
        self._save_template_btn.setToolTip(
            "Save this edited body as a new prompt template so future "
            "sessions can pick it from the dropdown."
        )
        self._save_template_btn.clicked.connect(self._on_save_as_template)
        action_row.addWidget(self._save_template_btn)
        action_row.addStretch(1)
        self._cancel_btn = QPushButton("Cancel", self)
        self._cancel_btn.clicked.connect(self.reject)
        action_row.addWidget(self._cancel_btn)
        if automation_enabled:
            self._primary_btn = QPushButton("Send to LLM", self)
            self._primary_btn.setToolTip(
                "Dispatch the edited prompt through the synthesis "
                "automation bridge."
            )
            self._primary_btn.setDefault(True)
            self._primary_btn.clicked.connect(self._on_send_clicked)
        else:
            self._primary_btn = QPushButton("Copy to Clipboard", self)
            self._primary_btn.setToolTip(
                "Copy the edited prompt to the clipboard. Paste it into "
                "your LLM chat manually, then use Paste Response Back "
                "to bring the synthesis into the session."
            )
            self._primary_btn.setDefault(True)
            self._primary_btn.clicked.connect(self._on_copy_clicked)
        action_row.addWidget(self._primary_btn)
        outer.addLayout(action_row)

        # ---- Ctrl+Enter accepts the primary action ----
        accept_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self)
        accept_shortcut.activated.connect(self._primary_btn.click)
        accept_shortcut_alt = QShortcut(QKeySequence("Ctrl+Enter"), self)
        accept_shortcut_alt.activated.connect(self._primary_btn.click)

    # ---- handlers --------------------------------------------------------

    def _on_send_clicked(self) -> None:
        body = self._editor.toPlainText()
        if not body.strip():
            QMessageBox.warning(
                self, "Empty prompt",
                "The prompt body is empty. Add text before sending.",
            )
            return
        self.result_payload = SessionPromptEditResult(
            edited_body=body, action="send",
        )
        self.accept()

    def _on_copy_clicked(self) -> None:
        body = self._editor.toPlainText()
        if not body.strip():
            QMessageBox.warning(
                self, "Empty prompt",
                "The prompt body is empty. Add text before copying.",
            )
            return
        self.result_payload = SessionPromptEditResult(
            edited_body=body, action="copy",
        )
        self.accept()

    def _on_save_as_template(self) -> None:
        """Persist the edited body as a new template (#89 CRUD layer).

        Doesn't dispatch -- the user is asking to save the template,
        not to send. After save the primary button continues to work
        as usual (so the typical flow is Save as template... ->
        Send to LLM).
        """
        body = self._editor.toPlainText()
        if not body.strip():
            QMessageBox.warning(
                self, "Empty prompt",
                "Cannot save an empty body as a template.",
            )
            return
        name, ok = QInputDialog.getText(
            self, "Save as new template",
            "Template name (letters, digits, dash, underscore):",
        )
        if not ok:
            return
        try:
            validated = prompts_mod.validate_prompt_name(name)
            prompts_mod.create_prompt(validated, body=body)
        except prompts_mod.PromptError as exc:
            QMessageBox.warning(self, "Cannot save template", exc.reason)
            return
        QMessageBox.information(
            self, "Template saved",
            f"Saved as &ldquo;{validated}&rdquo;. It will appear in the "
            "prompt template picker on next session.",
        )

    # ---- helpers --------------------------------------------------------

    def _update_stats(self) -> None:
        body = self._editor.toPlainText()
        lines = body.count("\n") + (1 if body else 0)
        chars = len(body)
        self._stats_label.setText(f"{lines:,} lines &middot; {chars:,} chars")
        self._stats_label.setTextFormat(Qt.TextFormat.RichText)

    @staticmethod
    def _set_monospace_font(editor: QPlainTextEdit) -> None:
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setPointSize(max(10, font.pointSize()))
        editor.setFont(font)
