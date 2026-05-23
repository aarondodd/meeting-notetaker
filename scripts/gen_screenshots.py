"""Regenerate every screenshot under docs/screenshots/ with generic content.

Usage:
    DISPLAY=:10 .venv/bin/python scripts/gen_screenshots.py
    # or, with xvfb:
    xvfb-run -a -s "-screen 0 1600x1000x24" .venv/bin/python scripts/gen_screenshots.py

All sample meeting content is deliberately fictional and generic
(Alex Chen / Sam Patel / Jordan Lee, "Q3 Platform Sync" -- no real
people, projects, or ticket IDs). When updating, keep it that way.

Covers 01-14: main window across its four tabs, all dialogs (new
session, settings, generate prompt, paste response, calendar pick,
new-session calendar prefill), and the v0.5 speaker walker / manage
dialogs.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def _ensure_qt_platform() -> None:
    if not os.environ.get("DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


_ensure_qt_platform()

# Force the app's data dir into a tempdir BEFORE any module imports
# that resolve paths at import time. The session view, transcript
# store, and prompts all use paths.app_data_dir() under the hood.
_TMPDATA = Path(tempfile.mkdtemp(prefix="mn-screenshots-"))
os.environ["MEETING_NOTETAKER_DATA_DIR"] = str(_TMPDATA)

from PyQt6.QtCore import QCoreApplication, Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker import integrations  # noqa: E402
from meeting_notetaker.diarization.store import SpeakerStore  # noqa: E402
from meeting_notetaker.integrations import outlook_calendar  # noqa: E402
from meeting_notetaker.models.session import (  # noqa: E402
    STATE_COMPLETE,
    Session,
)
from meeting_notetaker.models.transcript import TranscriptStore  # noqa: E402
from meeting_notetaker.ui.calendar_picker_dialog import (  # noqa: E402
    CalendarPickerDialog,
)
from meeting_notetaker.ui.main_window import MainWindow  # noqa: E402
from meeting_notetaker.ui.new_session_dialog import NewSessionDialog  # noqa: E402
from meeting_notetaker.ui.prompt_dialog import (  # noqa: E402
    GeneratePromptDialog,
    PasteNotesDialog,
)
from meeting_notetaker.ui.settings_dialog import SettingsDialog  # noqa: E402
from meeting_notetaker.ui.speaker_walker_dialog import (  # noqa: E402
    SpeakerWalkerDialog,
    SpeakerWalkerEntry,
)
from meeting_notetaker.ui.speakers_manage_dialog import (  # noqa: E402
    SpeakersManageDialog,
)
from meeting_notetaker.utils import prompts as prompts_mod  # noqa: E402
from meeting_notetaker.utils.config import Config  # noqa: E402
from meeting_notetaker.version import __version__  # noqa: E402


OUT_DIR = _HERE / "docs" / "screenshots"

# Generic cast + meeting context used across all screenshots. Keep this
# table the only source of identity data; do not inline anything else.
USER_NAME = "Alex Chen"
OTHER_ATTENDEES = ["Sam Patel", "Jordan Lee"]
ATTENDEES = [USER_NAME] + OTHER_ATTENDEES
SESSION_TITLE = "Q3 Platform Sync"
SESSION_ID = "screenshot-session-0001"
SESSION_DATE = datetime(2026, 5, 17, 14, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Sample artifacts: transcript, live notes, synthesized notes
# ---------------------------------------------------------------------------

SAMPLE_TRANSCRIPT = """\
[00:00:03] Alex Chen: hey, thanks for joining. We've got about 30 minutes to walk through the rollout plan.
[00:00:11] Them: sounds good. I had a quick read through your draft last night.
[00:00:18] Them: my main open question is whether the new service lands behind the existing feature gate or gets its own.
[00:00:34] Alex Chen: right. The audit-trail piece is what will be hardest -- they want to see who ran what against which environment.
[00:00:50] Them: yeah, we already have that for the legacy job runner. Same shape should work here.
[00:01:08] Alex Chen: ok, let me draft an addendum on the audit path and share it on the rollout doc.
[00:01:21] Them: perfect. One more thing -- do we genuinely need the parallel-approval track for the read-only routes? Six weeks of staffing time if we do.
[00:01:38] Alex Chen: if we wait for the security review on the writes before starting reads, we lose six to eight weeks of analyst time.
[00:01:50] Them: fair. I'll raise it with leadership Friday.
"""

SAMPLE_LIVE_NOTES = """\
# Attendees
- Alex Chen
- Sam Patel
- Jordan Lee

