"""Help > Diagnose Outlook... dialog.

Runs `outlook_calendar.diagnose()` and renders a step-by-step report so a
user can see exactly which link in the chain (platform / pywin32 / COM
Dispatch / MAPI namespace) is failing. Suggests remediation per failure
mode.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..integrations.outlook_calendar import DiagnosticResult, diagnose


class OutlookDiagnosticDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Diagnose Outlook")
        self.setModal(True)
        self.resize(560, 460)

        layout = QVBoxLayout(self)

        self._headline = QLabel("Running checks...", self)
        self._headline.setWordWrap(True)
        layout.addWidget(self._headline)

        self._steps = QPlainTextEdit(self)
        self._steps.setReadOnly(True)
        layout.addWidget(self._steps, 1)

        button_row = QDialogButtonBox(self)
        self._rerun_btn = QPushButton("Re-run", self)
        self._rerun_btn.clicked.connect(self.run_check)
        button_row.addButton(self._rerun_btn, QDialogButtonBox.ButtonRole.ActionRole)
        button_row.addButton(QDialogButtonBox.StandardButton.Close)
        button_row.rejected.connect(self.reject)
        button_row.accepted.connect(self.accept)
        layout.addWidget(button_row)

        self.run_check()

    def run_check(self) -> None:
        result = diagnose()
        self._headline.setText(self._headline_text(result))
        self._steps.setPlainText(self._steps_text(result))

    @staticmethod
    def _headline_text(result: DiagnosticResult) -> str:
        if result.overall_ok:
            return "Outlook integration is working."
        if not result.platform_ok:
            return "Outlook integration is Windows-only -- skipped here."
        if not result.pywin32_ok:
            return "pywin32 is not installed in this Python environment."
        if not result.dispatch_ok:
            return "pywin32 is installed but Outlook itself is unreachable."
        if not result.namespace_ok:
            return "Reached Outlook but the MAPI calendar namespace failed."
        return "Outlook reachable."

    @staticmethod
    def _steps_text(result: DiagnosticResult) -> str:
        def mark(ok: bool) -> str:
            return "[OK] " if ok else "[FAIL] "

        lines: list[str] = []
        lines.append(mark(result.platform_ok) + "Platform is Windows")
        if not result.platform_ok:
            lines.append(
                "      Outlook integration is intentionally disabled on "
                "non-Windows hosts."
            )
            return "\n".join(lines + ["", result.summary()])

        lines.append(mark(result.pywin32_ok) + "pywin32 (win32com.client) importable")
        if not result.pywin32_ok:
            lines.append(f"      {result.pywin32_error}")
            lines.append("")
            lines.append(result.summary())
            return "\n".join(lines)

        lines.append(mark(result.dispatch_ok) + "Outlook.Application Dispatch")
        if not result.dispatch_ok:
            lines.append(f"      {result.dispatch_error}")
            lines.append("")
            lines.append(result.summary())
            return "\n".join(lines)

        lines.append(mark(result.namespace_ok) + "MAPI namespace + calendar folder")
        if not result.namespace_ok:
            lines.append(f"      {result.namespace_error}")
            lines.append("")
            lines.append(result.summary())
            return "\n".join(lines)

        lines.append(f"[OK] Calendar items visible: {result.calendar_count}")
        lines.append("")
        lines.append(result.summary())
        return "\n".join(lines)
