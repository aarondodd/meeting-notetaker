"""MainApp -- the top-level orchestrator that wires UI, controller, tray, and store.

main() is the entry point used by main.py and the pyinstaller spec.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import Optional

from PyQt6.QtCore import QObject, Qt, QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox

from .controller import SessionController
from .diarization.persistence import (
    load_diarization,
    save_diarization,
    update_cluster_name,
)
from .diarization.refiner import RefinementResult, apply_labels_to_segments
from .diarization.store import open_speaker_store
from .integrations import outlook_calendar
from .integrations.outlook_calendar import MeetingInfo
from .models.session import (
    STATE_COMPLETE,
    STATE_ERROR,
    STATE_NEW,
    STATE_PAUSED,
    STATE_PROCESSING,
    STATE_RECORDING,
    Session,
    SessionStore,
)
from .models.transcript import TranscriptSegment, TranscriptStore
from .transcription import model_manager
from .ui.devices_dialog import DevicesDialog
from .ui.main_window import MainWindow
from .ui.new_session_dialog import NewSessionDialog
from .ui.progress import run_with_progress
from .ui.prompt_dialog import GeneratePromptDialog, PasteNotesDialog
from .ui.settings_dialog import SettingsDialog
from .ui.speaker_walker_dialog import (
    SpeakerWalkerDecision,
    SpeakerWalkerDialog,
)
from .ui.speaker_walker_helpers import (
    entries_from_persistence,
    entries_from_refinement,
    gather_suggestions,
)
from .ui.tray import TrayIcon
from .utils import prompts as prompts_mod
from .utils import updater as updater_mod
from .utils.config import Config
from .utils.icons import app_icon
from .utils.live_notes import extract_section, parse_attendees, seed_body_with_calendar
from .utils.paths import app_data_dir, calendar_state_path, log_path
from .utils.single_instance import acquire as acquire_lock, release as release_lock
from .utils.vocabulary import seed_vocabulary_file
from .version import __version__


log = logging.getLogger("meeting_notetaker")


# Map session state to tray state.
_TRAY_FOR_STATE = {
    STATE_NEW: "ready",
    STATE_RECORDING: "recording",
    STATE_PAUSED: "paused",
    STATE_PROCESSING: "processing",
    STATE_COMPLETE: "ready",
    STATE_ERROR: "error",
}


class MainApp(QObject):
    def __init__(self, qt_app: QApplication) -> None:
        super().__init__()
        self.qt_app = qt_app
        self.config = Config.load()
        prompts_mod.seed_user_prompts()
        seed_vocabulary_file()
        self.store = SessionStore()
        self.controller = SessionController(self.store, self.config, parent=self)
        self.window = MainWindow()
        self.tray = TrayIcon(self.window)
        self._calendar_monitor = None  # set lazily by _apply_calendar_config

        self._wire_signals()
        self._apply_user_name()
        self._refresh_session_list()
        self._handle_crash_recovery()
        self._warn_if_store_python()
        self._apply_calendar_config()
        self._refresh_status_indicators()
        # Clean up the .old file left over from a prior in-place upgrade.
        # On Windows the previous .exe is renamed (not deleted) when the
        # new one is installed; the rename succeeds while the app is
        # running, but the unlink can only happen once the old binary
        # is no longer loaded. The next launch is the natural cleanup
        # point.
        try:
            if updater_mod.cleanup_old_exe():
                log.info("Cleaned up stale .old executable from prior upgrade.")
        except Exception:
            log.exception("cleanup of stale .old exec failed")

        self.window.show()
        self.tray.set_state("idle")

        # Weekly background check for a newer release on GitHub. Defer 2s
        # after show() so the network call never blocks the first paint.
        QTimer.singleShot(2000, self._auto_check_for_updates)

    def _apply_user_name(self) -> None:
        self.window.session_view.set_user_name(self.config.ui.user_name)

    # ---- wiring ------------------------------------------------------------

    def _wire_signals(self) -> None:
        self.window.new_session_requested.connect(self._on_new_session)
        self.window.open_settings_requested.connect(self._on_settings)
        self.window.open_devices_dialog_requested.connect(self._on_devices)
        self.window.open_outlook_diagnostic_requested.connect(self._on_outlook_diagnostic)
        self.window.open_log_viewer_requested.connect(self._on_log_viewer)
        self.window.check_for_updates_requested.connect(self._on_check_for_updates)
        self.window.upgrade_requested.connect(self._on_upgrade)
        self.window.quit_requested.connect(self.qt_app.quit)
        self.window.session_selected.connect(self._on_session_selected)
        self.window.delete_sessions_requested.connect(self._on_delete_sessions)

        self.tray.open_main_window.connect(self._foreground_window)
        self.tray.new_session_requested.connect(self._on_new_session)
        self.tray.pause_requested.connect(self.controller.pause_session)
        self.tray.resume_requested.connect(self.controller.resume_session)
        self.tray.stop_requested.connect(self.controller.stop_session)
        self.tray.settings_requested.connect(self._on_settings)
        self.tray.quit_requested.connect(self.qt_app.quit)
        self.tray.meeting_notification_clicked.connect(self._on_create_session_from_calendar)

        sv = self.window.session_view
        sv.start_clicked.connect(self._on_start_clicked)
        sv.pause_clicked.connect(lambda _sid: self.controller.pause_session())
        sv.resume_clicked.connect(lambda _sid: self.controller.resume_session())
        sv.stop_clicked.connect(lambda _sid: self.controller.stop_session())
        sv.generate_prompt_clicked.connect(self._on_generate_prompt)
        sv.paste_notes_clicked.connect(self._on_paste_notes)
        sv.copy_tab_clicked.connect(self._on_copy_tab)
        sv.live_notes_changed.connect(self._on_live_notes_changed)
        sv.synthesis_notes_changed.connect(self._on_synthesis_notes_changed)
        sv.review_speakers_clicked.connect(self._on_review_speakers)
        sv.retain_audio_toggled.connect(self.controller.set_retain_audio)

        self.controller.state_changed.connect(self._on_session_state_changed)
        self.controller.segment_arrived.connect(self._on_segment_arrived)
        self.controller.transcript_replaced.connect(self._on_transcript_replaced)
        self.controller.batch_progress.connect(self._on_batch_progress)
        self.controller.speaker_refinement_starting.connect(
            self._on_speaker_refinement_starting
        )
        self.controller.speaker_refinement_done.connect(
            self._on_speaker_refinement_done
        )
        self.controller.speaker_refinement_skipped.connect(
            self._on_speaker_refinement_skipped
        )
        self.controller.error.connect(self._on_controller_error)
        self.controller.status.connect(lambda msg: self.window.status(msg, timeout_ms=5000))

    # ---- session list ------------------------------------------------------

    def _refresh_session_list(self, *, select: Optional[str] = None) -> None:
        sessions = self.store.list_sessions()
        self.window.set_sessions(sessions)
        if select:
            self.window.select_session(select)
        elif sessions:
            self.window.select_session(sessions[0].id)

    def _on_session_selected(self, session_id: str) -> None:
        session = self.store.get_session(session_id)
        if session is None:
            return
        store = TranscriptStore(session_id)
        self.window.session_view.set_session(
            session,
            transcript=store.read_transcript(),
            notes=store.read_notes(),
            previous_notes_paths=store.list_previous_notes(),
            live_notes=store.read_live_notes(),
        )

    # ---- session lifecycle handlers ---------------------------------------

    def _on_new_session(self) -> None:
        dialog = NewSessionDialog(
            retain_audio_default=self.config.audio.retain_audio_default,
            parent=self.window,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        result = dialog.result_value()
        session = self.store.create_session(
            title=result.title,
            retain_audio=result.retain_audio,
        )
        if result.calendar_meeting is not None:
            self._seed_live_notes_from_meeting(session.id, result.calendar_meeting)
        self._refresh_session_list(select=session.id)

    def _seed_live_notes_from_meeting(
        self, session_id: str, info: MeetingInfo
    ) -> None:
        attendee_names = [a.display for a in info.attendees if a.display]
        try:
            TranscriptStore(session_id).save_live_notes(
                seed_body_with_calendar(
                    attendees=attendee_names, agenda=info.body
                )
            )
        except OSError:
            log.exception("failed to seed live notes from calendar")

    def _on_start_clicked(self, session_id: str) -> None:
        session = self.store.get_session(session_id)
        if session is None:
            return
        # Preload the Whisper model under a progress dialog so the UI doesn't
        # freeze during first-run download or model swap. If capture-only mode
        # is on, live workers don't load the model -- but the batch pass at
        # stop time still needs it, so we preload either way.
        size = self.config.transcription.model_size
        if model_manager.current_size() == size:
            self.controller.start_session(session)
            return

        run_with_progress(
            self.window,
            title="Loading Whisper model",
            message=(
                f"Loading the '{size}' model (this can take a minute on first run "
                "while the weights download). The app will respond again once it's ready."
            ),
            fn=model_manager.get_model,
            kwargs={"size": size},
            on_success=lambda _model: self.controller.start_session(session),
            on_failure=lambda msg: self._on_controller_error(msg),
        )

    def _on_session_state_changed(self, session_id: str, state: str) -> None:
        # Update list label + selected view.
        self._refresh_session_list(select=session_id)
        self._on_session_selected(session_id)
        self.tray.set_state(_TRAY_FOR_STATE.get(state, "idle"))

    def _on_segment_arrived(self, session_id: str, segment) -> None:
        sv = self.window.session_view
        if sv._session and sv._session.id == session_id:
            sv.append_segment(segment)

    def _on_transcript_replaced(self, session_id: str, segments: list) -> None:
        from .models.transcript import format_segment

        sv = self.window.session_view
        if sv._session and sv._session.id == session_id:
            sv.set_transcript_text("\n".join(format_segment(s) for s in segments))

    def _on_batch_progress(self, session_id: str, pct: int) -> None:
        sv = self.window.session_view
        if sv._session and sv._session.id == session_id:
            sv.update_batch_progress(pct)

    def _on_controller_error(self, msg: str) -> None:
        log.error("controller error: %s", msg)
        self.window.status(msg, timeout_ms=8000)
        QMessageBox.warning(self.window, "Meeting Notetaker", msg)

    # ---- synthesis flows ---------------------------------------------------

    def _on_generate_prompt(self, session_id: str) -> None:
        session = self.store.get_session(session_id)
        if session is None:
            return
        store = TranscriptStore(session_id)
        transcript = store.read_transcript()
        if not transcript.strip():
            QMessageBox.information(self.window, "Generate Prompt", "This session has no transcript yet.")
            return
        # Ensure the editor's pending text is on disk and reflected in the prompt.
        self.window.session_view.flush_pending_live_notes()
        live_notes = store.read_live_notes()
        try:
            when = datetime.fromisoformat(session.created_at.replace("Z", "+00:00"))
        except ValueError:
            when = datetime.now(timezone.utc)
        dialog = GeneratePromptDialog(
            session_title=session.title,
            session_date=when,
            transcript=transcript,
            live_notes=live_notes,
            user_name=self.config.ui.user_name,
            templates=prompts_mod.list_templates(),
            parent=self.window,
        )
        dialog.exec()

    def _on_live_notes_changed(self, session_id: str, body: str) -> None:
        try:
            TranscriptStore(session_id).save_live_notes(body)
        except OSError:
            log.exception("failed to save live notes for %s", session_id)

    def _on_synthesis_notes_changed(self, session_id: str, body: str) -> None:
        """Persist inline edits to the Synthesis tab.

        archive_existing=False -- we're editing the current note in place,
        not replacing it with a new one. The Paste Response Back flow
        retains the original archiving behavior so each fresh synthesis
        preserves the prior version under notes-YYYYMMDD-HHMM.md.
        """
        try:
            TranscriptStore(session_id).save_notes(body, archive_existing=False)
            session = self.store.get_session(session_id)
            if session is not None and not session.has_notes and body.strip():
                self.store.update_session(session_id, has_notes=True)
        except OSError:
            log.exception("failed to save synthesis notes for %s", session_id)

    # ---- speaker refinement + labeling -------------------------------------

    def _on_speaker_refinement_starting(self, session_id: str) -> None:
        self.window.status("Identifying speakers...", timeout_ms=0)

    def _on_speaker_refinement_skipped(self, session_id: str, reason: str) -> None:
        log.info("speaker refinement skipped for %s: %s", session_id, reason)
        # Show a brief one-line note; don't block with a dialog.
        self.window.status(f"Speaker ID skipped: {reason}", timeout_ms=8000)

    def _on_speaker_refinement_done(
        self, session_id: str, result: RefinementResult
    ) -> None:
        """Pop the Label Unknown Speakers dialog when refinement finds unknowns.

        Show the Review Speakers button regardless so the user can revisit
        the cluster mapping later. The transcript was already rewritten
        with names (or `Speaker N` fallbacks for unknowns) by the
        controller before this signal fired.
        """
        sv = self.window.session_view
        # The session may have changed in the UI since refinement started.
        # Only flip the Review button on if we're still on this session.
        if sv._session is not None and sv._session.id == session_id:
            sv.set_has_diarization(True)
        if not result.has_unknown():
            count = len(result.clusters)
            if count > 0:
                self.window.status(
                    f"Identified {count} speaker(s); all matched the store.",
                    timeout_ms=8000,
                )
            return
        try:
            self._launch_label_dialog(session_id, result)
        except Exception:
            log.exception("label dialog failed; users can still use Review Speakers")

    def _launch_label_dialog(self, session_id: str, result: RefinementResult) -> None:
        suggestions = self._suggestion_pool_for(session_id)
        transcript_segments = self._read_transcript_segments(session_id)
        entries = entries_from_refinement(
            result,
            transcript_segments,
            suggestions=suggestions,
            only_unknown=True,
        )
        if not entries:
            return
        session = self.store.get_session(session_id)
        title = session.title if session else ""
        dialog = SpeakerWalkerDialog(entries, mode="label", session_title=title, parent=self.window)
        if dialog.exec() != SpeakerWalkerDialog.DialogCode.Accepted:
            return
        self._apply_walker_decisions(session_id, dialog.decisions(), transcript_segments)

    def _on_review_speakers(self, session_id: str) -> None:
        """Manual review walker. Reads diarization.json for the session."""
        store = TranscriptStore(session_id)
        data = load_diarization(store.session_dir)
        if data is None:
            QMessageBox.information(
                self.window,
                "Review Speakers",
                "No speaker data is available for this session yet. "
                "Speaker identification runs after a recording stops; "
                "if you ran with the loopback channel off or with "
                "speaker ID disabled in Settings, there's nothing to "
                "review here.",
            )
            return
        suggestions = self._suggestion_pool_for(session_id)
        transcript_segments = self._read_transcript_segments(session_id)
        entries = entries_from_persistence(
            data,
            transcript_segments,
            suggestions=suggestions,
            only_unknown=False,
        )
        if not entries:
            QMessageBox.information(
                self.window,
                "Review Speakers",
                "No detected speakers to review.",
            )
            return
        session = self.store.get_session(session_id)
        title = session.title if session else ""
        dialog = SpeakerWalkerDialog(entries, mode="review", session_title=title, parent=self.window)
        if dialog.exec() != SpeakerWalkerDialog.DialogCode.Accepted:
            return
        self._apply_walker_decisions(session_id, dialog.decisions(), transcript_segments)

    def _apply_walker_decisions(
        self,
        session_id: str,
        decisions: list[SpeakerWalkerDecision],
        transcript_segments: list[TranscriptSegment],
    ) -> None:
        """Persist the user's labeling choices and rewrite the transcript.

        Side effects:
        - For each decision with a non-empty `name`:
            - Update diarization.json: cluster.name = name
            - Speaker store: add_sample(name, centroid) for the
              running-average learning loop.
        - For each decision flagged should_forget:
            - Update diarization.json: cluster.name = None
            - (We do NOT delete the speaker from the global store; that
              is the Settings > Speakers job.)
        - After all updates land, rewrite raw.transcript.md so any
          rename / forget reflects in the on-disk file.
        """
        sdir = TranscriptStore(session_id).session_dir
        speaker_store = open_speaker_store()
        try:
            for decision in decisions:
                if decision.should_forget:
                    update_cluster_name(sdir, decision.cluster_id, None)
                    continue
                if not decision.name:
                    continue  # no change
                update_cluster_name(sdir, decision.cluster_id, decision.name)
                speaker_store.add_sample(decision.name, decision.centroid)
        finally:
            speaker_store.close()
        self._rewrite_transcript_from_diarization(session_id, transcript_segments)
        # If the UI is still on this session, refresh its transcript view.
        sv = self.window.session_view
        if sv._session is not None and sv._session.id == session_id:
            fresh = TranscriptStore(session_id).read_transcript()
            sv.set_transcript_text(fresh)

    def _rewrite_transcript_from_diarization(
        self,
        session_id: str,
        transcript_segments: list[TranscriptSegment],
    ) -> None:
        """Re-apply diarization.json's cluster->name map to the transcript."""
        store = TranscriptStore(session_id)
        data = load_diarization(store.session_dir)
        if data is None:
            return
        name_by_cluster = {c.cluster_id: c.name for c in data.clusters}
        cluster_by_segment_index = {
            s.segment_index: s.cluster_id for s in data.segments
        }
        labeled: list[TranscriptSegment] = []
        for i, seg in enumerate(transcript_segments):
            cluster_id = cluster_by_segment_index.get(i)
            if cluster_id is None or seg.source != "sys":
                labeled.append(seg)
                continue
            name = name_by_cluster.get(cluster_id)
            if name:
                new_seg = TranscriptSegment(
                    source=seg.source,
                    text=seg.text,
                    t_start=seg.t_start,
                    t_end=seg.t_end,
                    is_provisional=seg.is_provisional,
                    speaker_name=name,
                )
            else:
                # Fall back to "Speaker N" so reverted clusters still
                # render distinctly from the original "Them:" label.
                new_seg = TranscriptSegment(
                    source=seg.source,
                    text=seg.text,
                    t_start=seg.t_start,
                    t_end=seg.t_end,
                    is_provisional=seg.is_provisional,
                    speaker_name=f"Speaker {cluster_id + 1}",
                )
            labeled.append(new_seg)
        store.write_segments(labeled)

    def _read_transcript_segments(self, session_id: str) -> list[TranscriptSegment]:
        """Parse raw.transcript.md back into TranscriptSegments.

        We don't persist segments structurally on disk (the markdown file
        is the source of truth), so this re-parses on demand. The shape
        is what the walker needs for example-line rendering and the
        cluster->name rewrite path.
        """
        import re

        store = TranscriptStore(session_id)
        text = store.read_transcript()
        segments: list[TranscriptSegment] = []
        line_re = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\] ([^:]+): (.*)$")
        for line in text.splitlines():
            m = line_re.match(line)
            if not m:
                continue
            h, mn, s, label, content = m.groups()
            t_start = int(h) * 3600 + int(mn) * 60 + int(s)
            # Source is mic if the label resolves to the user's name (or "Me");
            # everything else is sys. The walker only cares about sys segments
            # but we keep mic ones in the list to preserve indices.
            user_label = (self.config.ui.user_name or "Me").strip()
            source = "mic" if label.strip() in ("Me", user_label) else "sys"
            speaker_name = None if source == "mic" or label.strip() == "Them" else label.strip()
            segments.append(TranscriptSegment(
                source=source,
                text=content,
                t_start=float(t_start),
                t_end=float(t_start) + 1.0,  # approximation -- only used for ordering
                is_provisional=False,
                speaker_name=speaker_name,
            ))
        return segments

    def _suggestion_pool_for(self, session_id: str) -> list[str]:
        """Build the name suggestion pool for the walker combo box.

        Pulls known speakers from the store plus attendees from the
        session's live_notes.md. Empty if either source is empty.
        """
        store = TranscriptStore(session_id)
        try:
            live_notes = store.read_live_notes()
        except OSError:
            live_notes = ""
        attendees = parse_attendees(live_notes) if live_notes else []
        speaker_store = open_speaker_store()
        try:
            known = [s.name for s in speaker_store.list_all()]
        finally:
            speaker_store.close()
        return gather_suggestions(known, attendees)

    def _on_copy_tab(self, session_id: str, tab_id: str) -> None:
        import pyperclip
        sv = self.window.session_view
        # Flush pending live-notes edits so the clipboard matches what's
        # visible on screen.
        if tab_id == "live_notes":
            sv.flush_pending_live_notes()
        text = sv.active_tab_text()
        label = sv.active_tab_label() or "Tab"
        if not text.strip():
            QMessageBox.information(
                self.window, f"Copy {label}", f"{label} is empty -- nothing to copy."
            )
            return
        try:
            pyperclip.copy(text)
        except Exception as exc:
            log.exception("clipboard copy failed")
            QMessageBox.warning(
                self.window, f"Copy {label}", f"Clipboard copy failed: {exc}"
            )
            return
        self.window.status(f"{label} copied to clipboard.", timeout_ms=4000)

    def _on_paste_notes(self, session_id: str) -> None:
        session = self.store.get_session(session_id)
        if session is None:
            return
        store = TranscriptStore(session_id)
        dialog = PasteNotesDialog(current_notes="", parent=self.window)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        body = dialog.body
        if not body.strip():
            QMessageBox.information(self.window, "Paste Notes", "Nothing to save (empty input).")
            return
        archive_path = store.save_notes(body, archive_existing=dialog.archive_existing)
        self.store.update_session(session_id, has_notes=True)
        self._on_session_selected(session_id)
        if archive_path:
            self.window.status(f"Notes saved. Prior notes archived to {archive_path.name}", timeout_ms=8000)
        else:
            self.window.status("Notes saved.", timeout_ms=5000)

    # ---- bulk delete -------------------------------------------------------

    def _on_delete_sessions(self, session_ids: list[str]) -> None:
        import shutil
        from .utils.paths import session_dir

        for sid in session_ids:
            try:
                shutil.rmtree(session_dir(sid), ignore_errors=True)
            except OSError:
                log.exception("failed to remove session dir for %s", sid)
        removed = self.store.delete_sessions(session_ids)
        self._refresh_session_list()
        self.window.session_view.set_session(None, transcript="", notes="", previous_notes_paths=[])
        self.window.status(f"Deleted {removed} session(s)", timeout_ms=5000)

    # ---- settings ---------------------------------------------------------

    def _on_devices(self) -> None:
        dialog = DevicesDialog(parent=self.window)
        dialog.exec()

    def _on_outlook_diagnostic(self) -> None:
        from .ui.outlook_diagnostic_dialog import OutlookDiagnosticDialog
        OutlookDiagnosticDialog(parent=self.window).exec()

    def _on_log_viewer(self) -> None:
        from .ui.log_viewer_dialog import LogViewerDialog
        # Non-modal: lets the user keep using the app while watching the log.
        # Stored as an attribute so the dialog isn't garbage-collected when
        # the handler returns.
        if getattr(self, "_log_viewer", None) is None:
            self._log_viewer = LogViewerDialog(log_path(), parent=self.window)
        self._log_viewer.show()
        self._log_viewer.raise_()
        self._log_viewer.activateWindow()

    def _on_settings(self) -> None:
        dialog = SettingsDialog(self.config, parent=self.window)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        errors = self.config.validate()
        if errors:
            QMessageBox.warning(self.window, "Settings", "\n".join(errors))
            return
        self.config.save()
        self._apply_user_name()
        self._apply_calendar_config()
        self._refresh_status_indicators()
        self.window.status("Settings saved.", timeout_ms=4000)

    # ---- status bar indicators -------------------------------------------

    def _refresh_status_indicators(self) -> None:
        """Repopulate the right-side status bar widgets from current state.

        Pulled out so settings-saved, calendar-config-applied, and startup
        all share one source of truth.
        """
        mic = self.config.audio.mic_device_name or "(System default)"
        loopback = self.config.audio.loopback_device_name or "(System default)"

        # Calendar indicator: combines the user's intent (Watch on/off) with
        # actual Outlook reachability so the user sees whether the feature
        # is silently disabled.
        if not self.config.calendar.watch_calendar:
            cal_label = "Calendar: off"
            cal_tooltip = (
                "Outlook calendar watching is disabled. Enable it in Settings "
                "to be notified when a meeting is about to start."
            )
        elif outlook_calendar.is_available():
            running = self._calendar_monitor is not None and self._calendar_monitor.is_running()
            if running:
                cal_label = "Calendar: watching"
                cal_tooltip = (
                    f"Watching Outlook calendar; notifying within "
                    f"+- {self.config.calendar.window_minutes} min of each "
                    "meeting start."
                )
            else:
                cal_label = "Calendar: idle"
                cal_tooltip = (
                    "Calendar watching is enabled but the monitor is not "
                    "running. Try toggling it off and on in Settings."
                )
        else:
            cal_label = "Calendar: Outlook unavailable"
            cal_tooltip = (
                "Calendar watching is enabled, but Outlook (or pywin32) is "
                "not reachable. Help > Diagnose Outlook... reports which "
                "step in the chain is failing."
            )

        self.window.set_status_indicators(
            version=__version__,
            mic_label=f"Mic: {_short_device_label(mic)}",
            mic_tooltip=f"Microphone device: {mic}",
            loopback_label=f"System audio: {_short_device_label(loopback)}",
            loopback_tooltip=f"System audio capture (loopback): {loopback}",
            calendar_label=cal_label,
            calendar_tooltip=cal_tooltip,
        )

    # ---- calendar integration ---------------------------------------------

    def _apply_calendar_config(self) -> None:
        """Start/stop/reconfigure the Outlook monitor to match self.config.calendar."""
        if outlook_calendar.OutlookCalendarMonitor is None:
            return  # PyQt6 missing in this runtime; integration disabled.
        want = self.config.calendar.watch_calendar
        if want and not outlook_calendar.is_available():
            # User asked to watch but pywin32/Outlook aren't reachable. Quiet log;
            # one-time warning the first time only -- avoid nagging on every reopen.
            if not getattr(self, "_calendar_unavailable_warned", False):
                log.info("Calendar watch enabled but pywin32 / Outlook unavailable.")
                self._calendar_unavailable_warned = True
            self._stop_calendar_monitor()
            return
        if not want:
            self._stop_calendar_monitor()
            return
        # Want it on; (re)create if missing or window changed.
        existing = self._calendar_monitor
        if existing is not None and existing.window_minutes == self.config.calendar.window_minutes:
            if not existing.is_running():
                existing.start()
            return
        self._stop_calendar_monitor()
        self._calendar_monitor = outlook_calendar.OutlookCalendarMonitor(
            calendar_state_path(),
            window_minutes=self.config.calendar.window_minutes,
            parent=self,
        )
        self._calendar_monitor.meeting_imminent.connect(self._on_meeting_imminent)
        self._calendar_monitor.start()

    def _stop_calendar_monitor(self) -> None:
        if self._calendar_monitor is not None:
            try:
                self._calendar_monitor.stop()
            except Exception:
                log.exception("calendar monitor stop failed")
            self._calendar_monitor = None

    def _on_meeting_imminent(self, info: MeetingInfo) -> None:
        try:
            local_start = info.start_time.strftime("%H:%M")
        except Exception:
            local_start = "soon"
        body_parts = [f"Starts at {local_start}"]
        if info.location:
            body_parts.append(info.location)
        body_parts.append("Click to create a session (recording does not auto-start).")
        self.tray.notify_meeting(
            info,
            title=f"Meeting: {info.subject}",
            body=" -- ".join(body_parts),
        )

    def _on_create_session_from_calendar(self, info: MeetingInfo) -> None:
        self._foreground_window()
        dialog = NewSessionDialog(
            retain_audio_default=self.config.audio.retain_audio_default,
            title_prefill=info.subject,
            prefill_note=(
                f"Pre-filled from your Outlook invite. Attendees + agenda will "
                f"appear in My Notes. Starts at "
                f"{info.start_time.strftime('%H:%M')}."
            ),
            calendar_meeting=info,
            allow_calendar_pick=False,
            parent=self.window,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        result = dialog.result_value()
        session = self.store.create_session(
            title=result.title, retain_audio=result.retain_audio
        )
        meeting = result.calendar_meeting or info
        self._seed_live_notes_from_meeting(session.id, meeting)
        self._refresh_session_list(select=session.id)
        self.window.status(
            f"Created session from calendar: {info.subject}", timeout_ms=5000
        )

    # ---- update checks ----------------------------------------------------

    def _auto_check_for_updates(self) -> None:
        """Silent weekly check on startup.

        Only nags the user via QMessageBox when there's a newer release.
        Network errors / private-repo 404s degrade to silent no-op.
        """
        try:
            result = updater_mod.check_for_updates()
        except Exception:
            log.exception("auto update check failed")
            return
        if result is None:
            return
        local, remote = result
        self.window.status(
            f"Update available: v{remote} (current v{local}). See Help > Upgrade.",
            timeout_ms=10000,
        )
        QMessageBox.information(
            self.window,
            "Update Available",
            f"A new version of Meeting Notetaker is available.\n\n"
            f"Current version: {local}\n"
            f"Latest version: {remote}\n\n"
            "Use Help > Upgrade... to install it.",
        )

    def _on_check_for_updates(self) -> None:
        """Manual Help > Check for Updates... path -- ignores the 7-day cooldown."""
        self.window.status("Checking for updates...", timeout_ms=4000)
        QApplication.processEvents()
        release = updater_mod.get_latest_release()
        if release is None:
            QMessageBox.information(
                self.window,
                "Check for Updates",
                "Could not check for updates.\n\n"
                "Possible reasons:\n"
                "  - No network connectivity\n"
                "  - The release feed is restricted (private repo)\n"
                "  - GitHub is unreachable from this network",
            )
            self.window.status("Ready", timeout_ms=2000)
            return
        remote = release["tag_name"]
        if updater_mod.is_newer_version(remote, __version__):
            choice = QMessageBox.question(
                self.window,
                "Update Available",
                f"A new version is available.\n\n"
                f"Current version: {__version__}\n"
                f"Latest version: {remote}\n\n"
                "Upgrade now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if choice == QMessageBox.StandardButton.Yes:
                self._on_upgrade()
        else:
            QMessageBox.information(
                self.window,
                "Check for Updates",
                f"You are running the latest version ({__version__}).",
            )
        self.window.status("Ready", timeout_ms=2000)

    def _on_upgrade(self) -> None:
        """Help > Upgrade... -- confirm + run UpgradeProgressDialog."""
        # Imported here so the static graph stays light when the dialog is
        # never opened.
        from .ui.upgrade_dialog import UpgradeProgressDialog

        release = updater_mod.get_latest_release()
        if release is None:
            QMessageBox.warning(
                self.window,
                "Upgrade",
                "Could not reach GitHub to fetch the latest release.",
            )
            return
        remote = release["tag_name"]
        if not updater_mod.is_newer_version(remote, __version__):
            QMessageBox.information(
                self.window,
                "Upgrade",
                f"You are already running the latest version ({__version__}).",
            )
            return
        confirm = QMessageBox.question(
            self.window,
            "Confirm Upgrade",
            f"Upgrade from {__version__} to {remote}?\n\n"
            "This downloads the new release and runs the build script "
            "(pyinstaller). The app needs to be restarted afterwards to "
            "pick up the new build.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        dialog = UpgradeProgressDialog(
            owner=updater_mod.DEFAULT_GITHUB_OWNER,
            repo=updater_mod.DEFAULT_GITHUB_REPO,
            parent=self.window,
        )
        dialog.exec()
        ok, message = dialog.result_summary()
        if not ok:
            return
        # Only offer Restart Now when we actually installed in place;
        # in dev (non-frozen) the new build is in dist/ and the user
        # has to move it manually, so a restart wouldn't pick up the
        # new code.
        if not updater_mod.is_frozen():
            return
        target = updater_mod.current_exe_path()
        if target is None:
            return
        choice = QMessageBox.question(
            self.window,
            "Restart to finish upgrade",
            "The upgrade is installed. Restart Meeting Notetaker now to "
            "load the new build?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if choice == QMessageBox.StandardButton.Yes:
            self._restart_app(target)

    def _restart_app(self, exe_path) -> None:
        """Launch the freshly installed exe and quit this process.

        On Windows a detached subprocess survives the parent exit; the
        new process opens its own window and re-acquires the single-
        instance lock once this one releases it. POSIX doesn't have a
        bundled .exe in this app's workflow, so the branch is included
        for completeness but is essentially unused.
        """
        import subprocess
        try:
            kwargs = {}
            if sys.platform.startswith("win"):
                # DETACHED_PROCESS so the child doesn't inherit our
                # console handles; CREATE_NEW_PROCESS_GROUP so a
                # Ctrl-C in a parent terminal doesn't kill it.
                kwargs["creationflags"] = (
                    getattr(subprocess, "DETACHED_PROCESS", 0)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                )
                kwargs["close_fds"] = True
            subprocess.Popen([str(exe_path)], cwd=str(exe_path.parent), **kwargs)
        except OSError as exc:
            log.exception("failed to launch new build at %s", exe_path)
            QMessageBox.warning(
                self.window,
                "Restart failed",
                f"Could not launch the new build:\n\n{exc}\n\n"
                "Quit and start the app manually to pick up the new "
                "version.",
            )
            return
        self.qt_app.quit()

    # ---- environment warnings ---------------------------------------------

    def _warn_if_store_python(self) -> None:
        if not sys.platform.startswith("win"):
            return
        try:
            from .audio.mic_recorder import _is_store_python
        except Exception:
            return
        if not _is_store_python():
            return
        QMessageBox.warning(
            self.window,
            "Microsoft Store Python detected",
            "You appear to be running Python from the Microsoft Store, which "
            "runs in an AppContainer sandbox that blocks microphone access at "
            "the OS level. Recording will fail with 'No input audio devices "
            "found' when you click Start.\n\n"
            "Fix: install Python from python.org (the standard installer), "
            "then rebuild the venv:\n\n"
            "    deactivate\n"
            "    Remove-Item -Recurse -Force .venv\n"
            "    py -3.12 -m venv .venv\n"
            "    .\\.venv\\Scripts\\Activate.ps1\n"
            "    pip install -r requirements-dev.txt\n\n"
            "Use Help -> Audio Devices... to confirm what PyAudio sees on this "
            "interpreter.",
        )

    # ---- crash recovery ---------------------------------------------------

    def _handle_crash_recovery(self) -> None:
        orphans = self.controller.recover_orphans()
        if not orphans:
            return
        titles = "\n".join(f"  - {s.title} ({s.state})" for s in orphans)
        QMessageBox.information(
            self.window,
            "Crash Recovery",
            "Found sessions left mid-recording from a previous run. They have been marked "
            "as 'error' so you can decide whether to keep the partial transcripts or delete "
            "them.\n\n" + titles,
        )

    # ---- misc -------------------------------------------------------------

    def _foreground_window(self) -> None:
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()


_STATUS_DEVICE_TRUNCATE_AT = 36


def _short_device_label(name: str) -> str:
    """Trim long device names so the status bar doesn't overflow."""
    if len(name) <= _STATUS_DEVICE_TRUNCATE_AT:
        return name
    return name[: _STATUS_DEVICE_TRUNCATE_AT - 1].rstrip() + "..."


def _set_windows_app_user_model_id() -> None:
    # Without this, the tray icon, taskbar grouping, and toast notifications
    # inherit the interpreter's AUMID -- "Python" when launched via python.exe.
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "AaronDodd.MeetingNotetaker"
        )
    except (OSError, AttributeError):
        pass


def main() -> int:
    _set_windows_app_user_model_id()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler(str(log_path()), encoding="utf-8"),
        ],
    )

    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName("Meeting Notetaker")
    qt_app.setApplicationDisplayName("Meeting Notetaker")
    qt_app.setOrganizationName("Aaron Dodd")
    qt_app.setOrganizationDomain("aarondodd.com")
    qt_app.setQuitOnLastWindowClosed(False)
    qt_app.setWindowIcon(app_icon())

    if not acquire_lock():
        QMessageBox.information(
            None,
            "Meeting Notetaker",
            "Another Meeting Notetaker instance is already running.\n"
            "Click its tray icon to bring the window forward.",
        )
        return 0

    app = MainApp(qt_app)
    try:
        return qt_app.exec()
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