# Agenda
- Q3 rollout plan walkthrough
- Security review handoff
- Parallel-approval track for read-only routes

# Notes
- Security-first routing is the consensus path. Sam and Jordan are already evaluating it for the queue.
- Auditability is the biggest open item. Need addendum covering both the audit log table and the warehouse gap analysis.
- Parallel approval for read-only is a hard requirement -- six to eight week analyst-time impact if we wait behind the writes.

# Action Items
- Alex: draft auditability addendum on the rollout doc by Friday
- Jordan: surface the parallel-approval ask to leadership Friday
"""

SAMPLE_SYNTHESIS = """\
# Meeting Notes -- Q3 Platform Sync

## Attendees
- Alex Chen
- Sam Patel
- Jordan Lee

## Decisions
- Route the new service through the security review path first, then back to the working group. Rationale: the team is mid-evaluation on the read tier, so bundling the two reviews keeps the queue light.
- The read-only routes get a parallel approval track so analyst tooling does not slip six to eight weeks.

## Discussion summary
The team aligned quickly on security-first routing. Sam confirmed the security review queue already has the read tier in flight, so adding the new service on the same workstream is the lowest-overhead option. The hardest open item is auditability -- the working group will want a clear answer on who ran what against which environment. The warehouse gap analysis is less clear and needs more digging.

## Action items
- [ ] Alex: draft auditability addendum on the rollout doc (Friday)
- [ ] Jordan: surface the parallel-approval ask to leadership (Friday)
- [ ] Alex: follow up on the warehouse audit-log gap analysis (next sprint)

## Risks / open questions
- Warehouse-side audit logging coverage is not yet enumerated.
- Parallel-approval for the read tier depends on leadership signoff; if denied, we lose the analyst-time argument.
"""

SAMPLE_PASTE_BODY = """\
# Q3 Platform Sync -- Sync Notes

**Date:** 2026-05-17
**Attendees:** Alex Chen, Sam Patel, Jordan Lee

## Decisions

- Soft launch end of July, full cutover first week of August
- 2x peak load test scheduled for week of July 18

## Action Items

- [ ] Alex -- lock launch date with leadership (Tuesday)
- [ ] Sam -- read-replica staged by July 15
- [ ] Jordan -- on-call rotation proposal by Friday
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unit_vec(*c: float) -> np.ndarray:
    arr = np.asarray(c, dtype=np.float32)
    return arr / max(np.linalg.norm(arr), 1e-8)


def _grab(widget, name: str, *, autosize: bool = True) -> None:
    if autosize:
        widget.adjustSize()
    QApplication.processEvents()
    pixmap = widget.grab()
    out = OUT_DIR / name
    pixmap.save(str(out), "PNG")
    print(f"wrote {out}  ({pixmap.width()}x{pixmap.height()})")


def _seed_session_on_disk() -> Session:
    """Lay down raw.transcript.md / live_notes.md / notes.md for the demo session.

    Also drops a couple of archived synthesis versions so the
    Previous Notes pane has content to show (the v0.6.3 redesign
    renders an empty-state pane otherwise, which isn't a useful
    screenshot)."""
    store = TranscriptStore(SESSION_ID)
    store.transcript_path.write_text(SAMPLE_TRANSCRIPT, encoding="utf-8")
    store.live_notes_path.write_text(SAMPLE_LIVE_NOTES, encoding="utf-8")
    store.notes_path.write_text(SAMPLE_SYNTHESIS, encoding="utf-8")
    # Seed two archived versions so PreviousNotesWidget has list items.
    (store.session_dir / "notes-20260520-0900.md").write_text(
        "# Q3 Platform Sync (v1)\n\n"
        "## Decisions\n- Migrate the ingest pipeline to a queue-based model.\n\n"
        "## Action items\n- Alex to draft the queue interface by Friday.\n",
        encoding="utf-8",
    )
    (store.session_dir / "notes-20260521-1430.md").write_text(
        "# Q3 Platform Sync (v2)\n\n"
        "## Decisions\n- Queue-based ingest, kicked off Monday.\n"
        "- Backfill plan tied to the same migration window.\n\n"
        "## Action items\n- Sam to spec the backfill steps.\n",
        encoding="utf-8",
    )
    return Session(
        id=SESSION_ID,
        title=SESSION_TITLE,
        created_at=SESSION_DATE.isoformat(),
        state=STATE_COMPLETE,
        has_audio=False,
        has_transcript=True,
        has_notes=True,
    )


MAIN_WIN_SIZE = (1280, 760)


