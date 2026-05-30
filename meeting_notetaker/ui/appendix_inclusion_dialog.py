"""Pre-export checkbox prompt: which Appendix sections to include
in the PDF / full-session export output (#followup).

Six per-section checkboxes under a master "Include Appendix"
toggle. Master OFF -> no appendix in the output at all (the raw
JSON blocks still get stripped from the rendered body so they
don't land verbatim). Master ON + section checkboxes selected ->
those sections are rendered; un-selected ones are filtered out
before ``inject_appendix`` runs.

Sections with zero entries in the live AppendixData are shown as
disabled checkboxes labelled "(empty)" so the user understands
why selecting them would add nothing.

Default: every checkbox the live data populates is ON.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ..utils.appendix_transform import AppendixData


@dataclass
class AppendixInclusion:
    """User's per-section export choice. ``include_appendix`` is
    the master toggle -- when False, every section is treated as
    excluded regardless of the individual flags."""
    include_appendix: bool = True
    include_attendee_context: bool = True
    include_attendee_details: bool = True
    include_topics: bool = True
    include_referenced_attachments: bool = True
    include_session_attachments: bool = True
    include_links: bool = True

    @classmethod
    def all_on(cls) -> "AppendixInclusion":
        return cls()

    @classmethod
    def all_off(cls) -> "AppendixInclusion":
        return cls(
            include_appendix=False,
            include_attendee_context=False,
            include_attendee_details=False,
            include_topics=False,
            include_referenced_attachments=False,
            include_session_attachments=False,
            include_links=False,
        )


def apply_inclusion(
    data: AppendixData, inclusion: AppendixInclusion,
) -> AppendixData:
    """Return a copy of ``data`` with excluded sections zeroed out.

    The master toggle short-circuits -- when off, every list is
    empty so ``build_appendix_markdown`` returns "" and the
    inject_appendix path skips appending a rendered section.
    """
    if not inclusion.include_appendix:
        return AppendixData(
            attendee_context=[],
            attendee_details=[],
            topics=[],
            referenced_attachments=[],
            session_attachments=[],
            links=[],
        )
    return AppendixData(
        attendee_context=(
            data.attendee_context if inclusion.include_attendee_context else []
        ),
        attendee_details=(
            data.attendee_details if inclusion.include_attendee_details else []
        ),
        topics=(data.topics if inclusion.include_topics else []),
        referenced_attachments=(
            data.referenced_attachments
            if inclusion.include_referenced_attachments
            else []
        ),
        session_attachments=(
            data.session_attachments
            if inclusion.include_session_attachments
            else []
        ),
        links=(data.links if inclusion.include_links else []),
    )


class AppendixInclusionDialog(QDialog):
    """Checkbox dialog letting the user pick which Appendix
    sub-sections to include in an export."""

    def __init__(
        self,
        data: AppendixData,
        *,
        export_label: str = "export",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Appendix sections to include")
        self.setModal(True)
        self.resize(420, 380)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        blurb = QLabel(
            f"Pick the Appendix sections to include in this {export_label}. "
            "Sections that have no entries for this session are "
            "disabled.",
            self,
        )
        blurb.setWordWrap(True)
        outer.addWidget(blurb)

        self._master = QCheckBox("Include Appendix", self)
        self._master.setChecked(True)
        font = self._master.font()
        font.setBold(True)
        self._master.setFont(font)
        self._master.toggled.connect(self._on_master_toggled)
        outer.addWidget(self._master)

        self._sections: dict[str, QCheckBox] = {}
        # Section key, label, current count from the live data.
        section_specs = [
            ("attendee_context",       "Attendee Context",       len(data.attendee_context)),
            ("attendee_details",       "Attendee Details",       len(data.attendee_details)),
            ("topics",                 "Suggested Topics",       len(data.topics)),
            ("referenced_attachments", "Referenced Attachments", len(data.referenced_attachments)),
            ("session_attachments",    "Session Attachments",    len(data.session_attachments)),
            ("links",                  "Links",                  len(data.links)),
        ]
        for key, label, count in section_specs:
            display = (
                f"    {label} ({count})" if count
                else f"    {label} (empty)"
            )
            cb = QCheckBox(display, self)
            cb.setChecked(count > 0)
            cb.setEnabled(count > 0)
            outer.addWidget(cb)
            self._sections[key] = cb

        outer.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def inclusion(self) -> AppendixInclusion:
        """Return the user's selection as a typed payload."""
        if not self._master.isChecked():
            return AppendixInclusion.all_off()
        return AppendixInclusion(
            include_appendix=True,
            include_attendee_context=self._sections["attendee_context"].isChecked(),
            include_attendee_details=self._sections["attendee_details"].isChecked(),
            include_topics=self._sections["topics"].isChecked(),
            include_referenced_attachments=self._sections["referenced_attachments"].isChecked(),
            include_session_attachments=self._sections["session_attachments"].isChecked(),
            include_links=self._sections["links"].isChecked(),
        )

    def _on_master_toggled(self, checked: bool) -> None:
        # Unchecking master visually disables (and unchecks) every
        # populated sub-section so it's obvious the export will
        # have no appendix at all. Re-checking master restores the
        # populated sub-sections.
        for key, cb in self._sections.items():
            if checked:
                # Only re-enable + check sections that actually
                # have content; "empty" rows stay disabled.
                if cb.text().endswith("(empty)"):
                    continue
                cb.setEnabled(True)
                cb.setChecked(True)
            else:
                cb.setChecked(False)
                cb.setEnabled(False)
