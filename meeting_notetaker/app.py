"""MainApp -- the top-level orchestrator that wires UI, controller, tray, and store.

main() is the entry point used by main.py and the pyinstaller spec.
"""
from __future__ import annotations

import logging
import secrets
import sys
import threading
from datetime import datetime, timezone
from typing import Optional

from PyQt6.QtCore import QObject, Qt, QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox

from .automation import messages as automation_messages
from .automation.bridge import Bridge, HandshakeState
from .automation.targets import get_target
from .controller import SessionController
from .diarization.persistence import (
    load_diarization,
    save_diarization,
    update_cluster_name,
)
from .diarization import user_voiceprint
from .diarization.encoder_prewarm import EncoderPrewarmThread
from .diarization.refiner import RefinementResult
from .diarization.store import open_speaker_store
from .integrations import audio_session_monitor, outlook_calendar
from .integrations.audio_session_monitor import MeetingAudioInfo
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
    gather_suggestions,
)
from .ui.tray import TrayIcon
from .utils import prompts as prompts_mod
from .utils import updater as updater_mod
from .utils.config import Config
from .utils.icons import app_icon
from .utils.live_notes import extract_section, parse_attendees, seed_body_with_calendar
from .utils.paths import (
    app_data_dir,
    audio_session_state_path,
    bridge_handshake_path,
    calendar_state_path,
    log_path,
)
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
        self._audio_monitor = None  # set lazily by _apply_audio_monitor_config
        self._encoder_prewarm: Optional[EncoderPrewarmThread] = None
        # Per-session capture-only overrides captured from NewSessionDialog.
        # Consumed at start_session time, then evicted. Sessions absent from
        # the dict fall back to the global config value.
        self._capture_only_overrides: dict[str, bool] = {}

        # Synthesis automation bridge. Listens on a loopback port for the
        # Chrome native-messaging host to connect; route inbound result/
        # status events to the matching in-flight synthesis. The bridge
        # runs whether or not the toggle is on -- letting it idle costs
        # nothing (a single bound TCP port), and starting it lazily
        # would mean the Verify button in the install wizard wouldn't
        # have anything to probe against the first time.
        self._inflight_syntheses: dict[str, str] = {}  # request_id -> session_id
        self._bridge_ready_state: bool = False
        self._pending_pings: dict[str, "threading.Event"] = {}
        self._bridge = Bridge(
            handshake_file=bridge_handshake_path(),
            on_message=self._on_bridge_message,
            on_connect=self._on_bridge_connect,
            on_disconnect=self._on_bridge_disconnect,
            app_version=__version__,
        )
        try:
            self._bridge.start()
        except OSError:
            log.exception("synthesis bridge failed to bind a loopback port")

        self._wire_signals()
        self._apply_user_name()
        self._apply_synthesis_automation()
        self._refresh_session_list()
        self._handle_crash_recovery()
        self._warn_if_store_python()
        self._apply_calendar_config()
        self._apply_audio_monitor_config()
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

        # Pre-warm the speaker-embedding encoder on a background thread.
        # The first batch refinement OR voice enrollment after a fresh
        # install otherwise blocks the UI for the ~22 MB ECAPA-TDNN
        # model download; doing it here means the model is ready (or
        # downloading visibly in the status bar) before the user
        # opens enrollment.
        if self.config.speakers.enabled:
            QTimer.singleShot(500, self._start_encoder_prewarm)

    def _apply_user_name(self) -> None:
        self.window.session_view.set_user_name(self.config.ui.user_name)

    def _apply_synthesis_automation(self) -> None:
        """Push the current setting state into the SessionView, swapping
        the Generate + Paste buttons for the single Send button or
        vice-versa. Called at startup and after Settings is closed."""
        self.window.session_view.set_automation_enabled(
            self.config.synthesis.automation_enabled,
            self.config.synthesis.llm_target,
        )

    # ---- synthesis automation: bridge callbacks ----------------------------

    def _on_bridge_connect(self, state: HandshakeState) -> None:
        log.info(
            "bridge: extension connected (id=%s host=%s)",
            state.extension_id,
            state.host_version,
        )
        self._bridge_ready_state = True

    def _on_bridge_disconnect(self) -> None:
        log.info("bridge: extension disconnected")
        self._bridge_ready_state = False

    def _on_bridge_message(self, msg: dict) -> None:
        """Inbound from the extension. Runs on the bridge's reader
        thread. Pongs are signaled inline (threading.Event is thread-
        safe) so a blocking _ping_extension on the main Qt thread
        doesn't deadlock; everything else bounces to the main thread
        via QTimer.singleShot."""
        if msg.get("type") == "pong":
            request_id = msg.get("request_id", "")
            evt = self._pending_pings.pop(request_id, None)
            if evt is not None:
                evt.set()
            return
        QTimer.singleShot(0, lambda: self._dispatch_bridge_message(msg))

    def _dispatch_bridge_message(self, msg: dict) -> None:
        msg_type = msg.get("type", "")
        request_id = msg.get("request_id", "")
        if msg_type == "status":
            session_id = self._inflight_syntheses.get(request_id, "")
            if session_id:
                event = msg.get("event", "")
                detail = msg.get("detail", "")
                label = self._format_status_event(event, detail)
                if label:
                    self.window.status(label, timeout_ms=4000)
            return
        if msg_type == "result":
            session_id = self._inflight_syntheses.pop(request_id, "")
            if not session_id:
                log.warning("result for unknown request %s", request_id)
                return
            markdown = msg.get("markdown", "")
            target = msg.get("target", "")
            self._handle_synthesis_result(session_id, markdown, target)
            return
        if msg_type == "error":
            session_id = self._inflight_syntheses.pop(request_id, "")
            code = msg.get("code", "unknown")
            detail = msg.get("detail", "")
            self._handle_synthesis_error(session_id, code, detail)
            return
        log.debug("ignored bridge message of type %r", msg_type)

    @staticmethod
    def _format_status_event(event: str, detail: str) -> str:
        labels = {
            automation_messages.STATUS_OPENING_TAB: "Synthesis: opening browser tab",
            automation_messages.STATUS_AWAITING_LOGIN: "Synthesis: waiting for sign-in",
            automation_messages.STATUS_PROXY_ACK_NEEDED:
                "Synthesis: click PROCEED in the browser to acknowledge AI use",
            automation_messages.STATUS_PROXY_ACK_CLEARED:
                "Synthesis: proxy ack cleared, continuing",
            automation_messages.STATUS_PASTING: "Synthesis: pasting prompt",
            automation_messages.STATUS_AWAITING_RESPONSE: "Synthesis: waiting for response",
            automation_messages.STATUS_RESPONSE_STREAMING: "Synthesis: response streaming",
            automation_messages.STATUS_DONE: "Synthesis: response received",
        }
        return labels.get(event, "")

    def _handle_synthesis_result(self, session_id: str, markdown: str, target: str) -> None:
        if not markdown.strip():
            QMessageBox.warning(
                self.window,
                "Synthesis Automation",
                "The extension returned an empty response. Try again, or "
                "fall back to the manual Generate / Paste flow by disabling "
                "automation in Settings.",
            )
            return
        try:
            archive_path = TranscriptStore(session_id).save_notes(
                markdown, archive_existing=True
            )
            self.store.update_session(session_id, has_notes=True)
        except OSError:
            log.exception("save_notes failed for %s", session_id)
            QMessageBox.critical(
                self.window,
                "Synthesis Automation",
                "Couldn't save the synthesis to disk; see the log for "
                "details.",
            )
            return
        # Reload the SessionView so the Synthesis tab shows the new body.
        sv = self.window.session_view
        if sv._session is not None and sv._session.id == session_id:
            self._on_session_selected(session_id)
        if archive_path:
            self.window.status(
                f"Synthesis received. Prior notes archived to {archive_path.name}",
                timeout_ms=8000,
            )
        else:
            self.window.status("Synthesis received.", timeout_ms=5000)

    def _handle_synthesis_error(self, session_id: str, code: str, detail: str) -> None:
        # Map known codes to friendlier messages.
        friendly = {
            automation_messages.ERR_NO_TAB:
                "Couldn't open a browser tab. Is Chrome running?",
            automation_messages.ERR_NOT_LOGGED_IN:
                "Couldn't find the chat composer. Are you signed in to the target LLM?",
            automation_messages.ERR_PASTE_FAILED:
                "Couldn't paste into the chat. The LLM page may have changed; "
                "fall back to manual Generate / Paste for this session.",
            automation_messages.ERR_TIMEOUT:
                "The response didn't finish in time. Check the browser tab "
                "to see if it's still streaming.",
            automation_messages.ERR_INTERSTITIAL_TIMEOUT:
                "The proxy interstitial didn't clear within the time window. "
                "Click PROCEED in the browser and try Send again.",
        }
        message = friendly.get(code, detail or "Unknown error")
        QMessageBox.warning(self.window, "Synthesis Automation", message)

    # ---- synthesis automation: send + ping ---------------------------------

    def _on_send_to_llm(self, session_id: str, target_key: str) -> None:
        session = self.store.get_session(session_id)
        if session is None:
            return
        if not self._bridge_ready_state:
            ans = QMessageBox.warning(
                self.window,
                "Synthesis Automation",
                "The Chrome extension is not connected to the app. "
                "Open the extension in Chrome (chrome://extensions) and "
                "ensure it's enabled, then try again.\n\nFall back to "
                "the manual Generate / Paste flow instead?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if ans == QMessageBox.StandardButton.Yes:
                self._on_generate_prompt(session_id)
            return
        try:
            target = get_target(target_key)
        except ValueError:
            QMessageBox.warning(
                self.window,
                "Synthesis Automation",
                f"Unknown LLM target {target_key!r}. Pick a valid target in Settings.",
            )
            return
        if not target.implemented:
            QMessageBox.information(
                self.window,
                "Synthesis Automation",
                f"{target.label} automation isn't implemented in this "
                "build. Switch the target to Claude.ai in Settings, or "
                "disable automation to use the manual flow.",
            )
            return

        store = TranscriptStore(session_id)
        transcript = store.read_transcript()
        if not transcript.strip():
            QMessageBox.information(
                self.window, "Synthesis Automation",
                "This session has no transcript yet.",
            )
            return
        self.window.session_view.flush_pending_live_notes()
        live_notes = store.read_live_notes()
        try:
            when = datetime.fromisoformat(
                session.created_at.replace("Z", "+00:00")
            ).astimezone()
        except ValueError:
            when = datetime.now().astimezone()

        # Render with the default template. Users who customize prompts
        # via the Generate dialog can pick a template there; the
        # automation flow uses the default to keep the Send path one-
        # click. (Per-target / per-session prompt picking is a v0.7
        # candidate.)
        templates = prompts_mod.list_templates()
        default = next(
            (t for t in templates if t.name == "default"),
            templates[0] if templates else None,
        )
        if default is None:
            QMessageBox.warning(
                self.window, "Synthesis Automation",
                "No prompt templates found in the prompts folder.",
            )
            return
        rendered = prompts_mod.render(
            default,
            session_title=session.title,
            session_date=when,
            transcript=transcript,
            live_notes=live_notes,
            user_name=self.config.ui.user_name,
        )

        request_id = secrets.token_hex(8)
        self._inflight_syntheses[request_id] = session_id
        msg = automation_messages.SynthesizeRequest(
            request_id=request_id,
            target=target_key,
            prompt=rendered,
            new_chat=True,
        )
        if not self._bridge.send(msg):
            self._inflight_syntheses.pop(request_id, None)
            QMessageBox.warning(
                self.window, "Synthesis Automation",
                "Couldn't reach the extension. Try Reconnect from the "
                "extension popup, or fall back to manual Generate / Paste.",
            )
            return
        self.window.status(
            f"Sent to {target.label}. The response will land in the Synthesis tab.",
            timeout_ms=6000,
        )

    def _ping_extension(self, timeout_sec: float) -> bool:
        """Used by the Settings install wizard's Verify step. Returns
        True if the extension is reachable within the timeout.

        Two phases:

        1. Wait for the bridge to report a connected peer. The
           extension's connectNative() doesn't fire until either Chrome
           starts the extension, the alarm-based retry ticks (every
           minute), or the user clicks Reconnect in the extension
           popup. If the user clicked Verify before the extension's
           service worker had time to reconnect, we sit here until
           one of those events lands a peer.

        2. Once connected, send a ping and wait for a pong. Catches
           the case where the bridge has a peer but the per-host
           protocol is broken (mismatched extension ID, etc.).

        Runs on the Qt main thread. Uses a QEventLoop with a polling
        QTimer so paints and other event-loop work continue during
        the wait -- a 60-second ``threading.Event.wait()`` would
        freeze the install wizard window."""
        from PyQt6.QtCore import QEventLoop  # noqa: PLC0415

        # Phase 1: wait for peer.
        deadline_ms = int(timeout_sec * 1000)
        if not self._bridge.is_connected:
            loop = QEventLoop()
            connect_poll = QTimer()
            connect_poll.setInterval(100)
            connect_poll.timeout.connect(
                lambda: self._bridge.is_connected and loop.quit()
            )
            deadline = QTimer()
            deadline.setSingleShot(True)
            deadline.setInterval(deadline_ms)
            deadline.timeout.connect(loop.quit)
            connect_poll.start()
            deadline.start()
            loop.exec()
            connect_poll.stop()
            deadline.stop()
        if not self._bridge.is_connected:
            return False

        # Phase 2: ping/pong probe.
        request_id = f"ping-{secrets.token_hex(4)}"
        evt = threading.Event()
        self._pending_pings[request_id] = evt
        ping = automation_messages.PingRequest(request_id=request_id)
        if not self._bridge.send(ping):
            self._pending_pings.pop(request_id, None)
            return False

        # Use a short window for the actual pong (the connection is
        # already established, so a pong should come back in well
        # under a second).
        loop = QEventLoop()
        poll = QTimer()
        poll.setInterval(50)
        poll.timeout.connect(lambda: evt.is_set() and loop.quit())
        pong_deadline = QTimer()
        pong_deadline.setSingleShot(True)
        pong_deadline.setInterval(5000)
        pong_deadline.timeout.connect(loop.quit)
        poll.start()
        pong_deadline.start()
        loop.exec()
        poll.stop()
        pong_deadline.stop()

        self._pending_pings.pop(request_id, None)
        return evt.is_set()

    # ---- wiring ------------------------------------------------------------

    def _wire_signals(self) -> None:
        self.window.new_session_requested.connect(self._on_new_session)
        self.window.open_settings_requested.connect(self._on_settings)
        self.window.open_devices_dialog_requested.connect(self._on_devices)
        self.window.open_outlook_diagnostic_requested.connect(self._on_outlook_diagnostic)
        self.window.open_log_viewer_requested.connect(self._on_log_viewer)
        self.window.open_dependency_check_requested.connect(self._on_dependency_check)
        self.window.check_for_updates_requested.connect(self._on_check_for_updates)
        self.window.upgrade_requested.connect(self._on_upgrade)
        self.window.quit_requested.connect(self.qt_app.quit)
        self.window.session_selected.connect(self._on_session_selected)
        self.window.delete_sessions_requested.connect(self._on_delete_sessions)
        self.window.rename_session_requested.connect(self._on_rename_session)
        self.window.edit_session_timestamp_requested.connect(self._on_edit_session_timestamp)

        self.tray.open_main_window.connect(self._foreground_window)
        self.tray.new_session_requested.connect(self._on_new_session)
        self.tray.pause_requested.connect(self.controller.pause_session)
        self.tray.resume_requested.connect(self.controller.resume_session)
        self.tray.stop_requested.connect(self.controller.stop_session)
        self.tray.settings_requested.connect(self._on_settings)
        self.tray.quit_requested.connect(self.qt_app.quit)
        self.tray.meeting_notification_clicked.connect(self._on_create_session_from_calendar)
        self.tray.audio_notification_clicked.connect(self._on_create_session_from_audio)

        sv = self.window.session_view
        sv.start_clicked.connect(self._on_start_clicked)
        sv.pause_clicked.connect(lambda _sid: self.controller.pause_session())
        sv.resume_clicked.connect(lambda _sid: self.controller.resume_session())
        sv.stop_clicked.connect(lambda _sid: self.controller.stop_session())
        sv.generate_prompt_clicked.connect(self._on_generate_prompt)
        sv.paste_notes_clicked.connect(self._on_paste_notes)
        sv.send_to_llm_clicked.connect(self._on_send_to_llm)
        sv.copy_tab_clicked.connect(self._on_copy_tab)
        sv.live_notes_changed.connect(self._on_live_notes_changed)
        sv.synthesis_notes_changed.connect(self._on_synthesis_notes_changed)
        sv.review_speakers_clicked.connect(self._on_review_speakers)
        sv.retain_audio_toggled.connect(self.controller.set_retain_audio)
        # Click-to-tag attendee sidebar. The session-view passes its own
        # session_id; we forward to controller.tag_speaker which captures
        # the WAV-aligned timestamp and persists.
        sv.tag_speaker_clicked.connect(
            lambda _sid, name: self.controller.tag_speaker(name)
        )
        sv.remove_last_tag_clicked.connect(
            lambda _sid, name: self.controller.remove_last_speaker_tag(name)
        )

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
        self.controller.speaker_tags_changed.connect(self._on_speaker_tags_changed)
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
        live_notes_body = store.read_live_notes()
        self.window.session_view.set_session(
            session,
            transcript=store.read_transcript(),
            notes=store.read_notes(),
            previous_notes_paths=store.list_previous_notes(),
            live_notes=live_notes_body,
        )
        # Seed the click-to-tag sidebar from the live_notes '# Attendees'
        # section. The sidebar is hidden unless the session is recording,
        # but seeding now means it shows up populated the moment Start
        # is clicked.
        self.window.session_view.set_attendee_names(
            parse_attendees(live_notes_body)
        )
        # If the user reselected a session that's still actively being
        # recorded (back-to-back-session scenario), surface any tag counts
        # the controller already collected.
        active = getattr(self.controller, "active_session", None)
        if active is not None and active.id == session_id:
            store = self.controller._tag_stores.get(session_id)
            if store is not None:
                self.window.session_view.set_speaker_tag_counts(store.counts())

    # ---- session lifecycle handlers ---------------------------------------

    def _on_new_session(self) -> None:
        dialog = NewSessionDialog(
            retain_audio_default=self.config.audio.retain_audio_default,
            capture_only_default=self.config.transcription.capture_only_mode,
            parent=self.window,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        result = dialog.result_value()
        session = self.store.create_session(
            title=result.title,
            retain_audio=result.retain_audio,
        )
        if result.capture_only_override is not None:
            self._capture_only_overrides[session.id] = result.capture_only_override
        if result.calendar_meeting is not None:
            self._align_created_at_to_meeting(session.id, result.calendar_meeting)
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

    def _align_created_at_to_meeting(
        self, session_id: str, info: MeetingInfo
    ) -> None:
        """Set the session's created_at to the meeting's start time.

        MeetingInfo.start_time is a naive local datetime (per the Outlook
        COM converter); the session store keeps timestamps as UTC ISO. Local
        -> UTC conversion uses the host's current local offset.
        """
        try:
            local_tz = datetime.now().astimezone().tzinfo
            aware_local = info.start_time.replace(tzinfo=local_tz)
            iso = aware_local.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        except (AttributeError, ValueError, OSError):
            log.exception("failed to align created_at to meeting start time")
            return
        self.store.update_session(session_id, created_at=iso)

    def _on_start_clicked(self, session_id: str) -> None:
        session = self.store.get_session(session_id)
        if session is None:
            return
        # Preload the Whisper model under a progress dialog so the UI doesn't
        # freeze during first-run download or model swap. If capture-only mode
        # is on, live workers don't load the model -- but the batch pass at
        # stop time still needs it, so we preload either way.
        size = self.config.transcription.model_size
        cpu_threads = self.config.transcription.resolved_cpu_threads()
        num_workers = self.config.transcription.num_workers
        # Pop the per-session override (if any) so the next Start on
        # this session falls back to the global setting.
        capture_override = self._capture_only_overrides.pop(session.id, None)
        if model_manager.current_size() == size:
            self.controller.start_session(
                session, capture_only_override=capture_override
            )
            return

        run_with_progress(
            self.window,
            title="Loading Whisper model",
            message=(
                f"Loading the '{size}' model (this can take a minute on first run "
                "while the weights download). The app will respond again once it's ready."
            ),
            fn=model_manager.get_model,
            kwargs={
                "size": size,
                "cpu_threads": cpu_threads,
                "num_workers": num_workers,
            },
            on_success=lambda _model: self.controller.start_session(
                session, capture_only_override=capture_override
            ),
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
            when = datetime.fromisoformat(
                session.created_at.replace("Z", "+00:00")
            ).astimezone()
        except ValueError:
            when = datetime.now().astimezone()
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
        # Keep the click-to-tag sidebar in sync with whatever the user
        # currently has under '# Attendees'. The widget only re-renders
        # when the parsed list actually changes.
        sv = self.window.session_view
        if sv._session is not None and sv._session.id == session_id:
            sv.set_attendee_names(parse_attendees(body))

    def _on_speaker_tags_changed(self, session_id: str, counts: dict[str, int]) -> None:
        sv = self.window.session_view
        if sv._session is not None and sv._session.id == session_id:
            sv.set_speaker_tag_counts(counts)

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
        # Surface what landed via the status bar; the Label Unknown
        # Speakers dialog no longer auto-pops -- the user clicks the
        # session view's Review Speakers button when they're ready to
        # label / correct. Tagged-cluster centroids were already fed to
        # the SpeakerStore inside the controller's _on_refinement_done
        # via _self_improve_store_from_tagged_clusters, so no follow-up
        # write is needed here for those.
        cluster_count = len(result.clusters)
        unknown_count = len(result.unknown_cluster_ids)
        tagged_count = sum(
            1 for c in result.clusters if getattr(c, "was_user_tagged", False)
        )
        if cluster_count == 0:
            return
        msg_parts = [f"Identified {cluster_count} speaker(s)"]
        if tagged_count:
            msg_parts.append(f"{tagged_count} from in-meeting tags")
        if unknown_count:
            msg_parts.append(
                f"{unknown_count} unlabeled -- click Review Speakers to label"
            )
        self.window.status("; ".join(msg_parts) + ".", timeout_ms=10000)

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
        # Speaker library may have grown / shrunk; refresh the indicator.
        self._refresh_status_indicators()

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

        # Notes + Synthesis tabs are Markdown bodies that may reference
        # local images. Run them through the multi-format helper so an
        # HTML-aware paste target (Notion, Word, browsers) gets inline
        # image bytes via data: URIs. Plain-text targets still receive
        # the original Markdown. Transcript / Previous tabs are plain
        # text and skip the markdown render.
        if tab_id in ("live_notes", "notes"):
            from .utils.clipboard import copy_markdown_with_images
            from .models.transcript import TranscriptStore
            try:
                store = TranscriptStore(session_id)
                copy_markdown_with_images(text, store.session_dir)
            except Exception as exc:
                log.exception("multi-format clipboard copy failed; falling back to plain text")
                try:
                    pyperclip.copy(text)
                except Exception as exc2:
                    log.exception("plain-text fallback copy also failed")
                    QMessageBox.warning(
                        self.window, f"Copy {label}", f"Clipboard copy failed: {exc2}"
                    )
                    return
        else:
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

    # ---- rename ------------------------------------------------------------

    def _on_rename_session(self, session_id: str, new_title: str) -> None:
        new_title = (new_title or "").strip()
        if not new_title:
            return
        session = self.store.get_session(session_id)
        if session is None or session.title == new_title:
            return
        self.store.update_session(session_id, title=new_title)
        # Preserve the user's current selection across the refresh.
        self._refresh_session_list(select=session_id)
        # Update the right pane in place if the renamed session is the
        # one currently displayed; avoids a full set_session() reload that
        # would discard in-flight live notes / synthesis edits.
        sv = self.window.session_view
        if sv._session is not None and sv._session.id == session_id:
            sv.set_title(new_title)
        self.window.status(f"Renamed to '{new_title}'", timeout_ms=4000)

    # ---- edit timestamp ----------------------------------------------------

    def _on_edit_session_timestamp(
        self, session_id: str, new_created_at_iso: str
    ) -> None:
        new_created_at_iso = (new_created_at_iso or "").strip()
        if not new_created_at_iso:
            return
        session = self.store.get_session(session_id)
        if session is None or session.created_at == new_created_at_iso:
            return
        self.store.update_session(session_id, created_at=new_created_at_iso)
        self._refresh_session_list(select=session_id)
        # Mirror the rename path: keep the in-memory session.created_at on the
        # right pane in sync so synthesis prompts + printing pick up the new
        # date without a full set_session() reload.
        sv = self.window.session_view
        if sv._session is not None and sv._session.id == session_id:
            sv.set_created_at(new_created_at_iso)
        self.window.status("Session timestamp updated.", timeout_ms=4000)

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

    def _on_dependency_check(self) -> None:
        from .ui.dependency_check_dialog import DependencyCheckDialog
        if getattr(self, "_dep_check", None) is None:
            self._dep_check = DependencyCheckDialog(parent=self.window)
        else:
            # Re-run so the report reflects the current venv state.
            self._dep_check._run()
        self._dep_check.show()
        self._dep_check.raise_()
        self._dep_check.activateWindow()

    def _on_settings(self) -> None:
        dialog = SettingsDialog(
            self.config,
            parent=self.window,
            ping_extension=self._ping_extension,
        )
        accepted = dialog.exec() == dialog.DialogCode.Accepted
        # The Manage Speakers sub-dialog can mutate the store regardless
        # of whether the parent Settings dialog is accepted or canceled,
        # so refresh the status indicator on the way out either way.
        if not accepted:
            self._refresh_status_indicators()
            return
        errors = self.config.validate()
        if errors:
            QMessageBox.warning(self.window, "Settings", "\n".join(errors))
            return
        self.config.save()
        self._apply_user_name()
        self._apply_calendar_config()
        self._apply_audio_monitor_config()
        self._apply_synthesis_automation()
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

        speakers_label, speakers_tooltip = self._speakers_indicator()
        voice_label, voice_tooltip = self._voice_indicator()
        detect_label, detect_tooltip = self._detection_indicator()
        self.window.set_status_indicators(
            version=__version__,
            mic_label=f"Mic: {_short_device_label(mic)}",
            mic_tooltip=f"Microphone device: {mic}",
            loopback_label=f"System audio: {_short_device_label(loopback)}",
            loopback_tooltip=f"System audio capture (loopback): {loopback}",
            calendar_label=cal_label,
            calendar_tooltip=cal_tooltip,
            speakers_label=speakers_label,
            speakers_tooltip=speakers_tooltip,
            voice_label=voice_label,
            voice_tooltip=voice_tooltip,
            detect_label=detect_label,
            detect_tooltip=detect_tooltip,
        )

    def _speakers_indicator(self) -> tuple[str, str]:
        """Return (label, tooltip) for the status-bar speakers segment.

        Hidden (empty label) when speaker ID is disabled or no speakers
        are stored yet -- "Speakers: 0" would just be noise on a fresh
        install. The tooltip lists up to 8 stored names so the user can
        sanity-check the library without opening Settings.
        """
        if not self.config.speakers.enabled:
            return "", ""
        try:
            store = open_speaker_store()
            try:
                records = store.list_all()
            finally:
                store.close()
        except Exception:
            log.exception("speakers indicator: store unreadable")
            return "", ""
        if not records:
            return "", ""
        names = [r.name for r in records]
        preview = ", ".join(names[:8])
        if len(names) > 8:
            preview += f" (+{len(names) - 8} more)"
        tooltip = f"Known speakers ({len(names)}): {preview}"
        return f"Speakers: {len(names)}", tooltip

    def _detection_indicator(self) -> tuple[str, str]:
        """Return (label, tooltip) for the ad-hoc-meeting-detect indicator.

        Hidden (empty label) when detection is off; surfaces the running /
        unavailable / idle state to mirror the Calendar indicator pattern.
        """
        if not self.config.detection.enabled:
            return "", ""
        if not audio_session_monitor.is_available():
            return (
                "Detect: pycaw unavailable",
                "Ad-hoc meeting detection is enabled, but pycaw / psutil "
                "are not importable. Install them in this environment to "
                "use this feature (Windows wheels)."
            )
        running = self._audio_monitor is not None and self._audio_monitor.is_running()
        if running:
            allowlist_size = len(self.config.detection.app_allowlist)
            return (
                "Detect: watching",
                f"Watching system audio for {allowlist_size} known meeting "
                f"app(s); prompting after audio sustains "
                f"{self.config.detection.min_duration_sec}s.",
            )
        return (
            "Detect: idle",
            "Ad-hoc meeting detection is enabled but the monitor is not "
            "running. Try toggling it off and on in Settings.",
        )

    def _voice_indicator(self) -> tuple[str, str]:
        """Return (label, tooltip) for the user-voice enrollment indicator.

        Only surfaces when speaker ID is enabled but no usable voiceprint
        is on disk -- the goal is to remind the user there's a setup
        step they haven't completed. A voiceprint recorded under a
        previous encoder (e.g. Resemblyzer before the v0.5 ECAPA swap)
        is treated as not enrolled because its dim is incompatible with
        the current encoder's output; the user must re-record.
        """
        if not self.config.speakers.enabled:
            return "", ""
        try:
            if user_voiceprint.load() is not None:
                return "", ""
        except Exception:
            log.exception("voice indicator: voiceprint check failed")
            return "", ""
        return (
            "Voice: not enrolled",
            "No voice sample has been recorded. Settings > Speaker "
            "Identification > Record voice sample lets the refiner "
            "tell your microphone from system-audio bleed.",
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
        self._align_created_at_to_meeting(session.id, meeting)
        self._seed_live_notes_from_meeting(session.id, meeting)
        self._refresh_session_list(select=session.id)
        self.window.status(
            f"Created session from calendar: {info.subject}", timeout_ms=5000
        )

    # ---- ad-hoc meeting auto-detect ---------------------------------------

    def _apply_audio_monitor_config(self) -> None:
        """Start/stop/reconfigure the audio session monitor to match config."""
        if audio_session_monitor.AudioSessionMonitor is None:
            return  # PyQt6 missing in this runtime; integration disabled.
        want = self.config.detection.enabled
        if want and not audio_session_monitor.is_available():
            if not getattr(self, "_audio_monitor_unavailable_warned", False):
                log.info(
                    "Audio session detection enabled but pycaw / psutil "
                    "unavailable on this host."
                )
                self._audio_monitor_unavailable_warned = True
            self._stop_audio_monitor()
            return
        if not want:
            self._stop_audio_monitor()
            return
        existing = self._audio_monitor
        same_config = (
            existing is not None
            and existing.min_duration_sec == self.config.detection.min_duration_sec
            and existing.cooldown_minutes == self.config.detection.cooldown_minutes
            and existing.allowlist == list(self.config.detection.app_allowlist)
        )
        if same_config:
            if not existing.is_running():
                existing.start()
            return
        self._stop_audio_monitor()
        self._audio_monitor = audio_session_monitor.AudioSessionMonitor(
            audio_session_state_path(),
            allowlist=list(self.config.detection.app_allowlist),
            min_duration_sec=self.config.detection.min_duration_sec,
            cooldown_minutes=self.config.detection.cooldown_minutes,
            is_recording=self._is_session_active,
            parent=self,
        )
        self._audio_monitor.meeting_audio_detected.connect(
            self._on_meeting_audio_detected
        )
        self._audio_monitor.start()

    def _stop_audio_monitor(self) -> None:
        if self._audio_monitor is not None:
            try:
                self._audio_monitor.stop()
            except Exception:
                log.exception("audio monitor stop failed")
            self._audio_monitor = None

    def _is_session_active(self) -> bool:
        """True when the live recording engine is in use -- the monitor
        uses this to suppress prompts during an existing call.

        Sessions in post-Stop processing (STATE_PROCESSING) do not block
        a new recording, so they do not suppress detection prompts either.
        """
        active = self.controller.active_session
        if active is None:
            return False
        return active.state in (STATE_RECORDING, STATE_PAUSED)

    def _on_meeting_audio_detected(self, info: MeetingAudioInfo) -> None:
        self.tray.notify_audio_detected(
            info,
            title=f"{info.app_label} call detected",
            body=(
                f"{info.app_label} has been playing audio for "
                f"{int(info.sustained_seconds)}s. Click to start a session."
            ),
        )

    def _on_create_session_from_audio(self, info: MeetingAudioInfo) -> None:
        self._foreground_window()
        prefill_title = f"{info.app_label} - {info.first_detected_at.strftime('%Y-%m-%d %H:%M')}"
        dialog = NewSessionDialog(
            retain_audio_default=self.config.audio.retain_audio_default,
            title_prefill=prefill_title,
            prefill_note=(
                f"Detected active audio from {info.app_label}. "
                "Rename the session if you'd like, then click OK to "
                "create it -- recording starts only when you click Start."
            ),
            parent=self.window,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        result = dialog.result_value()
        session = self.store.create_session(
            title=result.title, retain_audio=result.retain_audio
        )
        self._refresh_session_list(select=session.id)
        self.window.status(
            f"Created session from {info.app_label} audio.", timeout_ms=5000
        )

    # ---- encoder pre-warm -------------------------------------------------

    def _start_encoder_prewarm(self) -> None:
        """Fire the background encoder pre-warm thread.

        No-ops if PyQt6 is missing in the runtime (unlikely in this
        codepath) or if a prewarm is already in flight. Designed to be
        safe to call multiple times; subsequent calls reuse the
        already-loaded model.
        """
        if EncoderPrewarmThread is None:
            return
        if self._encoder_prewarm is not None and self._encoder_prewarm.isRunning():
            return
        self._encoder_prewarm = EncoderPrewarmThread(parent=self)
        self._encoder_prewarm.download_started.connect(
            self._on_encoder_download_started
        )
        self._encoder_prewarm.finished_ok.connect(self._on_encoder_ready)
        self._encoder_prewarm.failed.connect(self._on_encoder_failed)
        self._encoder_prewarm.start()

    def _on_encoder_download_started(self) -> None:
        self.window.status(
            "Downloading speaker identification model (~22 MB, first run only)...",
            timeout_ms=0,
        )

    def _on_encoder_ready(self) -> None:
        # Only clear the status if the user hasn't navigated away to a
        # more recent message. A short "Ready" with a timeout lets the
        # next status update overwrite it naturally.
        self.window.status("Speaker identification model ready.", timeout_ms=4000)

    def _on_encoder_failed(self, message: str) -> None:
        log.warning("encoder pre-warm failed: %s", message)
        self.window.status(
            "Speaker identification model failed to load; see log for details.",
            timeout_ms=8000,
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