def _build_main_window(*, automation_enabled: bool = False) -> MainWindow:
    session = _seed_session_on_disk()
    win = MainWindow()
    win.resize(*MAIN_WIN_SIZE)
    win.set_sessions([session])
    win.session_view.set_user_name(USER_NAME)
    store = TranscriptStore(SESSION_ID)
    win.session_view.set_session(
        session,
        transcript=SAMPLE_TRANSCRIPT,
        notes=SAMPLE_SYNTHESIS,
        previous_notes_paths=store.list_previous_notes(),
        live_notes=SAMPLE_LIVE_NOTES,
    )
    # Seed the per-session prompt-template picker so the dropdown
    # has realistic content. MainApp does this at runtime; the
    # screenshot harness has to do it manually.
    templates = [t.name for t in prompts_mod.list_templates()]
    win.session_view.set_prompt_templates(templates)
    from meeting_notetaker.ui.status_indicators import SegmentState
    base_indicators: dict[str, SegmentState] = {
        "mic": SegmentState(
            color="gray",
            short_label="Mic",
            payload="(default)",
            tooltip="Microphone device: (System default)",
        ),
        "sys": SegmentState(
            color="gray",
            short_label="Sys",
            payload="(default)",
            tooltip="System audio capture (loopback): (System default)",
        ),
        "voice": SegmentState(
            color="yellow",
            short_label="Voiceprint",
            tooltip="No voice sample has been recorded.",
        ),
    }
    if automation_enabled:
        win.session_view.set_automation_enabled(True, "claude")
        base_indicators["syn"] = SegmentState(
            color="green",
            short_label="Syn",
            tooltip=(
                "The Meeting Notetaker extension is connected. "
                "Send is ready to use."
            ),
        )
    win.set_status_indicators(version=__version__, indicators=base_indicators)
    win.select_session(SESSION_ID)
    win.show()
    QApplication.processEvents()
    return win


# ---------------------------------------------------------------------------
# Shot functions
# ---------------------------------------------------------------------------


def shot_main_transcript() -> None:
    # Transcript moved to the rightmost tab in v0.6.5. Order:
    # 0 My Notes, 1 Synthesis, 2 Previous Notes, 3 Transcript.
    win = _build_main_window()
    win.session_view._tabs.setCurrentIndex(3)
    QApplication.processEvents()
    _grab(win, "01-main-transcript.png", autosize=False)
    win.close()


def shot_main_my_notes_edit() -> None:
    win = _build_main_window()
    win.session_view._tabs.setCurrentIndex(0)
    # Force edit mode (template-seeded notes default to preview when populated).
    win.session_view._live_notes_editor.set_preview_mode(False)
    QApplication.processEvents()
    _grab(win, "02-main-my-notes-edit.png", autosize=False)
    win.close()


def shot_main_my_notes_preview() -> None:
    win = _build_main_window()
    win.session_view._tabs.setCurrentIndex(0)
    win.session_view._live_notes_editor.set_preview_mode(True)
    QApplication.processEvents()
    _grab(win, "03-main-my-notes-preview.png", autosize=False)
    win.close()


def shot_main_synthesis() -> None:
    win = _build_main_window()
    win.session_view._tabs.setCurrentIndex(1)
    # Synthesis defaults to preview mode in set_session; explicit set is a no-op
    # but keeps the assumption in the script obvious.
    win.session_view._notes_view.set_preview_mode(True)
    QApplication.processEvents()
    _grab(win, "04-main-synthesis.png", autosize=False)
    win.close()


def shot_main_previous_notes() -> None:
    win = _build_main_window()
    win.session_view._tabs.setCurrentIndex(2)
    QApplication.processEvents()
    _grab(win, "05-main-previous-notes.png", autosize=False)
    win.close()


def shot_new_session() -> None:
    # allow_calendar_pick=False keeps the dialog compact and consistent on
    # Linux where Outlook is unavailable anyway.
    dlg = NewSessionDialog(
        retain_audio_default=False,
        allow_calendar_pick=False,
    )
    dlg.resize(440, 200)
    dlg.show()
    QApplication.processEvents()
    _grab(dlg, "06-dialog-new-session.png", autosize=False)
    dlg.close()


def shot_settings() -> None:
    cfg = Config()
    cfg.ui.user_name = USER_NAME
    # Pre-enable synthesis automation so the new section's controls
    # (target picker, Claude project field, install/uninstall) are
    # all visible in the screenshot. Default (False) would still
    # show the group but the placeholder text/help reads better
    # when the picker has its real selected target shown.
    cfg.synthesis.automation_enabled = True
    cfg.synthesis.llm_target = "claude"
    dlg = SettingsDialog(cfg)
    # The dialog has a scroll area; force-resize to a tall window
    # so the full content is visible (rather than just the
    # Transcription group at the top). v0.6.3 added the synthesis
    # automation + prompts groups at the bottom -- bumped from 1200
    # to 1600 to accommodate.
    dlg.resize(680, 1600)
    dlg.show()
    QApplication.processEvents()
    _grab(dlg, "07-dialog-settings.png", autosize=False)
    dlg.close()


