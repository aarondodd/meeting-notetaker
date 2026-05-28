"""Address Book dialog -- the unified Contact catalog editor.

File > Address Book... opens this. Two-pane layout:

* **Left pane**: filterable list of Contacts. Each row shows
  display_name + alias count + session count + source-of-record
  badge (People / Speakers / Both / Calendar-only).
* **Right pane**: details for the selected Contact -- display_name
  (editable via Rename...), notes, aliases table with kind +
  source + remove. Add alias inline (with kind picker).

Actions on the selected Contact:
* **Rename canonical...** -- changes display_name (the old form
  stays as an alias so transcripts still resolve).
* **Merge into...** -- picks a target Contact; every alias +
  session link moves to the target; source is deleted.
* **Delete** -- drops the Contact + all its aliases / session
  links. Speaker-store rows that linked here become unlinked.

Bottom of the left pane:
* **Suggested merges** expander: scan results from
  `suggest_contact_merges` -- pairs of Contacts that look like
  duplicates (shared alias, name-token containment, low edit
  distance). One-click "Merge X into Y" per row.
* **Delete N orphans...** -- bulk cleanup of Contacts with zero
  session_contacts rows.

The dialog reads and writes the classification store directly
(no separate apply step). On close, MainApp refreshes the
session list + chips bar so any merge / rename / delete
propagates to the visible UI.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..models.classification import (
    ALIAS_KIND_EMAIL,
    ALIAS_KIND_INITIAL,
    ALIAS_KIND_NAME,
    ALIAS_KIND_SHORT,
    ALL_ALIAS_KINDS,
    ClassificationStore,
    Contact,
    ENRICH_SOURCE_LLM,
    ENRICH_SOURCE_MANUAL,
    ENRICH_SOURCE_OUTLOOK,
    SOURCE_DIARIZATION,
    SOURCE_MANUAL,
)
from ..utils.contact_resolution import suggest_contact_merges


_KIND_LABELS = {
    ALIAS_KIND_NAME: "name",
    ALIAS_KIND_SHORT: "short",
    ALIAS_KIND_INITIAL: "initial",
    ALIAS_KIND_EMAIL: "email",
}


# Source emoji map for the rich-Contact-fields header badge (issue #51).
# Aaron's spec was "unobtrusive single-character indicators"; one emoji
# per known source value, blank when no enrichment has touched the
# Contact yet. The badge sits next to the contact's display name in
# the right-pane header so the user can see at a glance whether
# manual edits, an Outlook pull, or an LLM extraction was the most
# recent touch.
_SOURCE_EMOJI = {
    ENRICH_SOURCE_OUTLOOK: "\U0001F4E7",  # envelope (Outlook)
    ENRICH_SOURCE_LLM: "\U0001F916",      # robot (LLM)
    ENRICH_SOURCE_MANUAL: "✏️",  # pencil (manual edit)
}
_SOURCE_LABELS = {
    ENRICH_SOURCE_OUTLOOK: "Outlook calendar",
    ENRICH_SOURCE_LLM: "auto-extracted from synthesis",
    ENRICH_SOURCE_MANUAL: "manually edited",
}


class AddressBookDialog(QDialog):
    """The unified Contact catalog."""

    def __init__(
        self,
        store: ClassificationStore,
        parent: Optional[QWidget] = None,
        *,
        speaker_store=None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._speaker_store = speaker_store
        self.setWindowTitle("Address Book")
        self.resize(900, 600)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        blurb = QLabel(
            "Contacts are the unified identity behind Session People and "
            "Speakers. Aliases let typing 'BS' resolve to Bob Smith "
            "automatically and let voice recognition cross-link the "
            "same person across meetings.",
            self,
        )
        blurb.setWordWrap(True)
        outer.addWidget(blurb)

        # Filter input above the splitter.
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter:", self))
        self._filter_input = QLineEdit(self)
        self._filter_input.setPlaceholderText(
            "Type to filter by display name or alias..."
        )
        self._filter_input.textChanged.connect(self._refresh_list)
        filter_row.addWidget(self._filter_input, 1)
        outer.addLayout(filter_row)

        # Splitter: list | details.
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        outer.addWidget(splitter, 1)

        # ---- left pane ----
        left = QWidget(splitter)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)

        self._list = QListWidget(left)
        self._list.itemSelectionChanged.connect(self._refresh_details)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        left_layout.addWidget(self._list, 1)

        # Suggested merges section.
        self._suggestions_group = QGroupBox("Suggested merges", left)
        sug_layout = QVBoxLayout(self._suggestions_group)
        sug_layout.setContentsMargins(6, 4, 6, 4)
        self._suggestions_list = QListWidget(self._suggestions_group)
        self._suggestions_list.setMaximumHeight(140)
        self._suggestions_list.itemDoubleClicked.connect(
            self._on_suggestion_activated,
        )
        sug_layout.addWidget(self._suggestions_list)
        sug_button_row = QHBoxLayout()
        sug_button_row.addStretch(1)
        self._sug_merge_btn = QPushButton("Merge selected pair...", left)
        self._sug_merge_btn.clicked.connect(self._on_merge_suggestion_clicked)
        sug_button_row.addWidget(self._sug_merge_btn)
        sug_layout.addLayout(sug_button_row)
        left_layout.addWidget(self._suggestions_group)

        # Bulk cleanup row.
        cleanup_row = QHBoxLayout()
        self._orphan_label = QLabel("", left)
        cleanup_row.addWidget(self._orphan_label, 1)
        self._delete_orphans_btn = QPushButton("Delete orphans...", left)
        self._delete_orphans_btn.setToolTip(
            "Drop every Contact with zero session associations. "
            "Aliases + speaker-store links are cleared as well."
        )
        self._delete_orphans_btn.clicked.connect(self._on_delete_orphans)
        cleanup_row.addWidget(self._delete_orphans_btn)
        left_layout.addLayout(cleanup_row)

        splitter.addWidget(left)

        # ---- right pane: contact details ----
        right = QWidget(splitter)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(4, 0, 0, 0)
        right_layout.setSpacing(8)

        header_row = QHBoxLayout()
        self._detail_name = QLabel("(select a contact)", right)
        font = QFont(self._detail_name.font())
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        self._detail_name.setFont(font)
        header_row.addWidget(self._detail_name, 1)
        self._rename_btn = QPushButton("Rename...", right)
        self._rename_btn.clicked.connect(self._on_rename_contact)
        header_row.addWidget(self._rename_btn)
        self._merge_btn = QPushButton("Merge into...", right)
        self._merge_btn.clicked.connect(self._on_merge_contact)
        header_row.addWidget(self._merge_btn)
        self._delete_btn = QPushButton("Delete", right)
        self._delete_btn.clicked.connect(self._on_delete_contact)
        header_row.addWidget(self._delete_btn)
        right_layout.addLayout(header_row)

        self._detail_meta = QLabel("", right)
        self._detail_meta.setStyleSheet("color: gray;")
        right_layout.addWidget(self._detail_meta)

        # Rich addressbook fields (issue #51 Phase 1). Each input
        # saves on focus-loss via editingFinished -- no Save button,
        # the dialog has no concept of "dirty state" for these. The
        # whole form is gated on contact selection; _refresh_details
        # enables/disables.
        self._fields_box = QGroupBox("Details", right)
        fields_layout = QFormLayout(self._fields_box)
        fields_layout.setContentsMargins(8, 8, 8, 8)
        fields_layout.setVerticalSpacing(4)
        self._title_input = QLineEdit(right)
        self._title_input.setPlaceholderText("e.g. VP of Engineering")
        self._title_input.editingFinished.connect(
            lambda: self._on_field_edited("title", self._title_input.text())
        )
        fields_layout.addRow("Title:", self._title_input)
        self._company_input = QLineEdit(right)
        self._company_input.setPlaceholderText("e.g. Acme Corp")
        self._company_input.editingFinished.connect(
            lambda: self._on_field_edited("company", self._company_input.text())
        )
        fields_layout.addRow("Company:", self._company_input)
        self._department_input = QLineEdit(right)
        self._department_input.setPlaceholderText("e.g. Engineering")
        self._department_input.editingFinished.connect(
            lambda: self._on_field_edited(
                "department", self._department_input.text(),
            )
        )
        fields_layout.addRow("Department:", self._department_input)
        self._email_input = QLineEdit(right)
        self._email_input.setPlaceholderText("e.g. vp@acme.com (primary)")
        self._email_input.editingFinished.connect(
            lambda: self._on_field_edited(
                "primary_email", self._email_input.text(),
            )
        )
        fields_layout.addRow("Primary email:", self._email_input)
        self._phone_input = QLineEdit(right)
        self._phone_input.setPlaceholderText("e.g. +1-555-0100")
        self._phone_input.editingFinished.connect(
            lambda: self._on_field_edited("phone", self._phone_input.text())
        )
        fields_layout.addRow("Phone:", self._phone_input)
        self._notes_input = QPlainTextEdit(right)
        self._notes_input.setPlaceholderText(
            "Free-form notes (preferences, context, anything)"
        )
        self._notes_input.setFixedHeight(64)
        # QPlainTextEdit has no editingFinished signal; commit on
        # focus-out via the focusOutEvent override-equivalent: connect
        # to the textChanged signal but debounce via a timer.
        self._notes_input.installEventFilter(self)
        fields_layout.addRow("Notes:", self._notes_input)
        right_layout.addWidget(self._fields_box)

        right_layout.addWidget(QLabel("Aliases:", right))
        self._alias_table = QTableWidget(right)
        self._alias_table.setColumnCount(4)
        self._alias_table.setHorizontalHeaderLabels([
            "Alias", "Kind", "Source", "",
        ])
        self._alias_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch,
        )
        self._alias_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents,
        )
        self._alias_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents,
        )
        self._alias_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents,
        )
        self._alias_table.verticalHeader().setVisible(False)
        self._alias_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers,
        )
        right_layout.addWidget(self._alias_table, 1)

        add_alias_row = QHBoxLayout()
        add_alias_row.addWidget(QLabel("Add alias:", right))
        self._alias_input = QLineEdit(right)
        self._alias_input.setPlaceholderText("e.g. BS / bsmith@corp.com")
        add_alias_row.addWidget(self._alias_input, 1)
        self._alias_kind_combo = QComboBox(right)
        for kind in ALL_ALIAS_KINDS:
            self._alias_kind_combo.addItem(_KIND_LABELS[kind], kind)
        add_alias_row.addWidget(self._alias_kind_combo)
        self._add_alias_btn = QPushButton("Add", right)
        self._add_alias_btn.clicked.connect(self._on_add_alias)
        add_alias_row.addWidget(self._add_alias_btn)
        right_layout.addLayout(add_alias_row)

        splitter.addWidget(right)
        splitter.setSizes([320, 580])

        button_row = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Close, self,
        )
        button_row.rejected.connect(self.reject)
        button_row.accepted.connect(self.accept)
        outer.addWidget(button_row)

        self._refresh_list()
        self._refresh_suggestions()

    # ---- public API ----
    def select_contact(self, contact_id: int) -> None:
        """Select the row for ``contact_id`` if present in the list.

        Used by the attendee-drawer click path (issue #51 Phase 3)
        to open the dialog already focused on a specific contact.
        No-op when the contact is filtered out or doesn't exist.
        """
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item is None:
                continue
            data = item.data(Qt.ItemDataRole.UserRole)
            if data is not None and int(data) == int(contact_id):
                self._list.setCurrentRow(i)
                return

    # ---- list refresh / filter ----
    def _refresh_list(self) -> None:
        filt = self._filter_input.text().strip().casefold()
        contacts = self._store.list_contacts()
        prior_selected_id = self._selected_contact_id()
        self._list.clear()
        for c in contacts:
            if filt and not self._contact_matches_filter(c, filt):
                continue
            n_sessions = self._store.session_count_for_contact(c.id)
            aliases = self._store.list_aliases(c.id)
            badge = self._source_badge(c, n_sessions, aliases)
            label = (
                f"{c.display_name}  -  "
                f"{len(aliases)} alias(es), {n_sessions} session(s) {badge}"
            )
            item = QListWidgetItem(label, self._list)
            item.setData(Qt.ItemDataRole.UserRole, c.id)
            if prior_selected_id == c.id:
                self._list.setCurrentItem(item)
        self._refresh_orphan_label(contacts)
        self._refresh_details()
        self._refresh_suggestions()

    def _refresh_orphan_label(self, contacts: list[Contact]) -> None:
        orphans = [
            c for c in contacts
            if self._store.session_count_for_contact(c.id) == 0
        ]
        if orphans:
            self._orphan_label.setText(f"{len(orphans)} orphan(s)")
            self._delete_orphans_btn.setEnabled(True)
        else:
            self._orphan_label.setText("No orphans.")
            self._delete_orphans_btn.setEnabled(False)

    def _contact_matches_filter(self, contact: Contact, filt: str) -> bool:
        if filt in contact.display_name.casefold():
            return True
        for a in self._store.list_aliases(contact.id):
            if filt in a.alias.casefold():
                return True
        return False

    def _source_badge(
        self,
        contact: Contact,
        session_count: int,
        aliases: list,
    ) -> str:
        sources = {a.source for a in aliases}
        has_speaker = self._has_speaker_link(contact.id)
        has_people = session_count > 0
        if has_speaker and has_people:
            return "[Both]"
        if has_speaker:
            return "[Speakers]"
        if has_people:
            return "[People]"
        if "calendar" in sources:
            return "[Calendar-only]"
        return ""

    def _has_speaker_link(self, contact_id: int) -> bool:
        if self._speaker_store is None:
            return False
        try:
            for rec in self._speaker_store.list_all():
                if rec.contact_id == contact_id:
                    return True
        except Exception:
            return False
        return False

    # ---- selection ----
    def _selected_contact_id(self) -> Optional[int]:
        item = self._list.currentItem()
        if item is None:
            return None
        data = item.data(Qt.ItemDataRole.UserRole)
        return int(data) if data is not None else None

    def _selected_contact(self) -> Optional[Contact]:
        cid = self._selected_contact_id()
        return self._store.get_contact(cid) if cid is not None else None

    # ---- rich fields (issue #51 Phase 1) ----
    def _populate_field_inputs(self, contact: Optional[Contact]) -> None:
        """Push a Contact's rich-field values into the form inputs.

        Signals are blocked while we set values so the editingFinished
        handlers don't fire (we'd be saving the value we just read).
        Called from _refresh_details whenever the selection changes.
        """
        widgets_text = (
            (self._title_input, contact.title if contact else None),
            (self._company_input, contact.company if contact else None),
            (self._department_input, contact.department if contact else None),
            (self._email_input, contact.primary_email if contact else None),
            (self._phone_input, contact.phone if contact else None),
        )
        for widget, value in widgets_text:
            widget.blockSignals(True)
            try:
                widget.setText(value or "")
            finally:
                widget.blockSignals(False)
        self._notes_input.blockSignals(True)
        try:
            self._notes_input.setPlainText((contact.notes if contact else "") or "")
        finally:
            self._notes_input.blockSignals(False)
        # Stash the as-loaded notes value so the focusOut commit can
        # tell whether the user actually changed it.
        self._notes_input_loaded_value = (contact.notes if contact else "") or ""

    def _on_field_edited(self, field: str, value: str) -> None:
        """Persist a single rich-field edit. Source = manual.

        The user typing in the form is explicit + authoritative;
        fill_empty_only stays False so empty values overwrite (the
        user can clear a field by selecting + deleting).
        """
        contact = self._selected_contact()
        if contact is None:
            return
        # Strip leading/trailing whitespace at save time so the DB
        # never gets " VP " when the user typed it.
        cleaned = value.strip()
        # Map empty string back to NULL in the DB -- the field is
        # nullable + the enrichment paths' "fill empty only" logic
        # treats NULL + "" identically, so storing NULL is the
        # cleaner representation of "no value".
        store_value = cleaned if cleaned else None
        self._store.update_contact_fields(
            contact.id,
            **{field: store_value},
            source=ENRICH_SOURCE_MANUAL,
        )
        # Refresh the header badge if this was the first manual touch.
        if contact.last_enriched_source != ENRICH_SOURCE_MANUAL:
            self._refresh_details()

    def eventFilter(self, obj, event):
        """Commit notes-field edits on focus-out.

        QPlainTextEdit has no editingFinished signal; we install
        ourselves as an event filter to catch FocusOut on the notes
        input and persist the change if the text differs from what
        we loaded.
        """
        if obj is self._notes_input and event.type() == event.Type.FocusOut:
            current = self._notes_input.toPlainText()
            if current != getattr(self, "_notes_input_loaded_value", None):
                self._on_field_edited("notes", current)
                self._notes_input_loaded_value = current
        return super().eventFilter(obj, event)

    # ---- details pane ----
    def _refresh_details(self) -> None:
        contact = self._selected_contact()
        enabled = contact is not None
        for w in (
            self._rename_btn, self._merge_btn, self._delete_btn,
            self._alias_input, self._alias_kind_combo, self._add_alias_btn,
            self._title_input, self._company_input, self._department_input,
            self._email_input, self._phone_input, self._notes_input,
        ):
            w.setEnabled(enabled)
        if contact is None:
            self._detail_name.setText("(select a contact)")
            self._detail_meta.setText("")
            self._alias_table.setRowCount(0)
            self._populate_field_inputs(None)
            return
        # Header line: contact name + source emoji when enriched.
        # The emoji is the badge Aaron asked for in his 2026-05-28
        # spec -- envelope for Outlook, robot for LLM, pencil for
        # manual edits. Tooltip on the name explains.
        badge = _SOURCE_EMOJI.get(contact.last_enriched_source or "", "")
        self._detail_name.setText(
            f"{contact.display_name} {badge}".strip()
        )
        if badge:
            self._detail_name.setToolTip(
                f"Last touched: {_SOURCE_LABELS.get(contact.last_enriched_source, '')}"
            )
        else:
            self._detail_name.setToolTip("")
        sessions = self._store.session_count_for_contact(contact.id)
        speaker_status = (
            "linked to a Speaker"
            if self._has_speaker_link(contact.id) else "no Speaker link"
        )
        self._detail_meta.setText(
            f"{sessions} session(s); {speaker_status}; "
            f"created {contact.created_at[:10]}"
        )
        self._populate_field_inputs(contact)
        aliases = self._store.list_aliases(contact.id)
        self._alias_table.setRowCount(len(aliases))
        for i, a in enumerate(aliases):
            self._alias_table.setItem(
                i, 0, QTableWidgetItem(a.alias),
            )
            self._alias_table.setItem(
                i, 1, QTableWidgetItem(_KIND_LABELS.get(a.kind, a.kind)),
            )
            self._alias_table.setItem(
                i, 2, QTableWidgetItem(a.source),
            )
            remove_btn = QPushButton("Remove", self)
            alias_id = a.id
            remove_btn.clicked.connect(
                lambda _checked=False, aid=alias_id: self._on_remove_alias(aid)
            )
            self._alias_table.setCellWidget(i, 3, remove_btn)

    # ---- contact mutations ----
    def _on_rename_contact(self) -> None:
        contact = self._selected_contact()
        if contact is None:
            return
        new_name, ok = QInputDialog.getText(
            self, "Rename contact",
            "New display name (old form stays as an alias):",
            QLineEdit.EchoMode.Normal,
            contact.display_name,
        )
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == contact.display_name:
            return
        try:
            self._store.rename_contact(contact.id, new_name)
        except Exception as exc:
            QMessageBox.warning(self, "Rename contact", str(exc))
            return
        self._refresh_list()

    def _on_merge_contact(self) -> None:
        source = self._selected_contact()
        if source is None:
            return
        candidates = [
            c for c in self._store.list_contacts() if c.id != source.id
        ]
        if not candidates:
            QMessageBox.information(
                self, "Merge contact",
                "No other contacts to merge into.",
            )
            return
        names = sorted(c.display_name for c in candidates)
        target_name, ok = QInputDialog.getItem(
            self, "Merge contact",
            f"Move all aliases and session links from "
            f"'{source.display_name}' into:",
            names, 0, False,
        )
        if not ok:
            return
        target = next(
            (c for c in candidates if c.display_name == target_name), None,
        )
        if target is None:
            return
        confirm = QMessageBox.question(
            self, "Merge contact",
            f"Merge '{source.display_name}' into '{target.display_name}'? "
            "Source is deleted; its aliases and session links move to "
            "the target.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            self._store.merge_contacts(source.id, target.id)
        except Exception as exc:
            QMessageBox.warning(self, "Merge contact", str(exc))
            return
        # If the speaker store is wired, retarget any speaker that
        # pointed at the source.
        if self._speaker_store is not None:
            try:
                for rec in self._speaker_store.list_all():
                    if rec.contact_id == source.id:
                        self._speaker_store.set_contact_id(rec.name, target.id)
            except Exception:
                pass
        self._refresh_list()

    def _on_delete_contact(self) -> None:
        contact = self._selected_contact()
        if contact is None:
            return
        sessions = self._store.session_count_for_contact(contact.id)
        message = f"Delete contact '{contact.display_name}'?"
        if sessions:
            message += (
                f"\n\n{sessions} session(s) currently associate this "
                "contact; they'll have the person dropped from their "
                "People list. Transcripts / notes / audio are not "
                "affected."
            )
        if self._has_speaker_link(contact.id):
            message += (
                "\n\nA Speaker record is also linked; the speaker will "
                "become unlinked but its voice samples are preserved."
            )
        confirm = QMessageBox.question(
            self, "Delete contact",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        # Unlink any speaker pointing here BEFORE delete (so the
        # speaker keeps its row -- just becomes unlinked).
        if self._speaker_store is not None:
            try:
                for rec in self._speaker_store.list_all():
                    if rec.contact_id == contact.id:
                        self._speaker_store.set_contact_id(rec.name, None)
            except Exception:
                pass
        try:
            self._store.delete_contact(contact.id)
        except Exception as exc:
            QMessageBox.warning(self, "Delete contact", str(exc))
            return
        self._refresh_list()

    def _on_add_alias(self) -> None:
        contact = self._selected_contact()
        if contact is None:
            return
        text = self._alias_input.text().strip()
        if not text:
            return
        kind = self._alias_kind_combo.currentData()
        try:
            self._store.add_alias(
                contact.id, text, kind=kind, source=SOURCE_MANUAL,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Add alias", str(exc))
            return
        self._alias_input.clear()
        self._refresh_details()
        self._refresh_suggestions()

    def _on_remove_alias(self, alias_id: int) -> None:
        try:
            self._store.remove_alias(alias_id)
        except Exception as exc:
            QMessageBox.warning(self, "Remove alias", str(exc))
            return
        self._refresh_details()
        self._refresh_suggestions()

    # ---- orphans + suggestions ----
    def _on_delete_orphans(self) -> None:
        orphans = self._store.list_orphan_contacts()
        if not orphans:
            return
        confirm = QMessageBox.question(
            self, "Delete orphans",
            f"Delete {len(orphans)} Contact(s) with zero session "
            "associations? Aliases + Speaker links are cleared.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        # Unlink any speaker pointing at an about-to-be-deleted orphan.
        if self._speaker_store is not None:
            try:
                orphan_ids = {c.id for c in orphans}
                for rec in self._speaker_store.list_all():
                    if rec.contact_id in orphan_ids:
                        self._speaker_store.set_contact_id(rec.name, None)
            except Exception:
                pass
        self._store.delete_orphan_contacts()
        self._refresh_list()

    def _refresh_suggestions(self) -> None:
        self._suggestions_list.clear()
        try:
            suggestions = suggest_contact_merges(self._store, max_suggestions=20)
        except Exception:
            suggestions = []
        if not suggestions:
            self._suggestions_group.setVisible(False)
            return
        self._suggestions_group.setVisible(True)
        for s in suggestions:
            label = (
                f"{s.source.display_name}  ⇄  {s.target.display_name}"
                f"   ({s.reason})"
            )
            item = QListWidgetItem(label, self._suggestions_list)
            item.setData(Qt.ItemDataRole.UserRole, (s.source.id, s.target.id))

    def _on_suggestion_activated(self, item: QListWidgetItem) -> None:
        # Double-click on a suggestion -> select the source contact
        # in the main list so the user can review it before merging.
        data = item.data(Qt.ItemDataRole.UserRole)
        if not data:
            return
        source_id = int(data[0])
        for i in range(self._list.count()):
            if self._list.item(i).data(Qt.ItemDataRole.UserRole) == source_id:
                self._list.setCurrentRow(i)
                break

    def _on_merge_suggestion_clicked(self) -> None:
        item = self._suggestions_list.currentItem()
        if item is None:
            QMessageBox.information(
                self, "Merge suggestion",
                "Pick a suggested pair first.",
            )
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        if not data:
            return
        source_id, target_id = int(data[0]), int(data[1])
        source = self._store.get_contact(source_id)
        target = self._store.get_contact(target_id)
        if source is None or target is None:
            self._refresh_list()
            return
        confirm = QMessageBox.question(
            self, "Merge contact pair",
            f"Merge '{source.display_name}' into '{target.display_name}'? "
            "Source is deleted; its aliases and session links move to "
            "the target.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            self._store.merge_contacts(source_id, target_id)
        except Exception as exc:
            QMessageBox.warning(self, "Merge contact pair", str(exc))
            return
        if self._speaker_store is not None:
            try:
                for rec in self._speaker_store.list_all():
                    if rec.contact_id == source_id:
                        self._speaker_store.set_contact_id(rec.name, target_id)
            except Exception:
                pass
        self._refresh_list()
