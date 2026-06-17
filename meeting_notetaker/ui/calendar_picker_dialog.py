"""Pick from Calendar dialog -- choose an upcoming Outlook meeting.

Launched from New Session via the "Pick from Calendar..." button. Lists
today's remaining meetings (start time + subject + duration). On accept
the chosen MeetingInfo is exposed via selected_meeting() so the caller
can pre-fill the session title + seed live_notes from the invite.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional


log = logging.getLogger(__name__)

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressDialog,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..integrations.outlook_calendar import (
    MeetingInfo,
    fetch_meeting_by_entry_id,
    remaining_today_cache,
)


class CalendarPickerDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Pick from Calendar")
        self.setModal(True)
        self.resize(560, 360)
        self._meetings: list[MeetingInfo] = []
        self._selected: Optional[MeetingInfo] = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Choose a meeting from today's remaining calendar. The session title, "
            "attendees, and agenda will be pre-filled on the new session.",
            self,
        ))

        self._list = QListWidget(self)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.itemDoubleClicked.connect(lambda _it: self._on_accept())
        layout.addWidget(self._list, 1)

        # Initial status set BEFORE the fetch so the user sees a loading
        # message immediately. The actual fetch is deferred to the next
        # event-loop tick (see __init__ tail) so the dialog gets a chance
        # to paint -- otherwise the synchronous COM query runs on the
        # same call stack as the setText and the user just sees a frozen
        # blank dialog for the duration of the query.
        status_row = QHBoxLayout()
        self._status = QLabel("Loading from Outlook...", self)
        self._status.setWordWrap(True)
        f = QFont(self._status.font())
        f.setItalic(True)
        self._status.setFont(f)
        status_row.addWidget(self._status, 1)
        # Refresh forces a re-fetch even if the cached payload is still
        # within its TTL. Used when the user knows a meeting just
        # landed in Outlook and the picker doesn't show it yet
        # (#102 bug 4).
        self._refresh_btn = QPushButton("Refresh", self)
        self._refresh_btn.setToolTip(
            "Re-fetch the calendar from Outlook now. The list is "
            "cached for 60 s by default -- click Refresh if you "
            "added a meeting that isn't showing."
        )
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        status_row.addWidget(self._refresh_btn)
        layout.addLayout(status_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        self._ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_btn.setEnabled(False)
        self._ok_btn.setText("Use Selected")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._list.itemSelectionChanged.connect(self._on_selection_changed)
        # Defer the blocking Outlook COM query to the next event-loop
        # tick so the dialog (including the "Loading from Outlook..."
        # status label) paints first. Without this, _populate runs on
        # the same call stack as construction and the user stares at a
        # frozen blank dialog for ~1-2 s while COM queries the GAL.
        QTimer.singleShot(0, self._populate)

    # ---- public API --------------------------------------------------------

    def selected_meeting(self) -> Optional[MeetingInfo]:
        return self._selected

    # ---- internal ----------------------------------------------------------

    def _populate(self) -> None:
        # Cache returns within milliseconds when fresh; on a cold miss
        # the light fetch (no Recipients / Body / Attachments) takes
        # under a second on most installs (#102 bug 4). The
        # expensive full record is fetched on accept by entry_id.
        self._refresh_btn.setEnabled(False)
        try:
            self._meetings = remaining_today_cache.get()
        except Exception as exc:
            self._meetings = []
            self._status.setText(f"Could not read Outlook calendar: {exc}")
            self._refresh_btn.setEnabled(True)
            return
        finally:
            self._refresh_btn.setEnabled(True)

        self._list.clear()
        if not self._meetings:
            self._status.setText(
                "No meetings found on your calendar for the rest of today. "
                "Outlook may not be running, or there genuinely are no more "
                "meetings today."
            )
            return

        self._status.clear()
        for m in self._meetings:
            item = QListWidgetItem(_format_meeting_row(m))
            item.setData(Qt.ItemDataRole.UserRole, m.entry_id)
            details = []
            if m.location:
                details.append(f"Location: {m.location}")
            # Attendees are not populated in the light cache; only
            # show them if a full fetch was already done for some
            # reason. Tooltip is best-effort anyway.
            if m.attendees:
                names = ", ".join(a.display for a in m.attendees if a.display)
                if names:
                    details.append(f"Attendees: {names}")
            if details:
                item.setToolTip("\n".join(details))
            self._list.addItem(item)

    def _on_refresh_clicked(self) -> None:
        """Invalidate the cache + re-fetch. Defers the COM call by one
        event-loop tick so the status label paints first, matching
        the initial-load UX."""
        self._status.setText("Refreshing from Outlook...")
        self._refresh_btn.setEnabled(False)
        remaining_today_cache.invalidate()
        QTimer.singleShot(0, self._populate)

    def _on_selection_changed(self) -> None:
        self._ok_btn.setEnabled(bool(self._list.selectedItems()))

    def _on_accept(self) -> None:
        items = self._list.selectedItems()
        if not items:
            return
        entry_id = items[0].data(Qt.ItemDataRole.UserRole)
        # The cache holds the cheap (light) variant: subject + start
        # + end + location, no attendees/body/attachments. Promote
        # the selected meeting to a full record now so the caller's
        # seed_live_notes_from_meeting / pull_attachments_to_temp
        # downstream paths see the data they need (#102 bug 4).
        cached = next(
            (m for m in self._meetings if m.entry_id == entry_id), None
        )
        if not entry_id:
            self._selected = cached
            self.accept()
            return
        # Pass the cached instance times through so the full fetch
        # doesn't accidentally swap the recurring master's Start /
        # End back in for picked occurrences of a recurring series
        # (Aaron's 2026-06-11 follow-up to #102 bug 4). Outlook's
        # GetItemFromID returns the master appointment, not the
        # occurrence, and that flow's downstream session created_at
        # alignment would otherwise land on the first-ever instance.
        instance_start = cached.start_time if cached else None
        instance_end = cached.end_time if cached else None
        # Run the full fetch off the UI thread with a busy spinner so
        # the picker doesn't look frozen during the Recipients walk
        # (#106). Attendee resolution does a GAL lookup per recipient
        # via GetExchangeUser + PropertyAccessor fallbacks; on
        # Exchange Online cached mode that's 1-3 s per attendee. The
        # light record's already in hand, so a fallback to that path
        # is safe if the worker fails for any reason.
        full = self._resolve_with_progress(
            entry_id, instance_start, instance_end,
        )
        # Fall back to the light record if the full re-fetch failed --
        # the caller still gets subject + times which is better than
        # nothing, and the empty attendees/body just means no seeding.
        self._selected = full if full is not None else cached
        self.accept()

    def _resolve_with_progress(
        self,
        entry_id: str,
        instance_start: Optional[datetime],
        instance_end: Optional[datetime],
    ) -> Optional[MeetingInfo]:
        """Run ``fetch_meeting_by_entry_id`` on a worker thread with a
        modal busy-spinner dialog so the user has feedback during
        attendee resolution (#106).

        Pattern mirrors ``AttachmentsTab.add_files`` -> worker +
        QProgressDialog: the dialog has no Cancel button (canceling
        mid-COM-call is messy and the slow phase typically completes
        in 5-30 s), and ``progress.exec()`` blocks until the worker's
        ``finished_with_meeting`` slot calls ``progress.close()``.
        """
        worker = _MeetingResolveWorker(entry_id, instance_start, instance_end)
        progress = QProgressDialog(
            "Resolving meeting attendees...",
            None,    # no cancel button
            0, 0,    # range (0,0) = indeterminate "busy" spinner
            self,
        )
        progress.setWindowTitle("Loading meeting")
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setCancelButton(None)
        # Strip the window-close affordance so the user can't dismiss
        # the spinner mid-resolve and end up in a half-finished state.
        progress.setWindowFlag(
            Qt.WindowType.WindowCloseButtonHint, False,
        )

        result: dict[str, Optional[MeetingInfo]] = {"meeting": None}

        def _on_done(meeting: Optional[MeetingInfo]) -> None:
            result["meeting"] = meeting
            progress.close()
            # Same deleteLater discipline as _AttachmentImportWorker:
            # the slot fires from inside the worker's run() before
            # the OS thread has joined, so deferring cleanup until
            # after progress.exec() returns + an explicit wait() is
            # what keeps Qt from raising "QThread: Destroyed while
            # thread is still running".

        worker.finished_with_meeting.connect(_on_done)
        progress.show()
        worker.start()
        progress.exec()
        worker.wait()
        worker.deleteLater()
        return result["meeting"]


def _format_meeting_row(m: MeetingInfo) -> str:
    try:
        start = m.start_time.strftime("%H:%M")
    except Exception:
        start = "??:??"
    try:
        duration_min = max(
            0, int((m.end_time - m.start_time).total_seconds() // 60)
        )
        dur = f"  ({duration_min} min)" if duration_min else ""
    except Exception:
        dur = ""
    return f"{start}  {m.subject}{dur}"


class _MeetingResolveWorker(QThread):
    """Off-UI-thread wrapper around ``fetch_meeting_by_entry_id`` (#106).

    The full meeting resolve does a Recipients walk + GAL lookup per
    attendee via ``GetExchangeUser`` + ``PropertyAccessor`` fallbacks,
    which is the slow phase the user perceived as a freeze on the
    'Use Selected' click. Running it on a QThread + showing a modal
    spinner is the same fix shape ``AttachmentsTab._AttachmentImport
    Worker`` uses for the Office-conversion path.

    COM lifetime: ``fetch_meeting_by_entry_id`` calls ``Dispatch
    ("Outlook.Application")`` which needs an apartment-threaded COM
    init on the calling thread. The main UI thread has one already;
    a worker thread has to call ``pythoncom.CoInitialize`` itself.
    The init is best-effort -- pythoncom is only present on Windows,
    and on Linux + macOS dev runtimes ``fetch_meeting_by_entry_id``
    returns ``None`` quickly without ever touching COM.
    """

    finished_with_meeting = pyqtSignal(object)

    def __init__(
        self,
        entry_id: str,
        instance_start: Optional[datetime],
        instance_end: Optional[datetime],
    ) -> None:
        super().__init__()
        self.setObjectName("MeetingResolveWorker")
        self._entry_id = entry_id
        self._instance_start = instance_start
        self._instance_end = instance_end

    def run(self) -> None:  # type: ignore[override]
        co_initialized = False
        try:
            import pythoncom  # noqa: PLC0415 -- Windows-only optional dep
            pythoncom.CoInitialize()
            co_initialized = True
        except Exception:
            # pythoncom absent (non-Windows dev runtime) or already
            # initialized in this apartment -- fetch can still run.
            pass
        try:
            meeting = fetch_meeting_by_entry_id(
                self._entry_id,
                instance_start=self._instance_start,
                instance_end=self._instance_end,
            )
        except Exception:
            log.exception(
                "meeting resolve worker failed for %s", self._entry_id,
            )
            meeting = None
        if co_initialized:
            try:
                import pythoncom  # noqa: PLC0415
                pythoncom.CoUninitialize()
            except Exception:
                pass
        self.finished_with_meeting.emit(meeting)