def shot_generate_prompt() -> None:
    templates = prompts_mod.list_templates()
    # Drop into the default template explicitly so the rendered preview is
    # the one captured.
    default = next((t for t in templates if t.name == "default"), templates[0])
    dlg = GeneratePromptDialog(
        session_title=SESSION_TITLE,
        session_date=SESSION_DATE,
        transcript=SAMPLE_TRANSCRIPT,
        templates=[default],
        live_notes=SAMPLE_LIVE_NOTES,
        user_name=USER_NAME,
    )
    dlg.resize(900, 640)
    dlg.show()
    QApplication.processEvents()
    _grab(dlg, "08-dialog-generate-prompt.png", autosize=False)
    dlg.close()


def shot_paste_response() -> None:
    dlg = PasteNotesDialog(current_notes=SAMPLE_PASTE_BODY)
    dlg.resize(900, 640)
    dlg.show()
    QApplication.processEvents()
    _grab(dlg, "09-dialog-paste-response.png", autosize=False)
    dlg.close()


def _fake_meetings() -> list[outlook_calendar.MeetingInfo]:
    base = datetime(2026, 5, 17, 14, 0)
    return [
        outlook_calendar.MeetingInfo(
            entry_id="fake-1",
            subject="Q3 Platform Sync",
            start_time=base,
            end_time=base + timedelta(minutes=30),
            attendees=[
                outlook_calendar.CalendarAttendee(name=n)
                for n in ATTENDEES
            ],
            body="Agenda: rollout plan walkthrough, security review handoff.",
        ),
        outlook_calendar.MeetingInfo(
            entry_id="fake-2",
            subject="Architecture Review -- read tier",
            start_time=base + timedelta(minutes=90),
            end_time=base + timedelta(minutes=150),
            attendees=[
                outlook_calendar.CalendarAttendee(name=n)
                for n in ATTENDEES
            ],
            body="",
        ),
        outlook_calendar.MeetingInfo(
            entry_id="fake-3",
            subject="Quick chat re: hiring panel",
            start_time=base + timedelta(minutes=165),
            end_time=base + timedelta(minutes=180),
            attendees=[],
            body="",
        ),
    ]


def shot_calendar_picker() -> None:
    # CalendarPickerDialog calls fetch_remaining_today() in __init__. Monkey
    # patch on the picker_dialog module's namespace (the picker imports the
    # function by name, so patching the source module isn't sufficient).
    from meeting_notetaker.ui import calendar_picker_dialog as picker_mod

    saved = picker_mod.fetch_remaining_today
    picker_mod.fetch_remaining_today = _fake_meetings
    try:
        dlg = CalendarPickerDialog()
        dlg.resize(720, 420)
        dlg.show()
        QApplication.processEvents()
        _grab(dlg, "11-dialog-calendar-picker.png", autosize=False)
        dlg.close()
    finally:
        picker_mod.fetch_remaining_today = saved


def shot_new_session_calendar_prefill() -> None:
    meeting = _fake_meetings()[0]
    dlg = NewSessionDialog(
        retain_audio_default=False,
        title_prefill=meeting.subject,
        prefill_note=(
            "Pre-filled from your Outlook invite. Attendees + agenda will "
            f"appear in My Notes. Starts at {meeting.start_time.strftime('%H:%M')}."
        ),
        calendar_meeting=meeting,
        allow_calendar_pick=False,
    )
    dlg.resize(440, 240)
    dlg.show()
    QApplication.processEvents()
    _grab(dlg, "10-dialog-new-session-calendar-prefill.png", autosize=False)
    dlg.close()


def shot_label_dialog() -> None:
    entries = [
        SpeakerWalkerEntry(
            cluster_id=0,
            current_name=None,
            example_lines=[
                "[00:02:14] Speaker 1: thanks for joining everyone, let's start with the rollout updates",
                "[00:05:42] Speaker 1: I think we need to revisit the API contract on the read path",
                "[00:11:08] Speaker 1: from my side I'd push for an end-of-month timeline on this",
            ],
            centroid=_unit_vec(1, 0, 0),
            match_similarity=None,
            suggestions=OTHER_ATTENDEES + [USER_NAME],
        ),
        SpeakerWalkerEntry(
            cluster_id=1,
            current_name=None,
            example_lines=[
                "[00:03:21] Speaker 2: agreed. We should also flag the dependency on the migration",
                "[00:09:47] Speaker 2: yeah, the integration team caught that one last sprint",
            ],
            centroid=_unit_vec(0, 1, 0),
            match_similarity=None,
            suggestions=OTHER_ATTENDEES + [USER_NAME],
        ),
    ]
    dlg = SpeakerWalkerDialog(entries, mode="label", session_title=SESSION_TITLE)
    dlg.show()
    QApplication.processEvents()
    _grab(dlg, "12-dialog-label-unknown-speakers.png")
    dlg.close()


def shot_review_dialog() -> None:
    entries = [
        SpeakerWalkerEntry(
            cluster_id=0,
            current_name="Sam Patel",
            example_lines=[
                "[00:02:14] Sam Patel: thanks for joining everyone, let's start with the rollout updates",
                "[00:11:08] Sam Patel: from my side I'd push for an end-of-month timeline on this",
            ],
            centroid=_unit_vec(1, 0, 0),
            match_similarity=0.86,
            suggestions=OTHER_ATTENDEES + [USER_NAME],
        ),
        SpeakerWalkerEntry(
            cluster_id=1,
            current_name="Jordan Lee",
            example_lines=[
                "[00:03:21] Jordan Lee: agreed. We should also flag the dependency on the migration",
            ],
            centroid=_unit_vec(0, 1, 0),
            match_similarity=0.78,
            suggestions=OTHER_ATTENDEES + [USER_NAME],
        ),
        SpeakerWalkerEntry(
            cluster_id=2,
            current_name=None,
            example_lines=[
                "[00:18:05] Speaker 3: I have one more thing if there's time at the end",
            ],
            centroid=_unit_vec(0, 0, 1),
            match_similarity=None,
            suggestions=OTHER_ATTENDEES + [USER_NAME],
        ),
    ]
    dlg = SpeakerWalkerDialog(entries, mode="review", session_title=SESSION_TITLE)
    dlg.show()
    QApplication.processEvents()
    _grab(dlg, "13-dialog-review-speakers.png")
    dlg.close()


def shot_manage_dialog() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = SpeakerStore(Path(td) / "speakers.db")
        try:
            store.upsert("Sam Patel", _unit_vec(1, 0, 0), sample_count=7)
            store.upsert("Jordan Lee", _unit_vec(0, 1, 0), sample_count=3)
            store.upsert("Riley Park", _unit_vec(0, 0, 1), sample_count=12)
            store.upsert("Morgan Hayes", _unit_vec(0.5, 0.5, 0), sample_count=2)
            dlg = SpeakersManageDialog(store)
            dlg.show()
            QApplication.processEvents()
            _grab(dlg, "14-dialog-manage-speakers.png")
            dlg.close()
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def shot_main_synthesis_automation_enabled() -> None:
    """v0.6.3: Send-to-Claude button replaces Generate + Paste when
    synthesis automation is enabled in Settings. Captures the synthesis
    tab with the new prompt-template picker visible too."""
    win = _build_main_window(automation_enabled=True)
    win.session_view._tabs.setCurrentIndex(2)
    win.session_view._notes_view.set_preview_mode(True)
    QApplication.processEvents()
    _grab(win, "15-main-synthesis-automation.png", autosize=False)
    win.close()


def shot_automation_install_dialog() -> None:
    """v0.6.3: three-step install wizard launched from Settings when
    the user toggles synthesis automation on for the first time."""
    from meeting_notetaker.ui.automation_install_dialog import (
        AutomationInstallDialog,
    )

    # Minimal stubs -- the wizard's UI shows the three steps + their
    # current state without needing the install to actually succeed.
    dlg = AutomationInstallDialog(do_install=lambda: {}, ping_extension=None)
    dlg.resize(580, 540)
    dlg.show()
    QApplication.processEvents()
    _grab(dlg, "16-dialog-automation-install.png", autosize=False)
    dlg.close()


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    shot_main_transcript()
    shot_main_my_notes_edit()
    shot_main_my_notes_preview()
    shot_main_synthesis()
    shot_main_previous_notes()
    shot_new_session()
    shot_settings()
    shot_generate_prompt()
    shot_paste_response()
    shot_new_session_calendar_prefill()
    shot_calendar_picker()
    shot_label_dialog()
    shot_review_dialog()
    shot_manage_dialog()
    shot_main_synthesis_automation_enabled()
    shot_automation_install_dialog()
    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
