"""MainApp -- the top-level orchestrator that wires UI, controller, tray, and store.

main() is the entry point used by main.py and the pyinstaller spec.
"""
from __future__ import annotations

import logging
import secrets
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QEvent, QObject, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication, QMessageBox

from .automation import messages as automation_messages
from .automation.bridge import Bridge, HandshakeState
from .automation.targets import get_target
from .utils.chrome_process import (
    SynthesisConnectionState,
    is_chrome_running,
    launch_chrome,
)
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
from .models.classification import (
    ClassificationStore,
    SOURCE_ATTENDEE_LIST,
    SOURCE_AUTO,
    SOURCE_MANUAL,
    SessionClassification,
)
from .utils.contact_resolution import (
    display_name_for_email,
    resolve_attendees_batch,
)
from .models.highlights import HighlightSet, HighlightsStore
from .models.search_index import SearchIndex
from .ui.classification_navigator import (
    VIEW_ALL, VIEW_BY_SERIES, VIEW_BY_PERSON, VIEW_BY_TOPIC,
)
from .ui.address_book_dialog import AddressBookDialog
from .ui.devices_dialog import DevicesDialog
from .ui.main_window import MainWindow
from .ui.manage_classification_dialog import ManageClassificationDialog
from .ui.search_dialog import SearchDialog, SessionSummary
from .ui.status_indicators import SegmentState
from .ui.new_session_dialog import NewSessionDialog
from .ui.progress import run_with_progress
from .ui.prompt_dialog import GeneratePromptDialog, PasteNotesDialog
from .ui.settings_dialog import SettingsDialog
from .utils.topic_extractor import extract_topics
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
from .utils.main_loop_watchdog import MainLoopWatchdog
from .utils.paths import (
    app_data_dir,
    audio_session_state_path,
    bridge_handshake_path,
    calendar_state_path,
    log_path,
    rotate_log_on_launch,
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


def _upgrade_path_blurb() -> str:
    """Phrasing reused by the auto-check and Help > Check for Updates dialogs.

    Both surfaces need to explain that the built-in updater only handles
    installer-managed installs; source / portable users need to upgrade
    via whatever workflow they originally used.
    """
    return (
        "If you installed via the Inno Setup installer, choose Help > "
        "Upgrade... to download and apply the new installer silently.\n\n"
        "If you built your own .exe via pyinstaller or are running "
        "directly from source (python main.py), upgrade however you "
        "originally set it up (git pull + rebuild, or replace your "
        "portable .exe with the one attached to the GitHub release)."
    )


class MainApp(QObject):
    # Cross-thread inbound message from the synthesis bridge. The bridge
    # reader runs on its own worker thread; we emit this signal from
    # that thread to bounce the message onto the Qt main thread (Qt's
    # auto-connection promotes a cross-thread emit to QueuedConnection,
    # which is what we want). Replaces the QTimer.singleShot(0, lambda)
    # approach used in v1, which silently failed on Windows because
    # QTimer's slot was registered on the bridge worker thread (no Qt
    # event loop) and never fired (Aaron's 2026-05-22 repro: bridge
    # received messages but the result never reached the app).
    bridge_message_received = pyqtSignal(dict)
    # Same thread-safety concern for connect/disconnect callbacks --
    # they fire on the bridge's accept/reader threads and need to
    # bounce to the main thread before touching UI / triggering a poll.
    bridge_state_changed = pyqtSignal()

    def __init__(self, qt_app: QApplication) -> None:
        super().__init__()
        self.qt_app = qt_app
        # Detect main-event-loop stalls (issue #31). When Windows
        # reports the app as "Not Responding", we now get a full
        # all-thread stack dump in meeting_notetaker.log so the next
        # freeze leaves a forensic trail. Start the watchdog as early
        # as possible so even cold-start hitches get captured.
        self._loop_watchdog = MainLoopWatchdog(
            stall_threshold_ms=750,
            log_file_path=log_path(),
            parent=self,
        )
        self._loop_watchdog.start()
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
        # One-shot worker for GitHub release checks (issue #34). Set
        # while a check is in flight, cleared by _retire_update_check_worker
        # on finished. Two starts in flight at once would race; guarded
        # in _auto_check_for_updates / _on_check_for_updates.
        self._update_check_worker: Optional[QThread] = None
        # Periodic search-index sweep worker (issue #37). The 30s
        # QTimer dispatches a one-shot scan; a second tick while a
        # prior scan is still running is skipped rather than queued.
        self._search_scan_worker: Optional[QThread] = None
        # Async session-content loader (issue #39). Bumped on every
        # _on_session_selected call; results whose captured generation
        # doesn't match are dropped (rapid session-switch cancels
        # the in-flight load). The worker reads transcript / live
        # notes / synthesis notes / archive list / highlights /
        # template list off-thread; the slot applies them via the
        # piecemeal session_view setters when the user-visible
        # session still matches.
        self._session_load_generation: int = 0
        self._session_content_worker: Optional[QThread] = None
        self._session_currently_loading: Optional[str] = None
        # Per-session capture-only overrides captured from NewSessionDialog.
        # Consumed at start_session time, then evicted. Sessions absent from
        # the dict fall back to the global config value.
        self._capture_only_overrides: dict[str, bool] = {}
        # Per-session screen-capture region in absolute screen coords.
        # Populated once the user accepts the region picker; cleared on
        # Stop Screen Capture or when the recording ends. Indexed by
        # session id so multi-session-in-flight (a v0.6 feature) still
        # routes captures to the right session.
        self._screen_capture_regions: dict[str, tuple[int, int, int, int]] = {}
        # Auto-capture state. Keyed by session id; only present
        # while auto-capture is armed AND running for that session.
        # _auto_capture_baseline_hash holds the dHash of the most-
        # recently-kept screenshot (manual or auto). New auto-captures
        # compare against it; matches within the threshold get
        # deleted, mismatches keep the file and update the baseline.
        self._auto_capture_timers: dict[str, QTimer] = {}
        self._auto_capture_baseline_hash: dict[str, int] = {}
        # Persistent on-screen outline showing the armed region. There
        # is one overlay at a time (only one session captures at a
        # time); _arm_screen_capture_overlay creates it, _disarm tears
        # it down.
        self._armed_region_overlay = None
        # One AudioPlayer instance, reused across sessions. Lazily
        # constructed on first use so a workspace without retained
        # audio never pays the sounddevice / PyAV import cost.
        self._audio_player = None
        # Track which session is currently loaded into the player so
        # we don't reload identical files on every selection.
        self._player_loaded_session_id: Optional[str] = None

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
        # Three-state synthesis connection: Chrome-running x bridge-
        # connected. The poll runs every 5s, the keep-alive ping runs
        # every 25s but only when state==RUNNING_CONNECTED. Both timers
        # start in _wire_signals so they exist before any UI shows.
        self._synth_state = SynthesisConnectionState.NOT_RUNNING
        self._synth_poll_timer = QTimer(self)
        self._synth_poll_timer.setInterval(5000)
        self._synth_poll_timer.timeout.connect(self._poll_synthesis_state)
        self._synth_keepalive_timer = QTimer(self)
        self._synth_keepalive_timer.setInterval(25000)
        self._synth_keepalive_timer.timeout.connect(self._send_keepalive_ping)
        # Bridge worker thread emits these signals; the connects below
        # auto-promote to QueuedConnection so the slots run on the
        # main thread regardless of which thread emitted.
        self.bridge_message_received.connect(self._dispatch_bridge_message)
        self.bridge_state_changed.connect(self._poll_synthesis_state)

        # Cross-session search index (FTS5). Created lazily so a corrupt
        # search.db doesn't keep the app from launching; opened on first
        # use and at the startup stale-scan below.
        try:
            self.search_index: Optional[SearchIndex] = SearchIndex()
        except Exception:
            log.exception("SearchIndex open failed; search disabled this session")
            self.search_index = None
        # Classification store (series / people / topics). Same
        # cold-start safety as search: a corrupt classification.db
        # disables the feature for the session but doesn't block
        # launch. Active filter state lives on MainApp because
        # set_sessions has to apply it client-side after every
        # store query.
        try:
            self.classification: Optional[ClassificationStore] = ClassificationStore()
        except Exception:
            log.exception("ClassificationStore open failed; chips disabled this session")
            self.classification = None
        # Phase 2 migration: link existing SpeakerStore rows to
        # Contacts in the classification store. Runs once per launch
        # (cheap when there's nothing to link). A failure here is
        # logged but non-fatal -- the user can re-trigger via Help
        # > Debug if it skipped a batch.
        if self.classification is not None:
            try:
                self._migrate_speakers_to_contacts()
            except Exception:
                log.exception("speakers -> contacts migration failed")
        self._classification_filter_view: str = VIEW_ALL
        self._classification_filter_value: Optional[int] = None
        self._search_dialog: Optional[SearchDialog] = None
        # Periodic catch-up: a 30s timer walks stale sessions and re-
        # indexes them. Belt-and-suspenders against missed explicit
        # hooks (e.g. a save path we haven't wired) so the user
        # never sees "I just edited X and search can't find it" for
        # more than 30 seconds.
        self._search_index_scan_timer = QTimer(self)
        self._search_index_scan_timer.setInterval(30_000)
        self._search_index_scan_timer.timeout.connect(self._scan_search_index_stale)

        # Persist window geometry + splitter state at app exit.
        # aboutToQuit fires regardless of how the app shuts down
        # (X button, menu Quit, tray Quit, Cmd+Q, OS signal) so this
        # is the single canonical save point. Single write -- no
        # debouncing needed for one-shot exit.
        self.qt_app.aboutToQuit.connect(self._persist_window_layout)
        # Stop the watchdog thread before the app tears down so its
        # final state lands in the log cleanly rather than getting
        # interrupted mid-write at process exit.
        self.qt_app.aboutToQuit.connect(self._loop_watchdog.stop)
        # Wait for the encoder prewarm to finish if it's still in
        # flight (issue #36). The download can take 30+s on a fresh
        # install; without this wait, quitting during that window
        # destroys a running QThread = process abort.
        self.qt_app.aboutToQuit.connect(self._retire_encoder_prewarm)
        # Backup-on-close + restore-after-close hooks (#67). Both run
        # last in the shutdown sequence so live connections are already
        # drained when the swap / snapshot fires.
        self._pending_restore_path: Optional[Path] = None
        # Background backup worker + last-error capture for the idle
        # scheduler (#67). Manual backup uses a separate modal-progress
        # path so we don't share state here.
        self._backup_worker: Optional[QThread] = None
        self._backup_worker_error: Optional[str] = None
        # Idle-trigger backup state (#67). The application event
        # filter (see eventFilter) bumps _last_input_at on any user
        # input; _backup_idle_timer polls every minute and fires the
        # snapshot when ``should_run_idle_backup`` returns True.
        import time as _time  # noqa: PLC0415
        self._last_input_at: float = _time.monotonic()
        self._backup_idle_timer = QTimer(self)
        self._backup_idle_timer.setInterval(60_000)  # 1 min poll
        self._backup_idle_timer.timeout.connect(self._maybe_fire_idle_backup)
        self._on_close_backup_ran = False
        self.qt_app.aboutToQuit.connect(self._run_backup_on_close)
        self.qt_app.aboutToQuit.connect(self._run_pending_restore)
        # Backup-in-progress close interception (#67). When the idle
        # scheduler kicked off a snapshot in a worker thread, clicking
        # the X / Quit drops into _handle_window_close which shows a
        # modal "Backup in progress..." dialog instead of tearing the
        # process down mid-write.
        self.window.set_close_handler(self._handle_window_close)
        # Application-wide event filter for idle-time tracking. We
        # only care about user input (key + mouse press/move) so the
        # filter is cheap; everything else falls through.
        self.qt_app.installEventFilter(self)
        if self.config.backup.schedule == "when_idle" \
                and (self.config.backup.folder or "").strip():
            self._backup_idle_timer.start()

        self._wire_signals()
        self._apply_user_name()
        self._apply_synthesis_automation()
        # Kick off the state poll immediately so the status bar shows
        # the right indicator on first paint, then settle into the
        # 5-second cadence.
        self._poll_synthesis_state()
        self._synth_poll_timer.start()
        self._synth_keepalive_timer.start()
        self._refresh_session_list()
        self._handle_crash_recovery()
        self._warn_if_store_python()
        self._apply_calendar_config()
        self._apply_audio_monitor_config()
        self._refresh_status_indicators()
        # Clean up the .old file left over from a prior in-place upgrade.
        self.window.show()
        self.tray.set_state("idle")

        # Weekly background check for a newer release on GitHub. Defer 2s
        # after show() so the network call never blocks the first paint.
        QTimer.singleShot(2000, self._auto_check_for_updates)

        # Startup stale-scan for the search index. Deferred so it
        # doesn't extend the first-paint critical path; once done,
        # the periodic catch-up timer takes over.
        QTimer.singleShot(1500, self._search_index_startup_scan)
        self._search_index_scan_timer.start()

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
        # Push the configured auto-capture interval into the sidebar
        # at startup so the hint text shows the current cadence.
        self.window.session_view.set_screencap_auto_interval(
            int(self.config.ui.screen_capture_auto_interval_sec)
        )
        # Push the user's saved appendix-inclusion defaults so the
        # Export PDF + Print dialogs pre-check the right boxes.
        self.window.session_view.set_appendix_export_defaults(
            self._appendix_export_defaults_from_config(),
        )
        # Restore the persisted Transcript-playback split percentage so
        # the user's preferred ratio applies the first time playback
        # engages this session.
        self.window.session_view.set_transcript_playback_split_top_pct(
            int(self.config.ui.transcript_playback_split_top_pct)
        )
        # Apply the persisted session-list sort spec so the user opens
        # the app the way they left it. Sort is set before any sessions
        # load via _refresh_sessions, so the first render is sorted.
        self.window.set_session_list_sort(self.config.ui.session_list_sort)
        # Restore the persisted window size + position + horizontal
        # splitter ratio. Has to run before window.show() ideally,
        # but Qt accepts restoreGeometry after show too -- it just
        # repositions the live window. Either way, applied here.
        self.window.restore_layout_state(
            self.config.ui.main_window_geometry,
            self.config.ui.main_splitter_state,
        )

    def _on_transcript_playback_split_changed(self, pct: int) -> None:
        """Persist the user's new splitter ratio (debounced)."""
        self.config.ui.transcript_playback_split_top_pct = int(pct)
        # Coalesce rapid drag events into one disk write 500 ms after
        # the last move; starting a running timer just resets it.
        if not hasattr(self, "_save_split_timer"):
            self._save_split_timer = QTimer(self)
            self._save_split_timer.setSingleShot(True)
            self._save_split_timer.setInterval(500)
            self._save_split_timer.timeout.connect(self.config.save)

    def _persist_window_layout(self) -> None:
        """Serialize + write window size/position + splitter state
        to config.toml at app exit.

        Tolerant of partial failure: if either save returns an empty
        string, the corresponding config field stays at the last
        successfully-saved value (no half-update). aboutToQuit is
        the only caller, so logging is enough on failure -- the user
        is exiting anyway.
        """
        try:
            geom, split = self.window.save_layout_state()
            self.config.ui.main_window_geometry = geom or self.config.ui.main_window_geometry
            self.config.ui.main_splitter_state = split or self.config.ui.main_splitter_state
            self.config.save()
        except Exception:
            log.exception("window layout persist failed")

    def _on_session_list_sort_changed(self, spec: str) -> None:
        """Persist the user's chosen sort order for the session list.

        Called from MainWindow when the user clicks the Date or Title
        column header. Indicator-column clicks snap back inside
        MainWindow without reaching this handler.
        """
        self.config.ui.session_list_sort = spec
        # Saved synchronously -- this is a one-shot user click, not a
        # drag event that would benefit from debouncing.
        self.config.save()
        self._save_split_timer.start()

    def _apply_synthesis_automation(self) -> None:
        """Push the current setting state into the SessionView, swapping
        the Generate + Paste buttons for the single Send button or
        vice-versa. Called at startup and after Settings is closed."""
        self.window.session_view.set_automation_enabled(
            self.config.synthesis.automation_enabled,
            self.config.synthesis.llm_target,
        )

    # ---- synthesis automation: connection state polling --------------------

    def _poll_synthesis_state(self) -> None:
        """Re-compute the three-state synthesis-connection value and
        propagate to the status bar + SessionView Send-button gating.

        Runs every 5 seconds via _synth_poll_timer. Cheap: one psutil
        process_iter pass + a property read on the bridge."""
        new_state = SynthesisConnectionState.derive(
            chrome_running=is_chrome_running(),
            bridge_connected=self._bridge_ready_state,
        )
        if new_state == self._synth_state:
            return
        old_state = self._synth_state
        self._synth_state = new_state
        log.info(
            "synthesis state: %s -> %s",
            old_state.value,
            new_state.value,
        )
        self._refresh_status_indicators()
        # Push the gating into SessionView so the Send button reflects
        # the new state without waiting for the next session-state event.
        self.window.session_view.set_synthesis_connection_state(new_state)
        # On just-became-connected transitions, fire a keepalive ping
        # right away rather than waiting up to 25s for the next tick --
        # the bridge is fresh and a quick pong confirms end-to-end.
        if (
            new_state == SynthesisConnectionState.RUNNING_CONNECTED
            and old_state != SynthesisConnectionState.RUNNING_CONNECTED
        ):
            self._send_keepalive_ping()

    def _send_keepalive_ping(self) -> None:
        """Fire a ping to keep the MV3 service worker awake.

        Only runs when state is RUNNING_CONNECTED -- a ping into a
        nonexistent peer is wasted work, and we don't want to log
        warnings every 25s when Chrome is closed."""
        if self._synth_state != SynthesisConnectionState.RUNNING_CONNECTED:
            return
        ping_id = f"keepalive-{secrets.token_hex(4)}"
        ping = automation_messages.PingRequest(request_id=ping_id)
        if not self._bridge.send(ping):
            log.debug("keepalive ping send failed (peer dropped)")

    # ---- synthesis automation: bridge callbacks ----------------------------

    def _on_bridge_connect(self, state: HandshakeState) -> None:
        log.info(
            "bridge: extension connected (id=%s host=%s)",
            state.extension_id,
            state.host_version,
        )
        self._bridge_ready_state = True
        # Bounce the re-poll to the main thread via signal (these
        # callbacks fire on the bridge's accept/reader thread).
        self.bridge_state_changed.emit()

    def _on_bridge_disconnect(self) -> None:
        log.info("bridge: extension disconnected")
        self._bridge_ready_state = False
        self.bridge_state_changed.emit()

    def _on_bridge_message(self, msg: dict) -> None:
        """Inbound from the extension. Runs on the bridge's reader
        thread.

        Pongs are signaled inline (threading.Event is thread-safe) so
        a blocking _ping_extension on the main Qt thread doesn't
        deadlock waiting for itself; everything else is emitted via
        pyqtSignal, which Qt auto-routes across threads using
        QueuedConnection so the slot runs on the main thread.

        We log on the bridge thread BEFORE the emit so future drops
        can be distinguished: if the bridge-thread log fires but
        _dispatch_bridge_message doesn't, the cross-thread bounce
        failed (signal/connection misconfigured). If the bridge-
        thread log doesn't fire, the message never reached the
        bridge at all (the failure is upstream in the extension /
        native host / TCP path).
        """
        msg_type = msg.get("type", "")
        request_id = msg.get("request_id", "")
        # Pongs arrive every keep-alive cycle (~25s) and never carry
        # information beyond "the worker is alive"; logging them at
        # INFO floods the production log. The Verify-wizard handshake
        # tracks pongs via a threading.Event so the log line wasn't
        # load-bearing for correctness either.
        if msg_type == "pong":
            log.debug("bridge worker: <- pong rid=%s", request_id)
            evt = self._pending_pings.pop(request_id, None)
            if evt is not None:
                evt.set()
            return
        log.info("bridge worker: <- %s rid=%s", msg_type, request_id)
        self.bridge_message_received.emit(msg)

    def _dispatch_bridge_message(self, msg: dict) -> None:
        msg_type = msg.get("type", "")
        request_id = msg.get("request_id", "")
        log.info(
            "bridge <- %s rid=%s inflight=%s",
            msg_type,
            request_id,
            list(self._inflight_syntheses.keys()),
        )
        if msg_type == "status":
            session_id = self._inflight_syntheses.get(request_id, "")
            if session_id:
                event = msg.get("event", "")
                detail = msg.get("detail", "")
                label = self._format_status_event(event, detail)
                if label:
                    self.window.status(label, timeout_ms=4000)
                    # Mirror the status into the synthesis banner so
                    # the user sees the same progress without having
                    # to look at the bottom status bar.
                    self.window.session_view.set_synthesis_in_progress(
                        session_id, True, status_text=label,
                    )
            return
        if msg_type == "result":
            markdown = msg.get("markdown", "")
            target = msg.get("target", "")
            log.info(
                "bridge result rid=%s len=%d target=%s",
                request_id,
                len(markdown),
                target,
            )
            session_id = self._inflight_syntheses.pop(request_id, "")
            if not session_id:
                log.warning(
                    "result for unknown request %s (inflight keys=%s); "
                    "dropping %d-char synthesis",
                    request_id,
                    list(self._inflight_syntheses.keys()),
                    len(markdown),
                )
                # Surface the orphaned result to the user instead of
                # dropping silently -- they at least see what came
                # back even if we can't route it to a session. Helps
                # debug the v0.6.3 result-routing path.
                self.window.status(
                    f"Synthesis returned {len(markdown)} chars but "
                    "the originating session was unrecognized -- "
                    "see the log.",
                    timeout_ms=10000,
                )
                return
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

    def _strip_all_appendices(self, markdown: str) -> str:
        """Run every LLM-appendix strip helper in sequence.

        Shared by the paste-back path (`_apply_synthesis_result`)
        and the edit-dialog path (`_on_appendix_edit_requested`)
        so the strip toggle behaves consistently regardless of
        which surface produced the new notes.md. All four
        appendices ride one Settings toggle -- if the user wants
        notes.md clean, they want them all gone. The sidecar
        persistence is the caller's responsibility (runs BEFORE
        the strip so data survives).
        """
        from .utils.attendee_appendix import strip_appendix  # noqa: PLC0415
        from .utils.attendee_context import (  # noqa: PLC0415
            strip_appendix as strip_attendee_context,
        )
        from .utils.invite_mentions import (  # noqa: PLC0415
            strip_appendix as strip_invite_mentions,
        )
        from .utils.topic_appendix import (  # noqa: PLC0415
            strip_appendix as strip_topic_appendix,
        )
        markdown = strip_appendix(markdown)
        markdown = strip_topic_appendix(markdown)
        markdown = strip_attendee_context(markdown)
        markdown = strip_invite_mentions(markdown)
        return markdown

    def _apply_synthesis_result(
        self,
        session_id: str,
        markdown: str,
        *,
        archive_existing: bool,
    ) -> Optional[Path]:
        """Shared post-processing for a freshly received synthesis.

        Runs the markdown normalization + LLM-appendix extraction +
        sidecar persistence + optional strip + save + search re-index
        + view reload, then returns the archive path (or None if no
        prior synthesis was archived). Both the Chrome-extension flow
        (``_handle_synthesis_result``) and the manual paste flow
        (``_on_paste_notes``) call this so the two paths produce
        identical on-disk state; without the shared call the manual
        flow used to skip the attendee-details fill, the sidecar
        write, the strip toggle, and the loose-list normalization.

        Raises ``OSError`` on save failure -- the caller is expected
        to display a path-appropriate dialog.
        """
        # Tighten Claude's loose-list serialization (issue #42).
        # Claude.ai's Copy button writes a blank line between every
        # bullet + multiple blank lines between sections; the user-
        # expected source view (matching browser text-selection paste)
        # is tight-list. No-op for any target that already returns
        # tight markdown.
        from .automation.markdown_normalize import normalize_synthesis_markdown  # noqa: PLC0415
        markdown = normalize_synthesis_markdown(markdown)
        # Parse + apply the LLM-extracted attendee details appendix
        # (issue #51 Phase 4). Done BEFORE save_notes so the parsed
        # data is independent of the save path's success.
        self._apply_attendee_details_appendix(session_id, markdown)
        # Persist the four LLM appendices to the sidecar BEFORE the
        # optional strip pass so the data is preserved regardless of
        # the strip toggle (#64 sidecar followup).
        try:
            from .utils.appendix_store import AppendixStore  # noqa: PLC0415
            AppendixStore(session_id).save_from_notes(markdown)
        except Exception:
            log.exception(
                "appendix sidecar write failed: %s", session_id,
            )
        if self.config.synthesis.strip_attendee_appendix:
            markdown = self._strip_all_appendices(markdown)
        tstore = TranscriptStore(session_id)
        archive_path = tstore.save_notes(
            markdown, archive_existing=archive_existing,
        )
        self.store.update_session(session_id, has_notes=True)
        self._reindex_search_for(session_id)
        # Targeted SessionView refresh in place of a full
        # _on_session_selected reload (#73 finding #3). The full
        # reload re-read transcript / live_notes / templates /
        # highlights / contacts from disk via a worker thread on
        # every paste, producing a 50-100ms empty-pane flash on the
        # highest-touch user actions. The targeted setters update
        # the Synthesis tab + tray + appendix transform in place
        # without the round-trip. Only previous-notes-paths need a
        # re-read when archive_existing actually rotated a file.
        sv = self.window.session_view
        if sv._session is not None and sv._session.id == session_id:
            sv.set_notes_text(markdown)
            if archive_path is not None:
                sv.set_previous_notes(tstore.list_previous_notes())
        return archive_path

    def _handle_synthesis_result(self, session_id: str, markdown: str, target: str) -> None:
        # Clear the in-progress indicator first so the banner is gone
        # by the time any modal dialogs surface.
        self.window.session_view.set_synthesis_in_progress(session_id, False)
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
            archive_path = self._apply_synthesis_result(
                session_id, markdown, archive_existing=True,
            )
        except OSError:
            log.exception("save_notes failed for %s", session_id)
            QMessageBox.critical(
                self.window,
                "Synthesis Automation",
                "Couldn't save the synthesis to disk; see the log for "
                "details.",
            )
            return
        if archive_path:
            self.window.status(
                f"Synthesis received. Prior notes archived to {archive_path.name}",
                timeout_ms=8000,
            )
        else:
            self.window.status("Synthesis received.", timeout_ms=5000)

    def _handle_synthesis_error(self, session_id: str, code: str, detail: str) -> None:
        # Clear the in-progress banner / re-enable Send before
        # showing the error dialog.
        if session_id:
            self.window.session_view.set_synthesis_in_progress(session_id, False)
        # The clipboard-unavailable path is the most common first-run
        # failure (Chrome shows a per-site permission prompt on the
        # initial programmatic clipboard read and the user has to
        # click Allow). Treat it as informational rather than a scary
        # "Warning" dialog -- the fix is one click and the response
        # is still visible in the browser tab.
        if code == automation_messages.ERR_CLIPBOARD_UNAVAILABLE:
            QMessageBox.information(
                self.window,
                "Clipboard permission needed",
                "Couldn't read Claude's response back from your browser.\n\n"
                "Chrome requires a one-time permission grant before an "
                "extension can read the clipboard for a given site. To "
                "fix this:\n\n"
                "1. Switch to your Claude.ai tab.\n"
                "2. Click Claude's own Copy button on the response.\n"
                "3. When Chrome asks 'Allow claude.ai to see text and "
                "images copied to the clipboard?', click Allow.\n"
                "4. Return here and click Send to Claude.ai again.\n\n"
                "Your response is still in the Claude tab -- nothing is lost. "
                "You can also copy + paste it manually for this one synthesis "
                "by switching automation off in Settings.",
            )
            return
        # Map other known codes to friendlier messages.
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
        # Three-state-aware preflight. If Chrome isn't running, launch
        # it and wait for the bridge to connect. The Send button is
        # disabled in RUNNING_DISCONNECTED so we shouldn't see that
        # path here, but guard for the race where the state poll
        # missed a transition.
        if self._synth_state == SynthesisConnectionState.NOT_RUNNING:
            if not self._launch_chrome_and_wait_for_connection(session_id):
                return
        elif self._synth_state == SynthesisConnectionState.RUNNING_DISCONNECTED:
            QMessageBox.warning(
                self.window,
                "Synthesis Automation",
                "Chrome is running but the extension hasn't connected "
                "back to the app. The extension service worker may be "
                "asleep -- it should reconnect within ~60s. If it "
                "persists, open the Meeting Notetaker extension popup "
                "in Chrome and click 'Reconnect to app', or reload the "
                "extension at chrome://extensions.",
            )
            return
        elif not self._bridge_ready_state:
            # Defensive: state thinks connected but bridge says no.
            # Rare race; surface a clear error.
            QMessageBox.warning(
                self.window,
                "Synthesis Automation",
                "Lost connection to the extension while preparing to "
                "send. Try again in a moment.",
            )
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

        # Pick the prompt template using the three-tier fallback:
        # 1. session's saved choice (SessionView dropdown)
        # 2. global Settings default_template_name
        # 3. bundled "default" template (or first available)
        templates = prompts_mod.list_templates()

        def _find(name: str):
            return next((t for t in templates if t.name == name), None)

        chosen = None
        chosen_name = store.read_prompt_template_name()
        if chosen_name:
            chosen = _find(chosen_name)
        if chosen is None and self.config.synthesis.default_template_name:
            chosen = _find(self.config.synthesis.default_template_name)
        if chosen is None:
            chosen = _find("default") or (templates[0] if templates else None)
        if chosen is None:
            QMessageBox.warning(
                self.window, "Synthesis Automation",
                "No prompt templates found in the prompts folder.",
            )
            return
        rendered = prompts_mod.render(
            chosen,
            session_title=session.title,
            session_date=when,
            transcript=transcript,
            live_notes=live_notes,
            user_name=self.config.ui.user_name,
            include_system_prompts=(
                self.config.synthesis.auto_extract_attendee_details
            ),
        )

        request_id = secrets.token_hex(8)
        self._inflight_syntheses[request_id] = session_id
        # Build the URL the extension should land on. For Claude this
        # honors the optional claude_project_id setting -- when set,
        # syntheses accumulate in that project rather than the user's
        # default chat list.
        chat_url = ""
        if target_key == "claude":
            chat_url = self.config.synthesis.claude_chat_url()
        msg = automation_messages.SynthesizeRequest(
            request_id=request_id,
            target=target_key,
            prompt=rendered,
            new_chat=True,
            chat_url=chat_url,
        )
        if not self._bridge.send(msg):
            self._inflight_syntheses.pop(request_id, None)
            self.window.session_view.set_synthesis_in_progress(
                session_id, False
            )
            QMessageBox.warning(
                self.window, "Synthesis Automation",
                "Couldn't reach the extension. Try Reconnect from the "
                "extension popup, or fall back to manual Generate / Paste.",
            )
            return
        # Make sure the banner persists across session-switches: if the
        # user clicks Send, navigates to a different session, and then
        # comes back, the banner should still be up. SessionView keys
        # by session id; the call here registers the in-progress state
        # for the originating session.
        self.window.session_view.set_synthesis_in_progress(
            session_id, True, status_text=f"Sent to {target.label}, waiting for response..."
        )
        self.window.status(
            f"Sent to {target.label}. The response will land in the Synthesis tab.",
            timeout_ms=6000,
        )

    def _launch_chrome_and_wait_for_connection(self, session_id: str) -> bool:
        """When the user clicks Send and Chrome isn't running, launch
        chrome.exe (passing claude.ai/new as the URL so it opens
        directly to the synthesis target), then wait up to 60s for
        the bridge to report a connected peer.

        Returns True if Chrome launched AND the bridge connected
        within the timeout; False otherwise (with an error dialog
        already shown).

        Cold start can be slow: the user might be waking the machine,
        Chrome has to load + restore tabs + spin up the extension
        service worker + native messaging host + TCP connection. 60s
        is generous; we use a QEventLoop so the UI stays responsive.
        """
        from PyQt6.QtCore import QEventLoop  # noqa: PLC0415

        # Show the in-progress banner so the user sees something
        # happening immediately. Updated as the launch progresses.
        self.window.session_view.set_synthesis_in_progress(
            session_id,
            True,
            status_text="Starting Chrome...",
        )
        # If a Claude project is configured, launch Chrome directly
        # at the project URL so the cold-start tab is the right one.
        # (Without this, Chrome would land on /new and the synthesis
        # would still work because background.js opens its own tab,
        # but we'd briefly leave the /new tab as a stray.)
        cold_url = self.config.synthesis.claude_chat_url()
        ok = launch_chrome(cold_url)
        if not ok:
            self.window.session_view.set_synthesis_in_progress(session_id, False)
            QMessageBox.warning(
                self.window,
                "Synthesis Automation",
                "Couldn't find or launch Chrome on this machine. "
                "Install Chrome from https://www.google.com/chrome/ "
                "or open it manually before clicking Send.",
            )
            return False

        # Wait for the bridge to report connected. 60-second budget.
        self.window.session_view.set_synthesis_in_progress(
            session_id,
            True,
            status_text="Waiting for Chrome to load extension...",
        )
        loop = QEventLoop()
        connect_poll = QTimer()
        connect_poll.setInterval(200)
        connect_poll.timeout.connect(
            lambda: self._bridge.is_connected and loop.quit()
        )
        deadline = QTimer()
        deadline.setSingleShot(True)
        deadline.setInterval(60_000)
        deadline.timeout.connect(loop.quit)
        connect_poll.start()
        deadline.start()
        loop.exec()
        connect_poll.stop()
        deadline.stop()

        if not self._bridge.is_connected:
            self.window.session_view.set_synthesis_in_progress(session_id, False)
            QMessageBox.warning(
                self.window,
                "Synthesis Automation",
                "Chrome started but the Meeting Notetaker extension "
                "didn't connect back within 60 seconds.\n\n"
                "Check that the extension is enabled at chrome://"
                "extensions and that it shows 'Connected' in its "
                "popup. Then try Send again.",
            )
            return False
        # Force one fresh state-poll so the indicator + button reflect
        # the connection before we proceed.
        self._poll_synthesis_state()
        return True

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
        self.window.open_about_requested.connect(self._on_about)
        self.window.open_user_guide_requested.connect(self._on_user_guide)
        self.window.check_for_updates_requested.connect(self._on_check_for_updates)
        self.window.upgrade_requested.connect(self._on_upgrade)
        self.window.quit_requested.connect(self.qt_app.quit)
        self.window.session_selected.connect(self._on_session_selected)
        self.window.delete_sessions_requested.connect(self._on_delete_sessions)
        self.window.rename_session_requested.connect(self._on_rename_session)
        self.window.edit_session_timestamp_requested.connect(self._on_edit_session_timestamp)
        self.window.open_recording_requested.connect(self._on_open_recording)
        self.window.export_recording_requested.connect(self._on_export_recording)
        self.window.export_video_requested.connect(self._on_export_video)
        self.window.export_package_requested.connect(self._on_export_package)
        self.window.delete_recording_requested.connect(self._on_delete_recording)
        self.window.session_list_sort_changed.connect(self._on_session_list_sort_changed)
        self.window.open_search_requested.connect(self._on_open_search)
        self.window.rebuild_search_index_requested.connect(self._on_rebuild_search_index)
        self.window.backup_now_requested.connect(self._on_backup_now_requested)
        self.window.restore_backup_requested.connect(self._on_restore_backup_requested)
        self.window.classification_filter_changed.connect(
            self._on_classification_filter_changed
        )
        self.window.manage_series_requested.connect(self._on_manage_series)
        self.window.manage_classification_requested.connect(
            self._on_manage_classification,
        )
        self.window.address_book_requested.connect(self._on_address_book)
        # SessionView -> classification chip mutations
        sv = self.window.session_view
        sv.add_topic_requested.connect(self._on_add_topic_requested)
        sv.remove_topic_requested.connect(self._on_remove_topic_requested)
        sv.accept_topic_requested.connect(self._on_accept_topic_requested)
        sv.set_series_requested.connect(self._on_set_series_requested)
        sv.highlights_changed.connect(self._on_session_highlights_changed)

        self.tray.open_main_window.connect(self._foreground_window)
        self.tray.new_session_requested.connect(self._on_new_session)
        self.tray.stop_requested.connect(self.controller.stop_session)
        self.tray.settings_requested.connect(self._on_settings)
        self.tray.quit_requested.connect(self.qt_app.quit)
        self.tray.meeting_notification_clicked.connect(self._on_create_session_from_calendar)
        self.tray.audio_notification_clicked.connect(self._on_create_session_from_audio)

        sv = self.window.session_view
        sv.start_clicked.connect(self._on_start_clicked)
        sv.stop_clicked.connect(lambda _sid: self.controller.stop_session())
        sv.generate_prompt_clicked.connect(self._on_generate_prompt)
        sv.paste_notes_clicked.connect(self._on_paste_notes)
        sv.send_to_llm_clicked.connect(self._on_send_to_llm)
        sv.copy_tab_clicked.connect(self._on_copy_tab)
        sv.live_notes_changed.connect(self._on_live_notes_changed)
        sv.synthesis_notes_changed.connect(self._on_synthesis_notes_changed)
        sv.review_speakers_clicked.connect(self._on_review_speakers)
        sv.appendix_edit_requested.connect(self._on_appendix_edit_requested)
        sv.restore_previous_notes_clicked.connect(self._on_restore_previous_notes)
        sv.delete_previous_notes_clicked.connect(self._on_delete_previous_notes)
        sv.prompt_template_changed.connect(self._on_prompt_template_changed)
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
        # Screen-capture lifecycle. Start launches the region picker;
        # Stop tears down. Capture / Insert in the sidebar grab the
        # currently-armed region.
        sv.start_screen_capture_clicked.connect(self._on_start_screen_capture)
        sv.stop_screen_capture_clicked.connect(self._on_stop_screen_capture)
        sv.screencap_capture_clicked.connect(self._on_screencap_capture)
        sv.screencap_insert_clicked.connect(self._on_screencap_insert)
        sv.screencap_auto_toggled.connect(self._on_auto_capture_toggled)
        sv.delete_screenshot_clicked.connect(self._on_delete_screenshot)
        # v0.7.2 (#51 Phase 3): drawer row-click opens the Address
        # Book filtered to that contact.
        sv.contact_clicked_in_drawer.connect(self._on_drawer_contact_clicked)
        # Transcript playback wiring. The player bar fires play / pause /
        # seek; MainApp owns the single AudioPlayer and routes them.
        sv.transcript_play_clicked.connect(self._on_transcript_play)
        sv.transcript_pause_clicked.connect(self._on_transcript_pause)
        sv.transcript_seek_ms_requested.connect(self._on_transcript_seek)
        sv.transcript_playback_split_changed.connect(
            self._on_transcript_playback_split_changed
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
        # Non-fatal capture-stall warning (issue #44). Surface via the
        # status bar with a long timeout so the user notices, but don't
        # block them with a modal -- the recording succeeded; only the
        # trailing N seconds are silence.
        self.controller.capture_warning.connect(self._on_capture_warning)

    # ---- session list ------------------------------------------------------

    def _refresh_session_list(self, *, select: Optional[str] = None) -> None:
        sessions = self.store.list_sessions()
        # Apply the active classification filter, if any.
        sessions = self._apply_classification_filter(sessions)
        self.window.set_sessions(sessions)
        # Refresh the navigator's choice lists so newly-added series/
        # people/topics show up in the pulldown without a restart.
        self._refresh_classification_choices()
        if select:
            self.window.select_session(select)
        elif sessions:
            self.window.select_session(sessions[0].id)

    def _apply_classification_filter(self, sessions: list[Session]) -> list[Session]:
        """Intersect the full session list with the active filter.

        VIEW_ALL or unselected value -> no filtering (returns the
        input). VIEW_BY_* with a value -> the subset that
        ClassificationStore associates with that filter value.
        Preserves the input list's order (which is already sorted
        by the session-list sort spec).
        """
        if (
            self._classification_filter_view == VIEW_ALL
            or self._classification_filter_value is None
            or self.classification is None
        ):
            return sessions
        try:
            if self._classification_filter_view == VIEW_BY_SERIES:
                allowed = set(self.classification.session_ids_for_series(
                    self._classification_filter_value
                ))
            elif self._classification_filter_view == VIEW_BY_PERSON:
                # "By Person" -- backed by Contacts post-Phase 2.
                allowed = set(self.classification.session_ids_for_contact(
                    self._classification_filter_value
                ))
            elif self._classification_filter_view == VIEW_BY_TOPIC:
                allowed = set(self.classification.session_ids_for_topic(
                    self._classification_filter_value
                ))
            else:
                return sessions
        except Exception:
            log.exception("classification filter query failed")
            return sessions
        return [s for s in sessions if s.id in allowed]

    def _refresh_classification_choices(self) -> None:
        """Re-populate the navigator combo + chips-bar pickers with
        current series / people / topics. Cheap (sub-millisecond at
        any realistic store size) so we run it on every list
        refresh rather than tracking dirty bits.

        Two different lists power the two surfaces:

        * Navigator (filter pulldown) -- in-use only. Offering a
          value that returns zero sessions wastes a click.
        * Chips bar (Add/Change pickers) -- full catalog. The user
          might re-link a session to a known-but-currently-orphan
          series/person/topic.
        """
        if self.classification is None:
            return
        try:
            # Navigator: in-use only (EXISTS-filtered server-side).
            nav_series = [
                (s.id, s.name)
                for s in self.classification.list_series_in_use()
            ]
            # People navigator entries -- backed by Contacts.
            nav_people = [
                (c.id, c.display_name)
                for c in self.classification.list_contacts_in_use()
            ]
            nav_topics = [
                (t.id, t.name)
                for t in self.classification.list_topics_in_use()
            ]
            self.window.set_classification_choices(
                series=nav_series, people=nav_people, topics=nav_topics,
            )
            # Chips bar: full catalog so the user can pick a topic
            # they previously used on another session even if it's
            # currently unassigned everywhere.
            full_series_rows = self.classification.list_series()
            full_people_rows = self.classification.list_contacts()
            full_topic_rows = self.classification.list_topics()
            self.window.session_view.set_classification_known_lists(
                series=[s.name for s in full_series_rows],
                people=[p.display_name for p in full_people_rows],
                topics=[t.name for t in full_topic_rows],
            )
        except Exception:
            log.exception("classification refresh failed")

    def _on_session_selected(self, session_id: str) -> None:
        """Bind the session to the right pane, then load its content
        asynchronously (issue #39).

        The synchronous prelude binds metadata + clears the panes via
        set_session() with empty content. Cheap state (classification
        chips, screenshot offsets, attachments tab, player) runs
        inline. The expensive disk reads (transcript / live notes /
        synthesis / previous-notes glob / templates / highlights) run
        on `_SessionContentLoader`; the result lands in
        `_on_session_content_loaded` which fills in the panes.

        Rapid session-switching is handled by a generation counter:
        every call bumps it, and stale results are dropped.
        """
        session = self.store.get_session(session_id)
        if session is None:
            return
        # Bind the session up front (empty content). The worker fills
        # in transcript / notes / live_notes / etc. when it finishes.
        self.window.session_view.set_session(
            session,
            transcript="",
            notes="",
            previous_notes_paths=[],
            live_notes="",
        )
        # Bump generation BEFORE dispatching so any in-flight worker
        # from a prior selection is treated as stale on its emit.
        self._session_load_generation += 1
        self._session_currently_loading = session_id
        # Things that don't need disk content -- run synchronously so
        # the immediate UI is responsive (buttons, audio player, chips).
        self._maybe_load_player_for_session(session_id)
        self._push_screenshot_offsets(session_id)
        # Push the session's classification (series / people / topics)
        # into the chips bar. Missing classification store -> empty
        # bar with disabled mutators.
        self._refresh_session_classification(session_id)
        # If the user reselected a session that's still actively being
        # recorded (back-to-back-session scenario), surface any tag counts
        # the controller already collected.
        active = getattr(self.controller, "active_session", None)
        if active is not None and active.id == session_id:
            store = self.controller._tag_stores.get(session_id)
            if store is not None:
                self.window.session_view.set_speaker_tag_counts(store.counts())
        # Issue #29: Attachments tab pulls the per-session list.
        # AttachmentsStore opens a SQLite connection on the main
        # thread -- typically fast, so keep it inline. Could move
        # off-thread later if attachment counts grow.
        try:
            self.window.session_view.attachments_tab().set_session(session_id)
        except Exception:
            log.exception("attachments tab failed to load for %s", session_id)
        # Async content load. Worker emits content_loaded(generation,
        # _LoadedSessionContent); _on_session_content_loaded applies
        # if the generation still matches.
        self._dispatch_session_content_load(session_id)

    def _dispatch_session_content_load(self, session_id: str) -> None:
        """Spawn the off-thread loader for a session's disk content."""
        prior = self._session_content_worker
        if prior is not None:
            prior.requestInterruption()
        worker = _SessionContentLoader(
            session_id=session_id,
            generation=self._session_load_generation,
        )
        worker.setObjectName(
            f"SessionContentLoader[{self._session_load_generation}]"
        )
        worker.content_loaded.connect(self._on_session_content_loaded)
        worker.finished.connect(lambda w=worker: self._retire_session_content_worker(w))
        self._session_content_worker = worker
        worker.start()

    def _on_session_content_loaded(self, generation: int, content) -> None:
        """Apply worker results when the user still has this session selected."""
        if generation != self._session_load_generation:
            log.debug(
                "dropping stale session-content result (gen=%d, current=%d)",
                generation, self._session_load_generation,
            )
            return
        if content.error is not None:
            log.warning(
                "session content load returned error for %s: %s",
                content.session_id, content.error,
            )
            # Leave the panes empty; the user can re-click or restart.
            return
        sv = self.window.session_view
        # Fill the panes via the piecemeal setters so we don't go
        # through set_session() again (which would flush saves +
        # reset state we already configured in the prelude).
        sv.set_transcript_text(content.transcript)
        # Public setter: applies the My Notes preview-mode default
        # based on whether the loaded body has content beyond the
        # seeded template (#67 followup). The session-select prelude
        # called set_session with an empty live_notes so the editor
        # landed in Edit; this call swings it to Preview when the
        # real body has actual notes.
        sv.set_live_notes_text(content.live_notes)
        sv.set_notes_text(content.notes)
        sv.set_previous_notes(content.previous_notes_paths)
        sv.set_prompt_templates(
            content.template_names,
            selected=content.selected_template,
            settings_default=self.config.synthesis.default_template_name,
        )
        sv.set_attendee_names(parse_attendees(content.live_notes))
        # Push the session's Contact list into the attendee-details
        # drawer (issue #51 Phase 3). Cheap query; refreshed again
        # whenever attendee sync re-runs (see _sync_attendees_to_people).
        self._refresh_session_contacts_in_drawer(content.session_id)
        # Issue #64: hydrate the Appendix tray's Session Attachments
        # section. The tray reads JSON appendices + scans for links
        # itself off the editors' current text, but the attachment
        # list lives in a separate store -- push it in explicitly.
        try:
            from .models.attachments import AttachmentsStore  # noqa: PLC0415
            store = AttachmentsStore(content.session_id)
            sv.set_session_attachment_names([
                rec.display_name for rec in store.list()
            ])
        except Exception:
            log.exception(
                "attachments fetch for appendix tray failed: %s",
                content.session_id,
            )
            sv.set_session_attachment_names([])
        # Highlights: total_ms is 0 until the audio player finishes
        # its own async load and fires set_player_total_ms. The
        # highlight bar disables interaction cleanly at total_ms=0
        # and updates when audio arrives.
        total_ms = 0
        if self._audio_player is not None and self._player_loaded_session_id == content.session_id:
            try:
                total_ms = int(self._audio_player.total_ms())
            except Exception:
                total_ms = 0
        sv.set_session_highlights(total_ms, content.highlights or HighlightSet())
        # Content-dependent side effects.
        self._sync_attendees_to_people(content.session_id, content.live_notes)
        if content.notes.strip():
            self._extract_topics_for_session(content.session_id, content.notes)

    def _retire_session_content_worker(self, worker) -> None:
        try:
            worker.wait()
        except Exception:
            log.exception("session content worker wait failed")
        try:
            worker.deleteLater()
        except Exception:
            log.exception("session content worker deleteLater failed")
        if self._session_content_worker is worker:
            self._session_content_worker = None

    def _refresh_session_classification(self, session_id: str) -> None:
        """Re-read + repaint the chips bar for the given session.

        Cheap to call after any classification mutation -- the store
        lookups are indexed and the bar's render is one layout pass.
        """
        if self.classification is None:
            self.window.session_view.set_classification(SessionClassification())
            return
        try:
            cls = self.classification.classification_for_session(session_id)
        except Exception:
            log.exception("classification read failed for %s", session_id)
            cls = SessionClassification()
        self.window.session_view.set_classification(cls)

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
        # Best-effort recurring-meeting series link from the title.
        # Runs AFTER calendar seed so the seeded attendees flow into
        # _sync_attendees_to_people on the first live_notes_changed.
        self._auto_link_series_for_new_session(session.id, result.title)
        self._refresh_session_list(select=session.id)

    def _seed_live_notes_from_meeting(
        self, session_id: str, info: MeetingInfo
    ) -> None:
        """Seed My Notes with the calendar invite's attendees + body.

        Phase 2 enhancement: for attendees that only carry an email
        (no display name), try to resolve via Contact email aliases.
        Hit -> seed the canonical Contact display_name instead of
        the raw email. Miss -> register the email as a stub Contact
        + email alias so future invites with that email resolve
        cleanly.

        Issue #29: also pull file attachments off the MeetingItem
        and stash them in the session's AttachmentsStore.
        """
        attendee_names: list[str] = []
        for a in info.attendees:
            chosen = self._resolve_calendar_attendee_display(a)
            if chosen:
                attendee_names.append(chosen)
        try:
            TranscriptStore(session_id).save_live_notes(
                seed_body_with_calendar(
                    attendees=attendee_names, agenda=info.body
                )
            )
        except OSError:
            log.exception("failed to seed live notes from calendar")
        # Pull calendar attachments (issue #29). Off-Windows / no
        # pywin32 -> pull_attachments_to_temp returns []. Errors
        # during save are logged + swallowed; the session is still
        # usable without the attachments.
        if info.attachments:
            try:
                from .integrations.outlook_calendar import pull_attachments_to_temp  # noqa: PLC0415
                from .models.attachments import (  # noqa: PLC0415
                    AttachmentsStore, SOURCE_CALENDAR,
                )
                tmp_paths = pull_attachments_to_temp(info.entry_id)
                if tmp_paths:
                    store = AttachmentsStore(session_id)
                    for p in tmp_paths:
                        try:
                            store.add_file(p, source=SOURCE_CALENDAR)
                        except Exception:
                            log.exception(
                                "calendar attachment import failed for %s", p,
                            )
                    # Bug fix: pre-convert Office attachments to PDF
                    # in the background so previews are warm by the
                    # time the user clicks them. Manual adds get the
                    # same treatment via AttachmentsTab's progress-
                    # dialog worker; calendar adds happen during
                    # session creation without a dialog, so we run
                    # silently in the background.
                    self._spawn_background_office_preconvert(session_id)
            except Exception:
                log.exception(
                    "calendar attachment pull failed for %s", session_id,
                )

    def _spawn_background_office_preconvert(self, session_id: str) -> None:
        """Scan the session's attachments for Office files and run
        COM conversion silently on a background thread. Pre-warms
        the office_preview cache so the user's first click on each
        attachment shows the preview instantly.

        No progress dialog: this typically completes before the
        user even reaches the Attachments tab. If they click an
        Office attachment before conversion completes, the
        preview pane's existing 'Converting...' flow handles it.

        Uses threading.Thread (not QThread) because this is pure
        fire-and-forget background work: no Qt signals, no UI
        updates, no lifetime tracking. The earlier QThread version
        crashed with 'QThread: Destroyed while thread is still
        running' when the deleteLater queued from finished raced
        ahead of Qt's internal isRunning() cleanup. convert_office_
        to_pdf does its own pythoncom.CoInitialize per call, so
        it's safe to run from a non-Qt thread.
        """
        import threading  # noqa: PLC0415

        from .models.attachments import AttachmentsStore  # noqa: PLC0415
        from .utils.office_preview import (  # noqa: PLC0415
            convert_office_to_pdf,
            is_office_extension,
        )

        try:
            store = AttachmentsStore(session_id)
            office_paths: list = []
            for rec in store.list():
                ext = Path(rec.stored_name).suffix.lower().lstrip(".")
                if not is_office_extension(ext):
                    continue
                p = store.file_path(rec.id)
                if p is not None:
                    office_paths.append(p)
            if not office_paths:
                return

            def _run() -> None:
                for p in office_paths:
                    try:
                        convert_office_to_pdf(p)
                    except Exception:
                        log.exception(
                            "background Office pre-convert failed for %s",
                            p,
                        )

            thread = threading.Thread(
                target=_run,
                name=f"office-preconvert-{session_id[:8]}",
                daemon=True,
            )
            thread.start()
            log.info(
                "spawned background Office pre-convert for %d file(s) in %s",
                len(office_paths), session_id,
            )
        except Exception:
            log.exception(
                "background Office pre-convert spawn failed for %s",
                session_id,
            )

    def _resolve_calendar_attendee_display(self, attendee) -> str:
        """Pick the best display string for one calendar attendee.

        Order of preference:
        1. Attendee.name (if present) -- the calendar already had
           a friendly name; use it as-is.
        2. Email alias hit in Contacts -- use the Contact's
           canonical display_name.
        3. Stub-create a Contact named after the email local-part,
           register the email as an alias, use the local-part.
        4. Whatever attendee.display falls back to (raw email or
           "(unknown)").
        """
        # Step 1: explicit name. Even when we have a name, we still
        # run email-based contact resolution + Outlook enrichment
        # (issue #51 Phase 2) so the Contact's title/company/etc.
        # get populated. Resolution + enrichment are best-effort;
        # display name comes back from the name field directly.
        name = (getattr(attendee, "name", "") or "").strip()
        email = (getattr(attendee, "email", "") or "").strip()
        self._enrich_contact_for_calendar_attendee(attendee, email)
        if name:
            return name
        # Step 2 + 3: email-driven resolution for the display name.
        if email and self.classification is not None:
            try:
                from .utils.contact_resolution import (  # noqa: PLC0415
                    display_name_for_email, resolve_attendee_email,
                )
                hit = display_name_for_email(self.classification, email)
                if hit:
                    return hit
                # Stub-create so the next invite resolves silently.
                result = resolve_attendee_email(
                    self.classification, email,
                )
                if result is not None:
                    return result.contact.display_name
            except Exception:
                log.exception(
                    "calendar email resolution failed for %r", email,
                )
        # Step 4: fall back to the existing display logic.
        return getattr(attendee, "display", "") or email or ""

    def _enrich_contact_for_calendar_attendee(self, attendee, email: str) -> None:
        """Apply Outlook-pulled rich fields to the Contact, if any.

        Issue #51 Phase 2. CalendarAttendee carries title/company/
        department from the Outlook AddressEntry (GAL or user
        contacts). Resolve the Contact via email when we have one
        OR by name otherwise, then call
        enrich_contact_from_calendar_attendee with fill_empty_only=True.
        Existing user values stay intact; new values populate NULL
        fields and flip last_enriched_source to 'outlook'.

        Best-effort: any exception falls through silently so a
        calendar-seed flow never errors over enrichment.
        """
        if self.classification is None:
            return
        # Need either a name or email to find/create the Contact.
        name = (getattr(attendee, "name", "") or "").strip()
        if not name and not email:
            return
        try:
            from .utils.contact_resolution import (  # noqa: PLC0415
                enrich_contact_from_calendar_attendee,
                resolve_calendar_attendee,
            )
            # 2026-05-28: resolve_calendar_attendee unifies email +
            # name lookup AND auto-merges any pre-existing duplicate
            # Contact rows for the same person into one canonical
            # row. Prior implementation called resolve_attendee_email
            # in isolation, which created stub Contacts named after
            # the email local-part; the attendee-by-name sync then
            # created a second bare Contact. Both paths now route
            # through the same resolver.
            result = resolve_calendar_attendee(
                self.classification, name=name, email=email,
            )
            if result is None:
                return
            enrich_contact_from_calendar_attendee(
                self.classification, result.contact.id, attendee,
            )
        except Exception:
            log.exception(
                "calendar enrichment failed for %r / %r", name, email,
            )

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
        # Invalidate the player's cached load whenever the session
        # state changes. The audio files on disk may have just been
        # rewritten (RECORDING -> PROCESSING -> COMPLETE encodes the
        # WAVs to opus and deletes the WAVs); the cached load is
        # now stale. _maybe_load_player_for_session, called inside
        # _on_session_selected, decides whether to reload based on
        # the new state.
        if self._player_loaded_session_id == session_id:
            if self._audio_player is not None:
                self._audio_player.close()
            self._player_loaded_session_id = None
        # Update list label + selected view.
        self._refresh_session_list(select=session_id)
        self._on_session_selected(session_id)
        self.tray.set_state(_TRAY_FOR_STATE.get(state, "idle"))
        # Drop any armed screen-capture region when the recording ends.
        # The button itself is greyed-out post-recording (the SessionView
        # gates on RECORDING / PAUSED), but the in-memory region would
        # otherwise stay until the user re-enters the session.
        from .models.session import (  # noqa: PLC0415
            STATE_COMPLETE, STATE_ERROR, STATE_NEW, STATE_PROCESSING,
        )
        if state in (STATE_COMPLETE, STATE_ERROR, STATE_NEW, STATE_PROCESSING):
            if session_id in self._screen_capture_regions:
                self._screen_capture_regions.pop(session_id, None)
                self._stop_auto_capture(session_id)
                self._hide_armed_region_overlay()
                self.window.session_view.set_screencap_armed(False)

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

    def _on_capture_warning(self, _session_id: str, msg: str) -> None:
        """Status-bar surface for a non-fatal capture stall (issue #44).

        The recording succeeded; the trailing seconds of audio are
        silence because the device callback stopped firing before
        Stop. Show in the status bar with a long timeout so it
        catches the user's eye without blocking them.
        """
        log.warning("capture warning: %s", msg)
        self.window.status(msg, timeout_ms=15000)

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
        # Pre-selection fallback chain: per-session override (set via
        # the SessionView dropdown) -> global Settings default ->
        # bundled "default.md". The dialog itself does its own lookup
        # by name, so we just hand it the best candidate.
        initial_name = (
            store.read_prompt_template_name()
            or self.config.synthesis.default_template_name
        )
        dialog = GeneratePromptDialog(
            session_title=session.title,
            session_date=when,
            transcript=transcript,
            live_notes=live_notes,
            user_name=self.config.ui.user_name,
            templates=prompts_mod.list_templates(),
            initial_template_name=initial_name,
            include_system_prompts=(
                self.config.synthesis.auto_extract_attendee_details
            ),
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
        # Mirror attendees into the classification People set so the
        # chips bar shows them and they're available as a filter.
        self._sync_attendees_to_people(session_id, body)
        if sv._session is not None and sv._session.id == session_id:
            self._refresh_session_classification(session_id)
            self._refresh_classification_choices()

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
            self._reindex_search_for(session_id)
        except OSError:
            log.exception("failed to save synthesis notes for %s", session_id)
            return
        # Refresh classification topic suggestions from the new body
        # and repaint the chips. Already-accepted topics survive.
        self._extract_topics_for_session(session_id, body)
        sv = self.window.session_view
        if sv._session is not None and sv._session.id == session_id:
            self._refresh_session_classification(session_id)
            self._refresh_classification_choices()

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

    def _on_appendix_edit_requested(self, session_id: str) -> None:
        """Open the AppendixEditDialog seeded from the sidecar.

        On accept: write the edited entries back to the sidecar and
        re-render the raw JSON blocks inside notes.md so the
        debounced _flush_notes pass doesn't immediately stomp the
        edits with the LLM's original.
        """
        from .ui.appendix_edit_dialog import AppendixEditDialog  # noqa: PLC0415
        from .utils.appendix_store import (  # noqa: PLC0415
            AppendixStore,
            regenerate_notes_json,
        )
        store = AppendixStore(session_id)
        ctx, details, topics, referenced = store.load_as_dataclasses()
        # Fall back to parsing notes.md in case the sidecar is
        # absent (sessions predating the sidecar migration).
        if not (ctx or details or topics or referenced):
            try:
                notes_md = TranscriptStore(session_id).read_notes()
                from .utils.attendee_appendix import parse_appendix  # noqa: PLC0415
                from .utils.attendee_context import (  # noqa: PLC0415
                    parse_attendee_context,
                )
                from .utils.invite_mentions import (  # noqa: PLC0415
                    parse_invite_mentions,
                )
                from .utils.topic_appendix import (  # noqa: PLC0415
                    parse_topic_appendix,
                )
                ctx = parse_attendee_context(notes_md)
                details = parse_appendix(notes_md)
                topics = parse_topic_appendix(notes_md)
                referenced = parse_invite_mentions(notes_md)
            except Exception:
                log.exception(
                    "appendix edit dialog seed-fallback failed: %s",
                    session_id,
                )
        dialog = AppendixEditDialog(
            attendee_context=ctx,
            attendee_details=details,
            topics=topics,
            referenced_attachments=referenced,
            parent=self.window,
        )
        if dialog.exec() != AppendixEditDialog.DialogCode.Accepted:
            return
        edited_ctx = dialog.attendee_context()
        edited_details = dialog.attendee_details()
        edited_topics = dialog.topics()
        edited_referenced = dialog.referenced_attachments()
        # Persist to sidecar first -- if the notes.md regenerate
        # fails, the sidecar still has the truthful state.
        store.save(
            attendee_context=edited_ctx,
            attendee_details=edited_details,
            topics=edited_topics,
            referenced_attachments=edited_referenced,
        )
        # Update notes.md so the LLM's original JSON blocks don't
        # round-trip back into the sidecar via the next
        # _flush_notes. Skipped when the synthesis on disk is
        # already empty -- no JSON blocks to overwrite. When the
        # strip_attendee_appendix Settings toggle is ON, apply the
        # strip pass so the dialog edits don't restore the JSON
        # the user told the app to hide (#73 finding #2).
        try:
            tstore = TranscriptStore(session_id)
            current_notes = tstore.read_notes()
            updated_notes = regenerate_notes_json(
                current_notes,
                attendee_context=edited_ctx,
                attendee_details=edited_details,
                topics=edited_topics,
                referenced_attachments=edited_referenced,
            )
            if self.config.synthesis.strip_attendee_appendix:
                updated_notes = self._strip_all_appendices(updated_notes)
            if updated_notes != current_notes:
                tstore.save_notes(updated_notes, archive_existing=False)
                # Keep the search index in sync so the edit is
                # findable immediately rather than waiting for the
                # 30s stale-fingerprint scan (#73 finding #2).
                self._reindex_search_for(session_id)
                # Push the updated text into the open SessionView so
                # the editor + tray reflect the change immediately
                # without waiting for the next session-switch.
                sv = self.window.session_view
                if sv._session is not None and sv._session.id == session_id:  # noqa: SLF001
                    sv.set_notes_text(updated_notes)
        except Exception:
            log.exception(
                "appendix edit notes.md regenerate failed: %s",
                session_id,
            )
        # Refresh the trays + preview against the new sidecar.
        sv = self.window.session_view
        if sv._session is not None and sv._session.id == session_id:  # noqa: SLF001
            sv._refresh_appendix_trays()  # noqa: SLF001

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
        # Transcript finalized -- push it into the search index now so
        # the user can search the meeting they just finished. The
        # periodic stale-scan would catch this in <=30s anyway; the
        # explicit call shortens the window to ~immediately.
        self._reindex_search_for(session_id)

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
            # Synthesis tab: substitute the raw LLM appendix JSON
            # blocks with the rendered "# Appendix (auto-extracted)"
            # Markdown tables before the clipboard handoff. Matches
            # what the user sees in preview / PDF / ZIP export so the
            # clipboard paste reads cleanly in Notion / Word / etc.
            # My Notes tab is intentionally NOT transformed -- the
            # user pastes their raw notes verbatim.
            if tab_id == "notes":
                from .utils.appendix_store import collect_for_session
                from .utils.appendix_transform import inject_appendix
                try:
                    from .models.attachments import AttachmentsStore
                    attach = AttachmentsStore(session_id)
                    attachment_names = [
                        rec.display_name for rec in attach.list()
                    ]
                except Exception:
                    log.exception(
                        "attachment names for copy-synthesis appendix failed: %s",
                        session_id,
                    )
                    attachment_names = []
                appendix_data = collect_for_session(
                    session_id=session_id,
                    notes_text=text,
                    live_notes_text=sv._live_notes_editor.toPlainText(),  # noqa: SLF001
                    session_attachments=attachment_names,
                )
                text = inject_appendix(text, appendix_data)
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

    def _on_prompt_template_changed(self, session_id: str, name: str) -> None:
        """Persist the user's per-session prompt template choice."""
        try:
            TranscriptStore(session_id).write_prompt_template_name(name)
        except OSError:
            log.exception("failed to save prompt template name for %s", session_id)

    def _on_restore_previous_notes(self, session_id: str, archive_path) -> None:
        """Replace notes.md with a selected archive's contents. The
        current notes.md gets archived first by ``restore_previous_notes``,
        so the operation is non-destructive."""
        from pathlib import Path  # noqa: PLC0415

        store = TranscriptStore(session_id)
        try:
            new_archive = store.restore_previous_notes(Path(archive_path))
        except (FileNotFoundError, ValueError, OSError) as exc:
            QMessageBox.warning(
                self.window, "Restore archive",
                f"Couldn't restore archive: {exc}",
            )
            return
        self.store.update_session(session_id, has_notes=True)
        self._on_session_selected(session_id)
        if new_archive:
            self.window.status(
                f"Restored. Prior synthesis archived to {new_archive.name}.",
                timeout_ms=6000,
            )
        else:
            self.window.status("Synthesis restored from archive.", timeout_ms=5000)

    def _on_delete_previous_notes(self, session_id: str, archive_path) -> None:
        from pathlib import Path  # noqa: PLC0415

        store = TranscriptStore(session_id)
        try:
            store.delete_previous_notes(Path(archive_path))
        except (FileNotFoundError, ValueError, OSError) as exc:
            QMessageBox.warning(
                self.window, "Delete archive",
                f"Couldn't delete archive: {exc}",
            )
            return
        # Refresh the previous-notes list (preserving the synthesis +
        # live-notes editor state, which set_session_selected would
        # reset).
        self.window.session_view.set_previous_notes(store.list_previous_notes())
        self.window.status(
            f"Archive deleted: {Path(archive_path).name}", timeout_ms=5000
        )

    def _on_paste_notes(self, session_id: str) -> None:
        session = self.store.get_session(session_id)
        if session is None:
            return
        dialog = PasteNotesDialog(current_notes="", parent=self.window)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        body = dialog.body
        if not body.strip():
            QMessageBox.information(self.window, "Paste Notes", "Nothing to save (empty input).")
            return
        # Route through the shared synthesis post-processor so the
        # manual paste-back gets identical handling to the Chrome-
        # extension flow: markdown normalize, Attendee Details
        # appendix application to Contact fields, sidecar
        # persistence of all four LLM appendices, strip toggle,
        # save, search re-index, view reload, topic extraction (via
        # the reload's _on_session_content_loaded handler).
        try:
            archive_path = self._apply_synthesis_result(
                session_id, body,
                archive_existing=dialog.archive_existing,
            )
        except OSError:
            log.exception("save_notes failed for %s", session_id)
            QMessageBox.critical(
                self.window, "Paste Notes",
                "Couldn't save the notes to disk; see the log for details.",
            )
            return
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

    # ---- screen capture ---------------------------------------------------

    def _on_start_screen_capture(self, session_id: str) -> None:
        """First-time popup -> RegionPicker -> store region + arm SessionView."""
        if not self.config.ui.screen_capture_first_time_seen:
            QMessageBox.information(
                self.window,
                "Screen capture",
                "Screen capture works by snapshotting a fixed region you "
                "draw on screen.\n\n"
                "ANY content that lands in that rectangle while capture is "
                "active will be saved -- including windows you move into "
                "frame after starting, popups, and overlays. Position your "
                "shared-content window before starting capture, and stop "
                "capture before showing anything you don't want recorded.\n\n"
                "Screenshots stay on this machine, alongside the rest of "
                "the session files. They are never sent to the LLM during "
                "synthesis.\n\n"
                "This notice only appears once.",
            )
            self.config.ui.screen_capture_first_time_seen = True
            self.config.save()

        from .screencap.region_picker import RegionPicker  # noqa: PLC0415
        picker = RegionPicker()
        rect = picker.exec()
        if rect is None or rect.width() < 8 or rect.height() < 8:
            return  # User canceled or drew a degenerate rect.
        self._screen_capture_regions[session_id] = (
            rect.x(), rect.y(), rect.width(), rect.height(),
        )
        self._show_armed_region_overlay(rect)
        self.window.session_view.set_screencap_armed(True)

    def _on_stop_screen_capture(self, session_id: str) -> None:
        self._screen_capture_regions.pop(session_id, None)
        self._stop_auto_capture(session_id)
        self._hide_armed_region_overlay()
        self.window.session_view.set_screencap_armed(False)

    def _show_armed_region_overlay(self, rect) -> None:
        """Build the persistent outline overlay and show it on top."""
        self._hide_armed_region_overlay()
        from .screencap.armed_overlay import ArmedRegionOverlay  # noqa: PLC0415
        overlay = ArmedRegionOverlay(rect)
        overlay.show()
        self._armed_region_overlay = overlay

    def _hide_armed_region_overlay(self) -> None:
        if self._armed_region_overlay is not None:
            self._armed_region_overlay.close()
            self._armed_region_overlay = None

    # ---- auto-capture lifecycle ------------------------------------------

    def _start_auto_capture(self, session_id: str) -> None:
        """Begin periodic screenshots for an armed session.

        Cadence comes from config.ui.screen_capture_auto_interval_sec.
        Each tick goes through the dedup gate -- captures whose
        dHash is within the threshold of the most-recent KEPT image
        are deleted; only differing captures stick around. Manual
        Capture / Insert clicks bypass the dedup check and reset
        the baseline.
        """
        if session_id in self._auto_capture_timers:
            return  # already running
        if self._screen_capture_regions.get(session_id) is None:
            log.warning(
                "auto-capture start requested but no region armed for %s",
                session_id,
            )
            return
        interval_ms = max(
            1000,
            int(self.config.ui.screen_capture_auto_interval_sec * 1000),
        )
        timer = QTimer(self)
        timer.setInterval(interval_ms)
        timer.timeout.connect(
            lambda sid=session_id: self._auto_capture_tick(sid),
        )
        self._auto_capture_timers[session_id] = timer
        timer.start()
        log.info(
            "auto-capture started for %s (interval=%d ms, threshold=%d bits)",
            session_id, interval_ms,
            self.config.ui.screen_capture_auto_dedup_threshold,
        )

    def _stop_auto_capture(self, session_id: str) -> None:
        timer = self._auto_capture_timers.pop(session_id, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
            log.info("auto-capture stopped for %s", session_id)
        # Drop the baseline too so the next session starts clean.
        self._auto_capture_baseline_hash.pop(session_id, None)

    def _on_auto_capture_toggled(self, session_id: str, enabled: bool) -> None:
        """SessionView pushes the per-session checkbox state here."""
        if enabled:
            self._start_auto_capture(session_id)
        else:
            self._stop_auto_capture(session_id)

    def _auto_capture_tick(self, session_id: str) -> None:
        """Periodic auto-capture body. Same as _capture_screenshot but
        passes the result through the dedup gate."""
        region = self._screen_capture_regions.get(session_id)
        if region is None:
            # Region was disarmed while the timer was pending; stop.
            self._stop_auto_capture(session_id)
            return
        from .screencap.dedup import dhash_path, is_dedup_match  # noqa: PLC0415
        from .utils.paths import session_screenshots_dir  # noqa: PLC0415
        dst_dir = session_screenshots_dir(session_id)
        saved = self._capture_region_hiding_overlay(region, dst_dir)
        if saved is None:
            log.warning("auto-capture: capture_region_to_file returned None")
            return
        new_hash = dhash_path(saved)
        baseline = self._auto_capture_baseline_hash.get(session_id)
        threshold = int(self.config.ui.screen_capture_auto_dedup_threshold)
        if new_hash is not None and is_dedup_match(new_hash, baseline, threshold):
            # Too similar; delete and skip the UI refresh.
            try:
                saved.unlink()
            except OSError:
                log.exception("auto-capture: failed to unlink dedup %s", saved)
            return
        if new_hash is not None:
            self._auto_capture_baseline_hash[session_id] = new_hash
        self.window.session_view.refresh_screenshots()
        self._push_screenshot_offsets(session_id)
        log.info("auto-capture: kept %s", saved.name)

    def _on_screencap_capture(self, session_id: str) -> None:
        self._capture_screenshot(session_id, insert=False)

    def _on_screencap_insert(self, session_id: str) -> None:
        self._capture_screenshot(session_id, insert=True)

    def _capture_screenshot(self, session_id: str, *, insert: bool) -> None:
        """Shared body for the manual Capture and Insert buttons.

        Manual captures (this path) ALWAYS keep the saved image and
        update the dedup baseline -- the user explicitly asked for
        this image so an auto-dedup check would be wrong here. The
        auto-capture path uses _auto_capture_tick, which goes
        through the dedup check.
        """
        region = self._screen_capture_regions.get(session_id)
        if region is None:
            self.window.status(
                "Screen capture is not armed. Click Start Screen Capture first.",
                timeout_ms=5000,
            )
            return
        from .screencap.dedup import dhash_path  # noqa: PLC0415
        from .utils.paths import session_screenshots_dir  # noqa: PLC0415
        dst_dir = session_screenshots_dir(session_id)
        saved = self._capture_region_hiding_overlay(region, dst_dir)
        if saved is None:
            self.window.status(
                "Screen capture failed (see log).", timeout_ms=5000,
            )
            return
        # Update the dedup baseline so the next auto-capture compares
        # against THIS manual image, not whatever came before.
        new_hash = dhash_path(saved)
        if new_hash is not None:
            self._auto_capture_baseline_hash[session_id] = new_hash
        self.window.session_view.refresh_screenshots()
        # Re-push offsets so the new capture anchors into the rail
        # and shows up as a candidate for the playback-mode auto-
        # advance.
        self._push_screenshot_offsets(session_id)
        if insert:
            # Relative path anchored at the session dir so the My Notes
            # preview's setSearchPaths resolves it.
            relative = f"screenshots/{saved.name}"
            self.window.session_view.insert_screenshot_markdown(relative)
        self.window.status(
            f"Screenshot saved: {saved.name}", timeout_ms=4000,
        )

    def _capture_region_hiding_overlay(self, region, dst_dir):
        """Hide the armed-region outline, take the screenshot, restore.

        mss reads the OS-composited screen state, which includes our
        cyan ArmedRegionOverlay outline -- so without this dance the
        outline ends up in every captured PNG. Hiding the widget
        before the grab + processing the hide event + a short sleep
        gives the Windows compositor a frame to redraw the area
        underneath. Restoring after the grab leaves the on-screen
        outline as before for the rest of the armed session.
        """
        from .screencap.capture import capture_region_to_file  # noqa: PLC0415
        overlay = self._armed_region_overlay
        was_visible = overlay is not None and overlay.isVisible()
        if was_visible:
            overlay.hide()
            QApplication.processEvents()
            # ~50 ms gives the OS compositor a frame to repaint the
            # region underneath the overlay before mss's BitBlt
            # pulls the screen state. Shorter intervals occasionally
            # leave a ghost on Windows; 50 ms is the smallest
            # reliable value we've found in practice.
            time.sleep(0.05)
        try:
            return capture_region_to_file(region, dst_dir)
        finally:
            if was_visible and overlay is not None:
                overlay.show()

    def _on_delete_screenshot(self, session_id: str, path) -> None:
        try:
            path.unlink()
        except OSError:
            log.exception("could not delete screenshot %s", path)
            return
        # Only the active session's SlidesWidget needs refreshing; if
        # the user has navigated away, the next set_session will pick
        # up the new state from disk.
        self.window.session_view.refresh_screenshots()
        # Re-push offsets so the rail and Slides-tab auto-advance
        # drop the deleted screenshot from their anchor list.
        self._push_screenshot_offsets(session_id)
        self.window.status(
            f"Deleted screenshot: {path.name}", timeout_ms=4000,
        )

    # ---- transcript playback ----------------------------------------------

    def _ensure_audio_player(self):
        """Lazy-build the AudioPlayer + wire its signals into the view."""
        if self._audio_player is not None:
            return self._audio_player
        from .audio.player import AudioPlayer  # noqa: PLC0415
        player = AudioPlayer(self)
        player.loaded.connect(self._on_player_loaded)
        player.load_failed.connect(self._on_player_load_failed)
        player.position_changed.connect(self._on_player_position_changed)
        player.playback_finished.connect(self._on_player_finished)
        self._audio_player = player
        return player

    def _push_screenshot_offsets(self, session_id: str) -> None:
        """Compute (path, offset_ms) for every screenshot + push to view.

        Called on session select AND after capture / delete so both
        the side rail and the Slides tab's auto-advance stay in sync
        with whatever's on disk.
        """
        from .screencap.timestamps import screenshot_offsets  # noqa: PLC0415
        from .utils.paths import list_screenshots  # noqa: PLC0415
        session = self.store.get_session(session_id)
        if session is None:
            self.window.session_view.set_screenshot_offsets([])
            return
        paths = list_screenshots(session_id)
        offsets = screenshot_offsets(paths, session.started_at)
        self.window.session_view.set_screenshot_offsets(offsets)

    def _maybe_load_player_for_session(self, session_id: str) -> None:
        """Load the session's retained audio into the player, if any.

        Only loads when the session is in a terminal state
        (COMPLETE / ERROR). Mid-recording (RECORDING / PAUSED) the
        WAVs are growing under us, and during PROCESSING the encoder
        rewrites them to opus -- in both cases any snapshot we take
        is stale by the time the user clicks Play. (Aaron's bug:
        loading during RECORDING produced a 92 ms buffer that stayed
        cached through to COMPLETE; restarting the app fixed it by
        forcing a fresh load against the final opus files.)
        """
        from .utils.paths import session_audio_files  # noqa: PLC0415
        # If we're loaded for this session, never reload mid-state-
        # change unless the state forces an invalidation upstream
        # (handled in _on_session_state_changed).
        if self._player_loaded_session_id == session_id:
            return
        session = self.store.get_session(session_id)
        if session is not None and session.state not in (STATE_COMPLETE, STATE_ERROR):
            # The recording is still in flight (or being encoded).
            # Keep the player torn down so the bar stays disabled;
            # the next state transition will trigger a fresh load.
            if self._audio_player is not None:
                self._audio_player.close()
            self._player_loaded_session_id = None
            self.window.session_view.set_player_loading_state(False)
            self.window.session_view.set_player_enabled(False)
            return
        files = session_audio_files(session_id)
        if not files:
            if self._audio_player is not None and self._player_loaded_session_id:
                self._audio_player.close()
                self._player_loaded_session_id = None
            self.window.session_view.set_player_loading_state(False)
            self.window.session_view.set_player_enabled(False)
            return
        # Separate the mic and sys halves out of the list. The path
        # helper sorts mic-first-then-sys so we can index, but go
        # safer and discriminate by stem.
        mic_path = next((p for p in files if p.stem == "mic"), None)
        sys_path = next((p for p in files if p.stem == "sys"), None)
        player = self._ensure_audio_player()
        # AudioPlayer.load() is async (issue #31): the PyAV decode +
        # mix runs on a worker QThread and the loaded / load_failed
        # signals fire when it finishes. Disable the player controls
        # until then so the user can't click Play on a buffer that
        # isn't ready, and so a long decode (1-3 s for a multi-hour
        # meeting) doesn't gray-out the whole window.
        self.window.session_view.set_player_enabled(False)
        # Surface the in-flight decode so the player bar reads
        # "Loading audio..." instead of "--:-- / --:--" -- without
        # this, a multi-minute meeting's decode pause looked like
        # the player was broken (#61).
        self.window.session_view.set_player_loading_state(True)
        try:
            player.load(mic_path, sys_path)
        except Exception:
            log.exception("AudioPlayer.load raised")
            self.window.session_view.set_player_loading_state(False)
            self._player_loaded_session_id = None
            return
        self._player_loaded_session_id = session_id

    def _on_player_loaded(self, total_ms: int) -> None:
        sv = self.window.session_view
        sv.set_player_loading_state(False)
        sv.set_player_total_ms(total_ms)
        sv.set_player_position_ms(0)
        sv.set_player_is_playing(False)
        sv.set_player_enabled(True)

    def _on_player_load_failed(self, message: str) -> None:
        self.window.session_view.set_player_loading_state(False)
        self.window.status(message, timeout_ms=5000)
        self.window.session_view.set_player_enabled(False)
        self._player_loaded_session_id = None

    def _on_player_position_changed(self, ms: int) -> None:
        self.window.session_view.set_player_position_ms(int(ms))

    def _on_player_finished(self) -> None:
        # Natural end-of-playback: revert to the side-rail layout
        # (per Aaron's "at end of playback, revert to normal view"
        # spec) and reset the playhead. set_player_position_ms(0)
        # would re-enter the playback layout in v0.6.5; call the
        # idle-revert AFTER so the layout stays on idle.
        sv = self.window.session_view
        sv.set_player_is_playing(False)
        sv.set_player_position_ms(0)
        sv.revert_to_idle_layout()

    def _on_transcript_play(self, _session_id: str) -> None:
        if self._audio_player is None:
            return
        self._audio_player.play()
        self.window.session_view.set_player_is_playing(True)

    def _on_transcript_pause(self, _session_id: str) -> None:
        if self._audio_player is None:
            return
        self._audio_player.pause()
        self.window.session_view.set_player_is_playing(False)

    def _on_transcript_seek(self, _session_id: str, ms: int) -> None:
        if self._audio_player is None:
            return
        self._audio_player.seek_ms(int(ms))

    # ---- recording context-menu actions -----------------------------------

    def _on_open_recording(self, session_id: str) -> None:
        """Launch the OS default media player on the session's recording.

        Tries the mic file first (always present when audio is retained),
        falls back to whichever exists in session_audio_files(). The
        user can replay either side from the player's open-file dialog;
        we just need to give them an entry point.
        """
        from PyQt6.QtCore import QUrl  # noqa: PLC0415
        from PyQt6.QtGui import QDesktopServices  # noqa: PLC0415
        from .utils.paths import session_audio_files  # noqa: PLC0415
        files = session_audio_files(session_id)
        if not files:
            self.window.status("No recording on disk for this session.", timeout_ms=5000)
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(files[0]))):
            self.window.status(
                f"Could not open {files[0].name} (no default player?)",
                timeout_ms=5000,
            )

    def _on_export_recording(self, session_id: str) -> None:
        """Prompt for a destination + format, mix mic+sys, write the file.

        QFileDialog's filter list drives which container the exporter
        uses. MP3 is the default filter -- it plays natively in every
        Windows player without codec packs and is the "share this with
        a colleague" lingua franca. The encode runs on a worker thread
        with an indeterminate progress dialog so the UI stays
        responsive on a long meeting (decoding a 1-hour stereo source
        takes a few seconds on a fast machine).
        """
        from PyQt6.QtWidgets import (  # noqa: PLC0415
            QFileDialog, QMessageBox, QProgressDialog,
        )
        from .audio.export import known_extensions  # noqa: PLC0415
        from .utils.export import default_export_filename  # noqa: PLC0415
        from .utils.paths import session_audio_files  # noqa: PLC0415

        files = session_audio_files(session_id)
        if not files:
            self.window.status(
                "No recording on disk for this session.", timeout_ms=5000,
            )
            return
        # If the user has marked highlights, ask whether to export
        # the full session or just the highlights. No highlights ->
        # silent fallthrough to full-session export (original
        # behavior unchanged).
        highlight_mode = self._prompt_highlights_or_full(session_id)
        if highlight_mode is None:
            return  # user cancelled
        session = self.store.get_session(session_id)
        title = session.title if session is not None else "session"

        # Build the QFileDialog filter string. MP3 sits first so it's
        # the default selection -- the common case is "send a colleague
        # a playable file" and MP3 is the safest pick for that.
        ext_labels = {
            ".mp3":  "MP3 -- universal compatibility (*.mp3)",
            ".flac": "FLAC -- lossless (*.flac)",
            ".m4a":  "AAC -- broad compatibility (*.m4a)",
            ".opus": "Opus -- smallest (*.opus)",
            ".wav":  "WAV -- raw PCM (*.wav)",
        }
        filters = ";;".join(ext_labels[e] for e in known_extensions())

        # Suggested filename uses MP3 to match the default filter so
        # the .mp3 extension is pre-typed; the user can switch the
        # filter dropdown to change format.
        suggested = default_export_filename(title, "Audio", ".mp3")
        suggested_path = str(files[0].parent / suggested)
        path_str, chosen_filter = QFileDialog.getSaveFileName(
            self.window,
            "Export recording",
            suggested_path,
            filters,
        )
        if not path_str:
            return
        target = Path(path_str)
        ext_for_filter = next(
            (ext for ext, label in ext_labels.items() if label == chosen_filter),
            None,
        )
        if ext_for_filter and target.suffix.lower() != ext_for_filter:
            target = target.with_suffix(ext_for_filter)

        mic_path = next((p for p in files if p.stem == "mic"), None)
        sys_path = next((p for p in files if p.stem == "sys"), None)

        # Worker thread + indeterminate progress dialog. Indeterminate
        # (range 0,0) gives a marquee animation since we can't easily
        # report PyAV encoder progress per-frame across the thread
        # boundary. The dialog is modal so the user can't interact
        # with the rest of the app mid-encode (avoiding the
        # session-deselect-during-encode race). Cancel (#60) is
        # honored at the highlights-slicer + chunk boundaries; the
        # full-recording mp3 path doesn't yet poll cancellation
        # because it has no progress callback to attach the check
        # to, so cancel on that path waits for the encoder to
        # finish on its own (acceptable -- it's the fast path).
        cancel_label = "Cancel" if highlight_mode == "highlights" else None
        progress = QProgressDialog(
            f"Exporting {target.name}...",
            cancel_label,
            0, 0, self.window,
        )
        progress.setWindowTitle("Export recording")
        progress.setMinimumDuration(0)
        progress.setAutoClose(True)
        progress.setAutoReset(True)
        if cancel_label is None:
            progress.setCancelButton(None)

        if highlight_mode == "highlights":
            highlights = self._session_highlights(session_id).sorted_by_start()
            worker = _HighlightAudioExportWorker(
                mic_path, sys_path, highlights, target,
            )
            progress.canceled.connect(worker.cancel)
        else:
            worker = _AudioExportWorker(mic_path, sys_path, target)

        def on_done(err_msg: str) -> None:
            progress.cancel()
            cancelled = err_msg == _HighlightAudioExportWorker.CANCELLED_RESULT
            if cancelled:
                self.window.status(
                    "Audio export cancelled.", timeout_ms=4000,
                )
            elif err_msg:
                log.error("export_mixed failed: %s", err_msg)
                QMessageBox.warning(
                    self.window, "Export recording",
                    f"Could not export audio: {err_msg}",
                )
            else:
                self.window.status(
                    f"Exported recording to {target.name}", timeout_ms=5000,
                )
            # wait() before deleteLater() closes a PyQt race: the
            # finished_with_result signal is emitted from run() but
            # the OS thread isn't guaranteed to be joined yet when
            # the queued slot fires on the main thread. If we call
            # deleteLater() while isRunning() is still True, Qt's
            # destructor (when it runs at the next event loop tick)
            # logs "QThread: Destroyed while thread '...' is still
            # running" and the process can crash. wait() blocks the
            # main thread for at most a few ms (the worker already
            # returned from run()) and guarantees the join.
            worker.wait()
            worker.deleteLater()

        worker.finished_with_result.connect(on_done)
        progress.show()
        worker.start()

    def _on_export_video(self, session_id: str) -> None:
        """Render the session as a slideshow MP4 with mixed audio + SRT.

        Builds the screenshot offset list from disk, reads the transcript
        text, then runs the encode on a worker thread with a determinate
        progress dialog. PyAV reports per-frame progress out of the
        encoder so the user sees real movement, not just a marquee.
        Output is a single MP4 plus a same-named .srt sidecar; the SRT
        is what makes subtitles toggleable in every standard player
        without baking them into the video stream.
        """
        from PyQt6.QtWidgets import (  # noqa: PLC0415
            QFileDialog, QMessageBox, QProgressDialog,
        )
        from .models.transcript import TranscriptStore  # noqa: PLC0415
        from .screencap.timestamps import screenshot_offsets  # noqa: PLC0415
        from .utils.export import default_export_filename  # noqa: PLC0415
        from .utils.paths import (  # noqa: PLC0415
            list_screenshots, session_audio_files,
        )

        files = session_audio_files(session_id)
        if not files:
            self.window.status(
                "No recording on disk for this session.", timeout_ms=5000,
            )
            return
        session = self.store.get_session(session_id)
        if session is None:
            self.window.status(
                "Session metadata missing; cannot export.", timeout_ms=5000,
            )
            return
        # Highlights present -> ask Full or Highlights-only.
        highlight_mode = self._prompt_highlights_or_full(session_id)
        if highlight_mode is None:
            return
        title = session.title

        suggested = default_export_filename(title, "Video", ".mp4")
        suggested_path = str(files[0].parent / suggested)
        path_str, _ = QFileDialog.getSaveFileName(
            self.window,
            "Export session as video",
            suggested_path,
            "MP4 video -- H.264 + AAC (*.mp4)",
        )
        if not path_str:
            return
        target = Path(path_str)
        if target.suffix.lower() != ".mp4":
            target = target.with_suffix(".mp4")

        mic_path = next((p for p in files if p.stem == "mic"), None)
        sys_path = next((p for p in files if p.stem == "sys"), None)
        offsets = screenshot_offsets(
            list_screenshots(session_id), session.started_at,
        )
        try:
            transcript_text = TranscriptStore(session_id).read_transcript()
        except OSError:
            log.exception("could not read transcript for %s", session_id)
            transcript_text = ""

        # The "Cancel" label gives the user the abort button #60
        # asks for; the encoder loops poll the worker's cancel flag
        # at frame boundaries and raise ExportCancelled at the next
        # checkpoint. Partial mp4 + sidecar SRT are deleted by
        # video_export's cleanup path.
        progress = QProgressDialog(
            f"Rendering {target.name}...",
            "Cancel",
            0, 100, self.window,
        )
        progress.setWindowTitle("Export session as video")
        progress.setMinimumDuration(0)
        progress.setAutoClose(True)
        progress.setAutoReset(True)
        progress.setValue(0)

        if highlight_mode == "highlights":
            highlights = self._session_highlights(session_id).sorted_by_start()
            # Pass session.title + session.started_at so the export
            # can prepend an initial session-title card carrying
            # the title on line 1 and "Recorded on YYYY-MM-DD HH:MM"
            # on line 2. Falls back to created_at when the session
            # was never recorded -- structurally impossible to have
            # highlights without a recording, but cheap to guard.
            started_at_iso = session.started_at or session.created_at or ""
            worker = _HighlightVideoExportWorker(
                mic_path, sys_path, offsets, transcript_text,
                highlights, target,
                session_title=session.title,
                session_started_at_iso=started_at_iso,
                quality=self.config.synthesis.video_quality,
            )
        else:
            worker = _VideoExportWorker(
                mic_path, sys_path, offsets, transcript_text, target,
                quality=self.config.synthesis.video_quality,
            )

        def on_progress(pct: int) -> None:
            progress.setValue(pct)

        def on_done(err_msg: str) -> None:
            progress.cancel()
            cancelled = err_msg == _VideoExportWorker.CANCELLED_RESULT
            if cancelled:
                self.window.status(
                    "Video export cancelled.", timeout_ms=4000,
                )
            elif err_msg:
                log.error("export_video failed: %s", err_msg)
                QMessageBox.warning(
                    self.window, "Export session as video",
                    f"Could not render video: {err_msg}",
                )
            else:
                self.window.status(
                    f"Exported video to {target.name}", timeout_ms=5000,
                )
            worker.wait()  # see comment in _on_export_mixed_audio
            worker.deleteLater()

        worker.progress_changed.connect(on_progress)
        worker.finished_with_result.connect(on_done)
        # QProgressDialog.canceled fires when the user clicks Cancel.
        # The worker keeps running until the next checkpoint -- we
        # just flag the cancel and let the encoder unwind.
        progress.canceled.connect(worker.cancel)
        progress.show()
        worker.start()

    def _on_export_package(self, session_id: str) -> None:
        """Full-session ZIP export (issue #30).

        Builds an archive containing PDFs of My Notes + Synthesis,
        plaintext transcript, MP3 audio, MP4 video (if screenshots
        exist), all attachments, and all screenshots. When highlights
        exist, prompts once for Full / Highlights-only / Both.
        """
        import tempfile  # noqa: PLC0415
        from datetime import datetime  # noqa: PLC0415

        from PyQt6.QtWidgets import (  # noqa: PLC0415
            QFileDialog, QMessageBox,
        )
        from .models.attachments import AttachmentsStore  # noqa: PLC0415
        from .models.highlights import HighlightsStore  # noqa: PLC0415
        from .models.transcript import TranscriptStore  # noqa: PLC0415
        from .ui.export_progress_dialog import (  # noqa: PLC0415
            ExportProgressDialog,
        )
        from .screencap.timestamps import screenshot_offsets  # noqa: PLC0415
        from .utils.export_package import (  # noqa: PLC0415
            HIGHLIGHTS_MODE_BOTH,
            HIGHLIGHTS_MODE_FULL,
            HIGHLIGHTS_MODE_HIGHLIGHTS,
            PackageOptions,
            default_package_filename,
            render_session_pdf,
        )
        from .utils.paths import (  # noqa: PLC0415
            list_screenshots, session_audio_files, session_dir,
        )

        session = self.store.get_session(session_id)
        if session is None:
            return

        # Pre-flight: ask about highlights mode when applicable.
        highlights = []
        try:
            highlights = HighlightsStore(session_id).load().sorted_by_start()
        except Exception:
            highlights = []
        if highlights:
            dialog = QMessageBox(self.window)
            dialog.setWindowTitle("Export full session")
            dialog.setText(
                f"This session has {len(highlights)} highlight(s). For "
                "the audio + video files, export:"
            )
            full_btn = dialog.addButton(
                "Full recording only", QMessageBox.ButtonRole.AcceptRole,
            )
            high_btn = dialog.addButton(
                "Highlights only", QMessageBox.ButtonRole.AcceptRole,
            )
            both_btn = dialog.addButton(
                "Both", QMessageBox.ButtonRole.AcceptRole,
            )
            cancel_btn = dialog.addButton(QMessageBox.StandardButton.Cancel)
            dialog.setDefaultButton(both_btn)
            dialog.exec()
            clicked = dialog.clickedButton()
            if clicked is cancel_btn:
                return
            elif clicked is full_btn:
                highlights_mode = HIGHLIGHTS_MODE_FULL
            elif clicked is high_btn:
                highlights_mode = HIGHLIGHTS_MODE_HIGHLIGHTS
            else:
                highlights_mode = HIGHLIGHTS_MODE_BOTH
        else:
            highlights_mode = HIGHLIGHTS_MODE_FULL

        # Save / folder picker. When compress is OFF (#62), prompt
        # for a parent directory and build a subfolder under it
        # named like the zip would have been. When ON, the original
        # save-file flow with .zip suffix.
        compress = self.config.synthesis.compress_full_session_export
        suggested = default_package_filename(
            session.title or "session",
            session.started_at or session.created_at or "",
        )
        if compress:
            suggested_path = str(Path.home() / "Documents" / suggested)
            path_str, _ = QFileDialog.getSaveFileName(
                self.window, "Export full session",
                suggested_path,
                "ZIP archive (*.zip)",
            )
            if not path_str:
                return
            target = Path(path_str)
            if target.suffix.lower() != ".zip":
                target = target.with_suffix(".zip")
        else:
            parent_dir = QFileDialog.getExistingDirectory(
                self.window, "Choose a folder for the export",
                str(Path.home() / "Documents"),
            )
            if not parent_dir:
                return
            # The .zip suffix is the part that distinguishes compressed
            # output from folder output -- strip it for the subfolder
            # name. default_package_filename always emits a name with
            # the .zip suffix.
            folder_name = suggested
            if folder_name.lower().endswith(".zip"):
                folder_name = folder_name[:-4]
            target = Path(parent_dir) / folder_name

        # Gather inputs.
        store = TranscriptStore(session_id)
        notes_md = ""
        synthesis_md = ""
        transcript_text = ""
        try:
            notes_md = store.read_live_notes()
        except OSError:
            log.exception("notes read failed for %s", session_id)
        try:
            synthesis_md = store.read_notes()
        except OSError:
            log.exception("synthesis read failed for %s", session_id)
        try:
            transcript_text = store.read_transcript()
        except OSError:
            log.exception("transcript read failed for %s", session_id)

        audio_files = session_audio_files(session_id)
        mic_path = next(
            (p for p in audio_files if p.stem == "mic"), None,
        )
        sys_path = next(
            (p for p in audio_files if p.stem == "sys"), None,
        )
        screenshots = screenshot_offsets(
            list_screenshots(session_id), session.started_at or "",
        )

        # Attachments -- copy the file + display name pairs into the
        # orchestrator. The display name lands as the on-disk
        # filename in the package, with FS-safe normalization.
        attachments_pairs: list[tuple[Path, str]] = []
        try:
            attach_store = AttachmentsStore(session_id)
            for rec in attach_store.list():
                path = attach_store.file_path(rec.id)
                if path is not None:
                    attachments_pairs.append((path, rec.display_name))
        except Exception:
            log.exception("attachments enumerate failed for %s", session_id)

        # Pre-render the two PDFs on the main thread (Bug fix):
        # QTextDocument + QPrinter are GUI classes; cross-thread use
        # produced spurious dialog flashes after the worker finished
        # and the PDFs themselves rendered with the wrong font size
        # vs. the in-app Export PDF button. Doing the render here
        # uses the same PrintTextDocument path the user sees in the
        # in-app Export PDF and keeps everything on the main thread.
        pdf_temp_dir = Path(tempfile.mkdtemp(prefix="mtn-export-pdfs-"))
        session_when = None
        try:
            session_when = datetime.fromisoformat(
                (session.created_at or "").replace("Z", "+00:00")
            ).astimezone()
        except ValueError:
            session_when = None
        sdir = session_dir(session_id)
        # v0.7.2 #51 Phase 5: fetch the session's linked Contacts so the
        # PDF render path can swap the Attendees bullet list for a rich
        # table when any contact has detail fields populated.
        session_contacts_for_pdf: list = []
        if self.classification is not None:
            try:
                session_contacts_for_pdf = [
                    sc.contact
                    for sc in self.classification.contacts_for_session(session_id)
                ]
            except Exception:
                log.exception("contact fetch failed for export PDF: %s", session_id)
        # #64: build the AppendixData payload the PDF render path
        # uses to inject the rendered "## Appendix (auto-extracted)"
        # section. Uses the sidecar-backed collector so a stripped
        # synthesis still produces the full appendix (sidecar
        # followup).
        try:
            from .utils.appendix_store import collect_for_session  # noqa: PLC0415
            attach_store = AttachmentsStore(session_id)
            attachment_names = [
                rec.display_name for rec in attach_store.list()
            ]
        except Exception:
            log.exception(
                "attachment names for PDF appendix failed: %s", session_id,
            )
            attachment_names = []
        appendix_data = collect_for_session(
            session_id=session_id,
            notes_text=synthesis_md or "",
            live_notes_text=notes_md or "",
            session_attachments=attachment_names,
        )
        # Prompt the user for which Appendix sub-sections to bundle.
        # Cancel aborts the whole export so the file dialog choice +
        # highlights mode pick aren't wasted.
        from .ui.appendix_inclusion_dialog import (  # noqa: PLC0415
            AppendixInclusionDialog,
            apply_inclusion,
        )
        inc_dlg = AppendixInclusionDialog(
            appendix_data,
            export_label="full-session export",
            defaults=self._appendix_export_defaults_from_config(),
            parent=self.window,
        )
        if inc_dlg.exec() != AppendixInclusionDialog.DialogCode.Accepted:
            return
        appendix_data = apply_inclusion(appendix_data, inc_dlg.inclusion())
        notes_pdf_path = None
        synthesis_pdf_path = None
        try:
            if notes_md.strip():
                notes_pdf_path = pdf_temp_dir / "my-notes.pdf"
                render_session_pdf(
                    notes_md,
                    dst=notes_pdf_path,
                    base_dir=sdir,
                    session_title=session.title or "session",
                    tab_label="My Notes",
                    session_date=session_when,
                    session_contacts=session_contacts_for_pdf,
                    appendix_data=appendix_data,
                )
            if synthesis_md.strip():
                synthesis_pdf_path = pdf_temp_dir / "synthesis.pdf"
                render_session_pdf(
                    synthesis_md,
                    dst=synthesis_pdf_path,
                    base_dir=sdir,
                    session_title=session.title or "session",
                    tab_label="Synthesis",
                    session_date=session_when,
                    session_contacts=session_contacts_for_pdf,
                    appendix_data=appendix_data,
                )
        except Exception:
            log.exception("PDF pre-render failed for %s", session_id)

        options = PackageOptions(
            session_id=session_id,
            session_title=session.title or "session",
            session_started_at_iso=session.started_at or session.created_at or "",
            mic_path=mic_path,
            sys_path=sys_path,
            screenshots=screenshots,
            transcript_text=transcript_text,
            notes_pdf_path=notes_pdf_path,
            synthesis_pdf_path=synthesis_pdf_path,
            attachments=attachments_pairs,
            highlights=highlights,
            highlights_mode=highlights_mode,
            video_quality=self.config.synthesis.video_quality,
        )

        # ExportProgressDialog couples a determinate bar, a current
        # phase label, a Cancel button (#60), and a scrolling log
        # of every status emit from the worker (#53).
        header = (
            f"Building {target.name}.zip..." if compress
            else f"Building {target.name}/..."
        )
        progress = ExportProgressDialog(self.window, header)

        worker = _ExportPackageWorker(options, target, compress=compress)

        def on_done(err_msg: str) -> None:
            # Close the dialog before showing any follow-up message
            # so we never have two top-level QDialogs visible at the
            # same time (cause of the 'multiple flashing dialogs'
            # bug Aaron caught -- combining autoClose + autoReset
            # with an immediate post-close QMessageBox produced
            # transient ghost windows).
            cancelled = err_msg == _ExportPackageWorker.CANCELLED_RESULT
            progress.close_with_result("" if cancelled else err_msg)
            # Clean up the temp PDF dir; it's cheap to keep but
            # we're disciplined about temp leaks.
            try:
                import shutil
                shutil.rmtree(pdf_temp_dir, ignore_errors=True)
            except Exception:
                pass
            if cancelled:
                self.window.status(
                    "Full-session export cancelled.", timeout_ms=4000,
                )
            elif err_msg:
                log.error("export_package failed: %s", err_msg)
                QMessageBox.warning(
                    self.window, "Export full session",
                    f"Could not build the export package:\n\n{err_msg}",
                )
            else:
                self.window.status(
                    f"Exported full session to {target.name}",
                    timeout_ms=5000,
                )
            worker.wait()  # see comment in _on_export_mixed_audio
            worker.deleteLater()

        worker.progress_changed.connect(progress.set_progress)
        worker.status_changed.connect(progress.append_status)
        worker.finished_with_result.connect(on_done)
        progress.cancel_requested.connect(worker.cancel)
        progress.show()
        worker.start()

    def _on_delete_recording(self, session_id: str) -> None:
        """Delete the session's audio files; keep transcript + notes."""
        from .utils.paths import session_audio_files  # noqa: PLC0415
        removed = 0
        for path in session_audio_files(session_id):
            try:
                path.unlink()
                removed += 1
            except OSError:
                log.exception("could not unlink recording %s", path)
        # Flip the on-disk has_audio flag so the session-list audio
        # column matches reality, and so a future Open in player call
        # has nothing to look at.
        self.store.update_session(session_id, has_audio=False)
        self._refresh_session_list()
        self.window.status(
            f"Deleted recording ({removed} file{'s' if removed != 1 else ''}).",
            timeout_ms=5000,
        )

    # ---- bulk delete -------------------------------------------------------

    def _on_delete_sessions(self, session_ids: list[str]) -> None:
        import shutil
        from .utils.paths import session_dir

        for sid in session_ids:
            try:
                shutil.rmtree(session_dir(sid), ignore_errors=True)
            except OSError:
                log.exception("failed to remove session dir for %s", sid)
        # Drop the deleted sessions out of the search index BEFORE the
        # store delete; the FTS5 store doesn't know about the SQL
        # cascade but a stale row would surface in the next search
        # until the periodic catch-up scan ran.
        if self.search_index is not None:
            for sid in session_ids:
                try:
                    self.search_index.remove_session(sid)
                except Exception:
                    log.exception("search remove failed for %s", sid)
        # Classification associations live in a separate DB; same
        # explicit cleanup pattern.
        if self.classification is not None:
            for sid in session_ids:
                try:
                    self.classification.remove_session(sid)
                except Exception:
                    log.exception("classification remove failed for %s", sid)
        removed = self.store.delete_sessions(session_ids)
        self._refresh_session_list()
        self.window.session_view.set_session(None, transcript="", notes="", previous_notes_paths=[])
        if self._audio_player is not None:
            self._audio_player.close()
            self._player_loaded_session_id = None
        self.window.status(f"Deleted {removed} session(s)", timeout_ms=5000)

    # ---- search ------------------------------------------------------------

    def _reindex_search_for(self, session_id: str) -> None:
        """Trigger a one-off reindex for a specific session.

        Cheap to call from any save site: the indexer fingerprints
        the four content files and no-ops when nothing changed.
        Any exception is swallowed -- search is an enhancement, not
        load-bearing for transcription / synthesis.
        """
        if not session_id or self.search_index is None:
            return
        try:
            from .utils.search_indexer import reindex_session
            reindex_session(self.search_index, session_id)
        except Exception:
            log.exception("search reindex failed for %s", session_id)

    def _scan_search_index_stale(self) -> None:
        """30s periodic sweep: re-index any session whose on-disk
        fingerprint differs from the index. Catches paths that didn't
        get an explicit _reindex_search_for hook.

        Runs on a worker QThread (issue #37) so the per-session
        fingerprint stat + SQLite point query doesn't hold the main
        event loop. With 1000 sessions the old inline scan blocked
        the UI for ~2 s every 30 s.
        """
        if self.search_index is None:
            return
        self._dispatch_search_scan(kind="periodic")

    def _search_index_startup_scan(self) -> None:
        """Initial pass after launch: catch any edits made offline."""
        if self.search_index is None:
            return
        self._dispatch_search_scan(kind="startup")

    def _dispatch_search_scan(self, *, kind: str) -> None:
        """Spawn a worker thread to walk every session through the
        search-index fingerprint check.

        Skips dispatch when a scan is already in flight -- two scans
        racing on the same SQLite WAL would serialize but still
        double-cost the disk reads.
        """
        if self._search_scan_worker is not None and self._search_scan_worker.isRunning():
            log.debug("search scan already running; skipping new %s scan", kind)
            return
        session_ids = [s.id for s in self.store.list_sessions()]
        worker = _SearchScanWorker(session_ids=session_ids, kind=kind)
        worker.scan_complete.connect(self._on_search_scan_complete)
        worker.finished.connect(lambda w=worker: self._retire_search_scan_worker(w))
        self._search_scan_worker = worker
        worker.start()

    def _on_search_scan_complete(self, kind: str, reindexed: int) -> None:
        if kind == "startup":
            log.info(
                "search index startup scan complete (%d session(s) reindexed)",
                reindexed,
            )
        elif reindexed > 0:
            log.debug(
                "search index periodic scan reindexed %d session(s)", reindexed,
            )

    def _retire_search_scan_worker(self, worker) -> None:
        try:
            worker.wait()
        except Exception:
            log.exception("search scan worker wait failed")
        try:
            worker.deleteLater()
        except Exception:
            log.exception("search scan worker deleteLater failed")
        if self._search_scan_worker is worker:
            self._search_scan_worker = None

    def _session_summary_for_search(self, session_id: str) -> Optional[SessionSummary]:
        """Adapter for the search dialog's session_lookup parameter."""
        s = self.store.get_session(session_id)
        if s is None:
            return None
        return SessionSummary(
            session_id=s.id, title=s.title, created_at=s.created_at,
        )

    def _on_open_search(self) -> None:
        """Ctrl+Shift+F handler: surface the cross-session search
        dialog, creating it on first use. Subsequent opens reuse the
        same dialog so the user's query + results aren't wiped when
        they Cmd+Tab away."""
        if self.search_index is None:
            QMessageBox.warning(
                self.window, "Search unavailable",
                "Could not open the search index. Try Help > Debug > "
                "Rebuild Search Index, or check the log for an "
                "underlying SQLite error.",
            )
            return
        if self._search_dialog is None:
            self._search_dialog = SearchDialog(
                self.search_index,
                self._session_summary_for_search,
                parent=self.window,
            )
            self._search_dialog.result_chosen.connect(self._on_search_result_chosen)
        self._search_dialog.show()
        self._search_dialog.raise_()
        self._search_dialog.activateWindow()
        self._search_dialog.focus_input()

    def _on_search_result_chosen(
        self, session_id: str, source: str, archive_name: object,
    ) -> None:
        """User double-clicked a hit -- select the session + drill
        into the matching tab. archive_name is a str only when the
        hit came from a notes archive file; None otherwise."""
        self.window.select_session(session_id)
        # select_session emits session_selected which already loads
        # the session into the view. Now switch to the right tab.
        tab_id = {
            "transcript":    "transcript",
            "live_notes":    "live_notes",
            "notes":         "notes",
            "notes_archive": "previous",
        }.get(source)
        if tab_id is None:
            return
        archive_str = archive_name if isinstance(archive_name, str) else None
        self.window.session_view.set_active_tab(tab_id, archive_str)

    def _on_rebuild_search_index(self) -> None:
        """Help > Debug > Rebuild Search Index. Wipes search.db and
        re-indexes every session from disk. Synchronous with a
        QProgressDialog so the user can watch + cancel."""
        if self.search_index is None:
            # Try to re-open in case the prior open failed.
            try:
                self.search_index = SearchIndex()
            except Exception:
                QMessageBox.critical(
                    self.window, "Rebuild failed",
                    "Could not open the search index file. See log for details.",
                )
                return
        confirm = QMessageBox.question(
            self.window, "Rebuild search index?",
            "This drops the existing search index and re-reads every "
            "session's transcript, notes, and archived notes from "
            "disk. Safe to do anytime -- usually only needed after "
            "a corrupt write or moving the data directory.\n\n"
            "Proceed?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            from .utils.search_indexer import rebuild_all
            ids = [s.id for s in self.store.list_sessions()]
            done = rebuild_all(self.search_index, ids)
            self.window.status(
                f"Rebuilt search index ({done} session(s)).",
                timeout_ms=5000,
            )
        except Exception:
            log.exception("Rebuild search index failed")
            QMessageBox.critical(
                self.window, "Rebuild failed",
                "An error occurred while rebuilding the search index. "
                "See the log for details.",
            )

    # ---- backup + restore (#67) --------------------------------------------

    def _make_backup_manager(self):  # type: ignore[no-untyped-def]
        """Build a BackupManager pointed at the configured destination.

        Returns None when the destination is not configured -- callers
        surface the "Configure first" message to the user. Lives as a
        helper so the Tools menu, Settings dialog, and the idle / close
        triggers can all share the same construction path.
        """
        from .utils.backup import BackupManager  # noqa: PLC0415
        from .utils.paths import app_data_dir  # noqa: PLC0415
        dest = (self.config.backup.folder or "").strip()
        if not dest:
            return None
        manager = BackupManager(
            data_dir=app_data_dir(),
            destination=Path(dest),
        )
        manager.set_retention(
            count=self.config.backup.retention_count,
            days=self.config.backup.retention_days,
        )
        return manager

    def _on_backup_now_requested(self) -> None:
        """Tools > Backup Now. Synchronous with a progress dialog so the
        user can see the write happen. Errors surface as critical-level
        message boxes."""
        from .utils.backup import BackupError  # noqa: PLC0415
        manager = self._make_backup_manager()
        if manager is None:
            QMessageBox.information(
                self.window, "Backup not configured",
                "Open Settings > Backups and pick a destination folder "
                "before running a manual backup.",
            )
            return
        # Block the UI behind a non-cancelable busy dialog. The sqlite
        # backup + filesystem copy together complete in well under the
        # 1-minute mark even for multi-GB session dirs, so a simple
        # "Backing up..." dialog (rather than a progress bar) is fine.
        from PyQt6.QtWidgets import QProgressDialog  # noqa: PLC0415
        from PyQt6.QtCore import Qt  # noqa: PLC0415
        dlg = QProgressDialog(
            "Backing up data directory...", "", 0, 0, self.window,
        )
        dlg.setWindowTitle("Backup Now")
        dlg.setCancelButton(None)  # no cancel for manual backup
        dlg.setMinimumDuration(0)
        dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
        dlg.show()
        try:
            from PyQt6.QtCore import QCoreApplication  # noqa: PLC0415
            QCoreApplication.processEvents()
            result = manager.snapshot_now()
        except BackupError as exc:
            dlg.close()
            log.exception("Manual backup failed")
            QMessageBox.critical(
                self.window, "Backup failed", str(exc),
            )
            return
        except Exception as exc:  # noqa: BLE001
            dlg.close()
            log.exception("Manual backup raised unexpected error")
            QMessageBox.critical(
                self.window, "Backup failed",
                f"Unexpected error during backup: {exc}",
            )
            return
        finally:
            try:
                dlg.close()
            except Exception:  # noqa: BLE001
                pass
        self._record_successful_backup(result)
        size_mb = result.size_bytes / (1024 * 1024)
        QMessageBox.information(
            self.window, "Backup complete",
            f"Snapshot written: {result.path.name}\n"
            f"Size: {size_mb:,.1f} MB\n"
            f"Pruned {len(result.pruned)} older snapshot(s).",
        )

    def _on_restore_backup_requested(self) -> None:
        """Tools > Restore from Backup. Picks a snapshot, confirms,
        quits the app after kicking off the restore, and relies on
        the user to relaunch."""
        from .utils.backup import BackupError, BackupManager  # noqa: PLC0415
        from .utils.paths import app_data_dir  # noqa: PLC0415
        from PyQt6.QtWidgets import QFileDialog  # noqa: PLC0415
        start_dir = (self.config.backup.folder or "").strip() or str(Path.home())
        picked, _ = QFileDialog.getOpenFileName(
            self.window, "Pick a snapshot to restore",
            start_dir, "Backup snapshots (*.zip)",
        )
        if not picked:
            return
        # Verify before asking the user to confirm a destructive op.
        manager = BackupManager(
            data_dir=app_data_dir(),
            destination=Path(picked).parent,
        )
        try:
            manager.verify(Path(picked))
        except BackupError as exc:
            QMessageBox.critical(
                self.window, "Snapshot is not usable", str(exc),
            )
            return
        confirm = QMessageBox.warning(
            self.window, "Restore from backup?",
            f"Selected snapshot:\n  {Path(picked).name}\n\n"
            "Restoring REPLACES the current data directory with the "
            "contents of this snapshot. Your existing data dir will be "
            "preserved as .pre-restore.<timestamp> in case you need to "
            "roll back manually.\n\n"
            "The app will close before the swap; relaunch after the "
            "restore completes.\n\n"
            "Proceed?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        # Restore must happen with all sqlite connections closed; the
        # cleanest way is to schedule the restore for after-quit. The
        # actual swap runs in MainApp.aboutToQuit -> _run_pending_restore.
        self._pending_restore_path = Path(picked)
        self.qt_app.quit()

    def _record_successful_backup(self, result) -> None:  # type: ignore[no-untyped-def]
        """Stamp last_snapshot_at into config so the idle scheduler's
        24h dedup gate has fresh state. Save synchronously -- the
        backup just landed and is the most important piece of state."""
        import datetime as _dt  # noqa: PLC0415
        self.config.backup.last_snapshot_at = _dt.datetime.now().isoformat(
            timespec="seconds",
        )
        try:
            self.config.save()
        except Exception:  # noqa: BLE001
            log.warning(
                "Could not persist last_snapshot_at after backup",
                exc_info=True,
            )

    # Qt event filter for idle tracking. The filter is installed on
    # ``self.qt_app`` so it sees every user-input event across all
    # windows; only mouse + key events bump _last_input_at, everything
    # else falls through cheaply. Note: this fires on every key + mouse
    # event, so the body must stay fast (no logging, no allocation).
    _IDLE_INPUT_EVENTS = (
        QEvent.Type.KeyPress,
        QEvent.Type.MouseButtonPress,
        QEvent.Type.MouseMove,
        QEvent.Type.Wheel,
        QEvent.Type.TouchBegin,
        QEvent.Type.TabletMove,
    )

    def eventFilter(self, obj, event):  # noqa: N802, ARG002
        try:
            if event.type() in self._IDLE_INPUT_EVENTS:
                import time as _time  # noqa: PLC0415
                self._last_input_at = _time.monotonic()
        except Exception:  # noqa: BLE001
            pass
        # Always return False so the event continues to its real
        # target -- we're a passive observer here.
        return False

    def apply_backup_idle_scheduler_config(self) -> None:
        """Re-evaluate whether the idle timer should run after a
        Settings save. Called from ``_on_settings`` after the config
        round-trips so flipping the schedule on / off picks up
        without an app restart."""
        if (
            self.config.backup.schedule == "when_idle"
            and (self.config.backup.folder or "").strip()
        ):
            if not self._backup_idle_timer.isActive():
                import time as _time  # noqa: PLC0415
                # Reset the idle clock so a recently-changed schedule
                # doesn't trigger immediately from old activity.
                self._last_input_at = _time.monotonic()
                self._backup_idle_timer.start()
        else:
            if self._backup_idle_timer.isActive():
                self._backup_idle_timer.stop()

    def _maybe_fire_idle_backup(self) -> None:
        """1-minute timer tick: decide whether to launch a snapshot
        in the background worker. Cheap when not eligible; the
        ``should_run_idle_backup`` predicate is the gate."""
        import datetime as _dt  # noqa: PLC0415
        import time as _time  # noqa: PLC0415
        from .utils.backup import should_run_idle_backup  # noqa: PLC0415
        if self._backup_worker is not None and self._backup_worker.isRunning():
            return
        cfg = self.config.backup
        if not (cfg.folder or "").strip():
            return
        idle_seconds = _time.monotonic() - self._last_input_at
        eligible = should_run_idle_backup(
            schedule=cfg.schedule,
            last_snapshot_at=cfg.last_snapshot_at,
            idle_seconds=idle_seconds,
            idle_after_minutes=cfg.idle_after_minutes,
            idle_after_hour=cfg.idle_after_hour,
            now_local=_dt.datetime.now(),
        )
        if not eligible:
            return
        manager = self._make_backup_manager()
        if manager is None:
            return
        log.info(
            "Idle-trigger backup firing (idle %.0fs, schedule=%s)",
            idle_seconds, cfg.schedule,
        )
        self._start_backup_worker(manager)

    def _start_backup_worker(self, manager) -> None:  # type: ignore[no-untyped-def]
        """Spawn a QThread that runs ``manager.snapshot_now()`` and
        emits the result back to the GUI thread. Used by the idle
        trigger; manual + on-close paths run synchronously instead."""
        worker = _BackupWorker(manager, parent=self)
        worker.finished_ok.connect(self._on_idle_backup_success)
        worker.failed.connect(self._on_idle_backup_failure)
        worker.finished.connect(worker.deleteLater)

        def _clear() -> None:
            if self._backup_worker is worker:
                self._backup_worker = None

        worker.finished.connect(_clear)
        self._backup_worker = worker
        worker.start()

    def _on_idle_backup_success(self, result) -> None:  # type: ignore[no-untyped-def]
        self._record_successful_backup(result)
        log.info(
            "Idle backup landed: %s (%.1f MB, %d pruned)",
            result.path,
            result.size_bytes / (1024 * 1024),
            len(result.pruned),
        )

    def _on_idle_backup_failure(self, message: str) -> None:
        log.warning("Idle backup failed: %s", message)

    def _handle_window_close(self, event) -> None:  # type: ignore[no-untyped-def]
        """MainWindow close-handler hook (#67).

        Two interesting cases here:
          (1) An idle-triggered backup is running in the background.
              Show a modal busy dialog and defer the close until the
              worker finishes; then re-emit close.
          (2) Schedule == on_close. Run the synchronous snapshot now
              with a modal progress dialog so the user sees activity
              instead of a frozen window during aboutToQuit. Accept
              the close after the snapshot finishes.
        Anything else: accept the close immediately (default Qt path).

        The aboutToQuit hook ``_run_backup_on_close`` skips its work
        when this method handled the synchronous on-close snapshot
        already; we don't double-run via the LAST_BACKUP_FROM_CLOSE
        guard flag.
        """
        # Backup-in-flight wait dialog.
        if self._backup_worker is not None and self._backup_worker.isRunning():
            self._show_wait_for_backup_dialog(event)
            return
        if self.config.backup.schedule == "on_close" \
                and (self.config.backup.folder or "").strip():
            self._run_on_close_backup_with_dialog()
            # Mark that the close-path snapshot ran so aboutToQuit
            # doesn't try a second time.
            self._on_close_backup_ran = True
        event.accept()

    def _show_wait_for_backup_dialog(self, close_event) -> None:  # type: ignore[no-untyped-def]
        """Block the close behind a modal informational dialog while a
        background backup finishes. Once the worker emits finished, the
        dialog dismisses and we re-trigger ``self.qt_app.quit()``."""
        from PyQt6.QtWidgets import QProgressDialog  # noqa: PLC0415
        from PyQt6.QtCore import Qt  # noqa: PLC0415
        close_event.ignore()
        dlg = QProgressDialog(
            "Backup in progress. The app will close when it finishes.",
            "",  # no cancel button text
            0, 0, self.window,
        )
        dlg.setWindowTitle("Backup in progress")
        dlg.setCancelButton(None)
        dlg.setMinimumDuration(0)
        dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
        dlg.show()
        worker = self._backup_worker

        def _on_done() -> None:
            try:
                dlg.close()
            except Exception:  # noqa: BLE001
                pass
            # Trigger Qt's quit path. Since the worker has finished,
            # _handle_window_close will fall through to event.accept()
            # when Qt re-issues the close on quit.
            self.qt_app.quit()

        if worker is not None:
            worker.finished.connect(_on_done)
        else:
            _on_done()

    def _run_on_close_backup_with_dialog(self) -> None:
        """Synchronous snapshot during the close path, with a modal
        progress dialog so the user sees activity rather than a frozen
        window. Failures log + show a critical message box but still
        allow the close to proceed (we don't want to trap the user)."""
        from PyQt6.QtWidgets import QProgressDialog  # noqa: PLC0415
        from PyQt6.QtCore import Qt, QCoreApplication  # noqa: PLC0415
        from .utils.backup import BackupError  # noqa: PLC0415
        manager = self._make_backup_manager()
        if manager is None:
            return
        dlg = QProgressDialog(
            "Backing up data directory before close...",
            "", 0, 0, self.window,
        )
        dlg.setWindowTitle("Backup")
        dlg.setCancelButton(None)
        dlg.setMinimumDuration(0)
        dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
        dlg.show()
        QCoreApplication.processEvents()
        try:
            result = manager.snapshot_now()
            self._record_successful_backup(result)
            log.info(
                "Close-path backup landed: %s (%d pruned)",
                result.path, len(result.pruned),
            )
        except BackupError as exc:
            log.exception("Close-path backup failed")
            QMessageBox.critical(
                self.window, "Backup failed",
                f"The on-close backup did not complete: {exc}\n\n"
                "The app will still exit. Your data dir is unchanged.",
            )
        except Exception:  # noqa: BLE001
            log.exception("Close-path backup raised unexpected error")
        finally:
            try:
                dlg.close()
            except Exception:  # noqa: BLE001
                pass

    def _run_backup_on_close(self) -> None:
        """aboutToQuit fallback for the ``on_close`` schedule.

        ``_handle_window_close`` already does the synchronous snapshot
        with a progress dialog when the user clicks the X / Quit menu.
        This hook is for the (rare) shutdown path where Qt quits
        without first dispatching a closeEvent to MainWindow -- e.g.
        a tray quit or programmatic ``self.qt_app.quit()`` mid-task.
        It runs in silent mode (no dialog, just logs)."""
        from .utils.backup import BackupError  # noqa: PLC0415
        if getattr(self, "_on_close_backup_ran", False):
            return
        if self._pending_restore_path is not None:
            return
        if self.config.backup.schedule != "on_close":
            return
        manager = self._make_backup_manager()
        if manager is None:
            return
        try:
            result = manager.snapshot_now()
            self._record_successful_backup(result)
            log.info(
                "On-close backup (silent path) landed: %s (%d pruned)",
                result.path, len(result.pruned),
            )
        except BackupError:
            log.exception("On-close backup failed")
        except Exception:  # noqa: BLE001
            log.exception("On-close backup raised unexpected error")

    def _run_pending_restore(self) -> None:
        """aboutToQuit hook -- if the user asked to restore from a
        snapshot, perform the data-dir swap now (all sqlite handles
        from this process are closed by Qt's shutdown sequence).

        The user must relaunch manually after the restore; auto-relaunch
        would require respawning the process from the dying one, which
        is fragile on Windows."""
        if self._pending_restore_path is None:
            return
        zip_path = self._pending_restore_path
        self._pending_restore_path = None
        from .utils.backup import BackupError, BackupManager  # noqa: PLC0415
        from .utils.paths import app_data_dir  # noqa: PLC0415
        # Close any sqlite connections we still own so the rename in
        # restore_from doesn't fail with EBUSY on Windows.
        for store_attr in ("store", "classification", "search_index"):
            store = getattr(self, store_attr, None)
            close = getattr(store, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    log.exception("Closing %s before restore failed", store_attr)
        manager = BackupManager(
            data_dir=app_data_dir(),
            destination=zip_path.parent,
        )
        try:
            manager.restore_from(zip_path)
            log.info("Restore from %s complete", zip_path)
        except BackupError:
            log.exception("Restore from %s failed", zip_path)
        except Exception:  # noqa: BLE001
            log.exception("Restore raised unexpected error")

    # ---- classification -----------------------------------------------------

    def _migrate_speakers_to_contacts(self) -> None:
        """Link every existing SpeakerStore row to a Contact.

        Runs once at launch (cheap when there's nothing to link).
        For each speaker without a contact_id:
        * Unique Contact alias hit on the speaker's name -> link.
        * No match -> create a new Contact named after the speaker,
          with the speaker's name as a `name`-kind alias sourced
          from `diarization`.
        * Multiple matches -> create a new Contact rather than
          guess; the Address Book's suggested-merges surface lets
          the user pick later.

        Idempotent: a second run is a no-op since speakers gain
        contact_id on first link.
        """
        if self.classification is None:
            return
        from .models.classification import (  # noqa: PLC0415
            ALIAS_KIND_NAME, SOURCE_DIARIZATION,
        )
        speaker_store = open_speaker_store()
        try:
            unlinked = speaker_store.list_unlinked()
            for speaker in unlinked:
                name = (speaker.name or "").strip()
                if not name:
                    continue
                matches = self.classification.find_contacts_by_alias(name)
                if len(matches) == 1:
                    contact = matches[0]
                elif not matches:
                    contact = self.classification.create_contact(
                        name, initial_alias_source=SOURCE_DIARIZATION,
                    )
                else:
                    # Ambiguous -- create a new Contact; suggested
                    # merges in Address Book will surface the
                    # collision.
                    contact = self.classification.create_contact(
                        name, initial_alias_source=SOURCE_DIARIZATION,
                    )
                speaker_store.set_contact_id(name, contact.id)
            if unlinked:
                log.info(
                    "speakers -> contacts migration linked %d speaker(s)",
                    len(unlinked),
                )
        finally:
            speaker_store.close()

    def _auto_link_series_for_new_session(self, session_id: str, title: str) -> None:
        """Best-effort: fuzzy-match the title against known series + link.

        Called from _on_new_session and the calendar-creation path
        after the session row exists. Silent on no-match -- the
        chips bar will show "Series: (none)" and the user can pick
        one manually.
        """
        if self.classification is None or not title.strip():
            return
        try:
            series = self.classification.find_series_for_title(title)
            if series is not None:
                self.classification.assign_series(session_id, series.id)
        except Exception:
            log.exception("series auto-link failed for %s", session_id)

    def _sync_attendees_to_people(self, session_id: str, body: str) -> None:
        """Mirror the live notes' # Attendees list into the session's
        Contact links.

        Resolution is smart (issue #28): each typed name routes through
        resolve_attendees_batch which:
        * Unique alias match -> link to existing Contact, register
          the typed form as a `name` alias if new.
        * No match -> create new Contact.
        * Ambiguous -> create new Contact AND surface the conflict
          via the Address Book's suggested-merges section so the
          user can resolve deliberately.

        The hot path stays modal-free; ambiguity prompts wait for an
        explicit Address Book visit.
        """
        if self.classification is None:
            return
        try:
            names = parse_attendees(body or "")
            contacts = resolve_attendees_batch(
                self.classification, names, source=SOURCE_ATTENDEE_LIST,
            )
            self.classification.replace_session_attendee_contacts(
                session_id, [c.id for c in contacts],
            )
        except Exception:
            log.exception("attendee sync failed for %s", session_id)
            return
        # Push the resolved contacts into the attendee-details drawer
        # so the table refreshes inline as the user edits My Notes.
        # Issue #51 Phase 3.
        if (
            self.window.session_view._session is not None  # noqa: SLF001
            and self.window.session_view._session.id == session_id  # noqa: SLF001
        ):
            self._refresh_session_contacts_in_drawer(session_id)

    def _apply_attendee_details_appendix(self, session_id: str, markdown: str) -> None:
        """Parse + apply the LLM "Attendee Details" appendix.

        Issue #51 Phase 4. Resolves each appendix entry's name to a
        Contact via the same resolver attendee-list typing uses,
        then fills missing rich fields via update_contact_fields
        with fill_empty_only=True + source=llm.

        Best-effort; any exception logs + returns -- the synthesis
        save proceeds regardless.
        """
        if self.classification is None:
            return
        if not self.config.synthesis.auto_extract_attendee_details:
            return
        try:
            from .models.classification import ENRICH_SOURCE_LLM  # noqa: PLC0415
            from .utils.attendee_appendix import parse_appendix  # noqa: PLC0415
            from .utils.contact_resolution import (  # noqa: PLC0415
                resolve_attendee_email, resolve_attendee_text,
            )
            entries = parse_appendix(markdown)
            if not entries:
                return
            applied_count = 0
            for entry in entries:
                # Prefer email-based resolution when the LLM gave us
                # an email -- it's the most unambiguous identifier.
                result = None
                if entry.email:
                    result = resolve_attendee_email(self.classification, entry.email)
                if result is None and entry.name:
                    result = resolve_attendee_text(self.classification, entry.name)
                if result is None:
                    continue
                fields: dict[str, str] = {}
                if entry.title:
                    fields["title"] = entry.title
                if entry.company:
                    fields["company"] = entry.company
                if entry.department:
                    fields["department"] = entry.department
                if entry.email:
                    fields["primary_email"] = entry.email
                if entry.phone:
                    fields["phone"] = entry.phone
                if not fields:
                    continue
                self.classification.update_contact_fields(
                    result.contact.id,
                    source=ENRICH_SOURCE_LLM,
                    fill_empty_only=True,
                    **fields,
                )
                applied_count += 1
            if applied_count:
                log.info(
                    "attendee appendix applied: %d entries for session %s",
                    applied_count, session_id,
                )
        except Exception:
            log.exception(
                "attendee appendix application failed for %s", session_id,
            )

    def _refresh_session_contacts_in_drawer(self, session_id: str) -> None:
        """Pull the session's linked Contacts + push them into the
        SessionView's drawer widgets (issue #51 Phase 3).

        Used both on session-select (initial paint) and after
        attendee sync (live updates while the user types in My Notes).
        Cheap query; called on every keystroke in the worst case
        but bounded by the My Notes debounce so it doesn't fire
        more than a few times a second.
        """
        if self.classification is None:
            return
        try:
            session_contacts = self.classification.contacts_for_session(
                session_id,
            )
            contacts = [sc.contact for sc in session_contacts]
        except Exception:
            log.exception("drawer refresh failed for %s", session_id)
            return
        self.window.session_view.set_session_contacts(contacts)

    def _on_drawer_contact_clicked(self, contact_id: int) -> None:
        """Open the Address Book filtered to a specific contact
        (issue #51 Phase 3). Bridges the drawer's row-click signal
        through to the existing _on_address_book handler with a
        pre-selected contact id."""
        if self.classification is None:
            return
        speaker_store = open_speaker_store()
        try:
            dialog = AddressBookDialog(
                self.classification, parent=self.window,
                speaker_store=speaker_store,
            )
            dialog.select_contact(contact_id)
            dialog.exec()
        finally:
            speaker_store.close()
        # After the dialog closes the drawer may need to reflect
        # any field edits the user made.
        sv = self.window.session_view
        if sv._session is not None:  # noqa: SLF001
            self._refresh_session_contacts_in_drawer(sv._session.id)  # noqa: SLF001

    def _extract_topics_for_session(self, session_id: str, body: str) -> None:
        """Run the deterministic extractor over synthesis text + push
        suggestions into the classification store.

        Already-accepted topics survive the replace; only previously-
        unaccepted auto-suggestions get refreshed.

        Stopword strategy: tokenize every known person's display
        name AND this session's parsed attendees, adding every
        token (>= 2 chars) to the stopword set. Names usually
        appear in synthesis text by first name alone -- "Alice
        said..." rather than "Alice Smith said..." -- so just
        passing display_names misses the noise. Catches first,
        middle, last names alike. Case-insensitive comparison is
        done inside extract_topics so we don't need to pre-fold
        case here.
        """
        if self.classification is None:
            return
        try:
            stopwords: set[str] = set()
            # Every known person (across all sessions, not just
            # this one) gets their full name + every name token
            # added. A "Bob" surfacing in this synthesis because
            # he was mentioned in a different meeting still gets
            # suppressed.
            for person in self.classification.list_contacts():
                name = (person.display_name or "").strip()
                if not name:
                    continue
                stopwords.add(name)
                for tok in name.split():
                    if len(tok) >= 2:
                        stopwords.add(tok)
            # Also pull from this session's live-notes attendees in
            # case some haven't made it into the people store yet
            # (e.g. typed but the live_notes save hasn't fired the
            # attendee sync this cycle).
            try:
                live = TranscriptStore(session_id).read_live_notes()
                for name in parse_attendees(live or ""):
                    name = (name or "").strip()
                    if not name:
                        continue
                    stopwords.add(name)
                    for tok in name.split():
                        if len(tok) >= 2:
                            stopwords.add(tok)
            except Exception:
                log.exception(
                    "attendee read for stopword build failed for %s", session_id,
                )
            suggestions = extract_topics(
                body or "", extra_stopwords=stopwords,
            )
            # LLM-suggested topics from the Suggested Topics appendix
            # (#57) get folded in BEFORE the deterministic ones so
            # they rank earlier in the suggestion list. Dedupe is
            # case-insensitive so the deterministic extractor doesn't
            # re-surface a topic the LLM already named.
            try:
                from .utils.topic_appendix import (  # noqa: PLC0415
                    parse_topic_appendix,
                )
                llm_topics = parse_topic_appendix(body or "")
            except Exception:
                log.exception(
                    "LLM topic appendix parse failed for %s", session_id,
                )
                llm_topics = []
            if llm_topics:
                seen = {t.lower() for t in llm_topics}
                deduped_extractor = [
                    t for t in suggestions if t.lower() not in seen
                ]
                suggestions = llm_topics + deduped_extractor
            self.classification.replace_session_topic_suggestions(
                session_id, suggestions,
            )
        except Exception:
            log.exception("topic extraction failed for %s", session_id)

    def _on_manage_series(self) -> None:
        """Legacy menu entry (kept for back-compat). Opens the
        full Manage Classification dialog (Series + Topics tabs)
        landing on the Series tab."""
        self._on_manage_classification(initial_tab="series")

    def _on_manage_classification(
        self, _checked: bool = False, *, initial_tab: str = "series",
    ) -> None:
        """File > Manage Classification. Tabbed dialog (Series +
        Topics) over the ClassificationStore. People moved to the
        Address Book in Phase 2 -- see _on_address_book."""
        if self.classification is None:
            QMessageBox.warning(
                self.window, "Manage Classification",
                "Classification store unavailable. See log for details.",
            )
            return
        dialog = ManageClassificationDialog(
            self.classification, parent=self.window,
        )
        if initial_tab == "topics":
            dialog._tabs.setCurrentIndex(1)  # noqa: SLF001
        dialog.exec()
        self._refresh_session_list()
        sv = self.window.session_view
        if sv._session is not None:
            self._refresh_session_classification(sv._session.id)

    def _on_address_book(self) -> None:
        """File > Address Book. Manages Contacts (the master
        identity behind People + Speakers). On close, refresh
        navigator + chips bar + the speakers manage dialog (if
        open) won't auto-refresh -- it's typically closed before
        reaching here."""
        if self.classification is None:
            QMessageBox.warning(
                self.window, "Address Book",
                "Classification store unavailable. See log for details.",
            )
            return
        speaker_store = open_speaker_store()
        try:
            dialog = AddressBookDialog(
                self.classification, parent=self.window,
                speaker_store=speaker_store,
            )
            dialog.exec()
        finally:
            speaker_store.close()
        self._refresh_session_list()
        sv = self.window.session_view
        if sv._session is not None:
            self._refresh_session_classification(sv._session.id)

    def _on_classification_filter_changed(self, view: str, value_id) -> None:
        self._classification_filter_view = view
        # value_id is int | None; the navigator emits None when "All"
        # is active or when a By_X view has no value picked.
        self._classification_filter_value = (
            int(value_id) if isinstance(value_id, int) else None
        )
        self._refresh_session_list()

    def _on_add_topic_requested(self, session_id: str, name: str) -> None:
        if self.classification is None or not name:
            return
        try:
            topic = self.classification.get_or_create_topic(name)
            self.classification.add_session_topic(
                session_id, topic.id, source=SOURCE_MANUAL, accepted=True,
            )
        except Exception:
            log.exception("add_topic failed for %s/%s", session_id, name)
        self._refresh_session_classification(session_id)
        self._refresh_classification_choices()

    def _on_remove_topic_requested(self, session_id: str, topic_id: int) -> None:
        if self.classification is None:
            return
        try:
            self.classification.remove_session_topic(session_id, topic_id)
        except Exception:
            log.exception("remove_topic failed for %s/%s", session_id, topic_id)
        self._refresh_session_classification(session_id)

    def _on_accept_topic_requested(self, session_id: str, topic_id: int) -> None:
        if self.classification is None:
            return
        try:
            self.classification.set_topic_accepted(session_id, topic_id, True)
        except Exception:
            log.exception("accept_topic failed for %s/%s", session_id, topic_id)
        self._refresh_session_classification(session_id)

    def _on_set_series_requested(self, session_id: str, series_name: str) -> None:
        if self.classification is None:
            return
        try:
            if not series_name.strip():
                # Empty -> unfile.
                self.classification.assign_series(session_id, None)
            else:
                series = self.classification.get_or_create_series(series_name)
                self.classification.assign_series(session_id, series.id)
        except Exception:
            log.exception("set_series failed for %s/%s", session_id, series_name)
        self._refresh_session_classification(session_id)
        self._refresh_classification_choices()

    # ---- highlights --------------------------------------------------------

    def _on_session_highlights_changed(self, session_id: str, hs: HighlightSet) -> None:
        """Persist the user's marker edits to highlights.json.

        Synchronous (no debounce) -- highlight adds/removes are
        infrequent (one click each) and the JSON file is tiny.
        """
        try:
            HighlightsStore(session_id).save(hs)
        except Exception:
            log.exception("highlights save failed for %s", session_id)

    def _session_highlights(self, session_id: str) -> HighlightSet:
        try:
            return HighlightsStore(session_id).load()
        except Exception:
            log.exception("highlights load failed for %s", session_id)
            return HighlightSet()

    def _prompt_highlights_or_full(self, session_id: str) -> Optional[str]:
        """Ask the user "full session or highlights-only" when the
        session has highlights. Returns:
            "full"       -- export the full recording
            "highlights" -- export the concatenated highlights
            None         -- user cancelled

        Sessions with no highlights short-circuit to "full" without
        prompting.
        """
        try:
            hs = self._session_highlights(session_id)
        except Exception:
            hs = HighlightSet()
        if not hs.highlights:
            return "full"
        total_s = hs.total_duration_ms() // 1000
        dialog = QMessageBox(self.window)
        dialog.setWindowTitle("Export scope")
        dialog.setText(
            f"This session has {len(hs.highlights)} highlight(s) "
            f"({total_s}s total). Export everything, or just the highlights?"
        )
        full_btn = dialog.addButton(
            "Full session", QMessageBox.ButtonRole.AcceptRole,
        )
        highlights_btn = dialog.addButton(
            "Highlights only", QMessageBox.ButtonRole.AcceptRole,
        )
        cancel_btn = dialog.addButton(QMessageBox.StandardButton.Cancel)
        dialog.setDefaultButton(full_btn)
        dialog.exec()
        clicked = dialog.clickedButton()
        if clicked is full_btn:
            return "full"
        if clicked is highlights_btn:
            return "highlights"
        return None

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

    def _on_about(self) -> None:
        from .ui.about_dialog import AboutDialog  # noqa: PLC0415
        AboutDialog(parent=self.window).exec()

    def _on_user_guide(self) -> None:
        """Open the GitHub README at the User Guide anchor.

        Keeping the how-to in the README (where mermaid renders) and
        deep-linking from the app avoids maintaining two copies. The
        repo's main branch is the source of truth; if the user is on
        an older build the link still points to the latest docs --
        appropriate for a how-to-use guide.
        """
        from PyQt6.QtCore import QUrl  # noqa: PLC0415
        from PyQt6.QtGui import QDesktopServices  # noqa: PLC0415
        QDesktopServices.openUrl(QUrl(
            "https://github.com/aarondodd/meeting-notetaker#user-guide"
        ))

    def _on_settings(self) -> None:
        dialog = SettingsDialog(
            self.config,
            parent=self.window,
            ping_extension=self._ping_extension,
            classification_store=self.classification,
        )
        # Backups group exposes a Backup Now button; route it through
        # MainApp's manual-backup handler so the same modal dialog +
        # validation as the Tools menu path is used.
        dialog.inject_backup_now_handler(self._on_backup_now_requested)
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
        self.apply_backup_idle_scheduler_config()
        self._refresh_status_indicators()
        # Push the new auto-capture interval to the sidebar so the
        # "every Ns" hint reflects what Settings just saved.
        self.window.session_view.set_screencap_auto_interval(
            int(self.config.ui.screen_capture_auto_interval_sec)
        )
        # Push the appendix-inclusion defaults so the per-tab
        # Export PDF + Print dialogs reflect the new settings.
        self.window.session_view.set_appendix_export_defaults(
            self._appendix_export_defaults_from_config(),
        )
        self.window.status("Settings saved.", timeout_ms=4000)

    def _appendix_export_defaults_from_config(self):
        """Build the AppendixInclusion the per-export dialog should
        pre-check, derived from the Settings-saved booleans."""
        from .ui.appendix_inclusion_dialog import AppendixInclusion  # noqa: PLC0415
        s = self.config.synthesis
        return AppendixInclusion(
            include_appendix=s.appendix_export_include,
            include_attendee_context=s.appendix_export_attendee_context,
            include_attendee_details=s.appendix_export_attendee_details,
            include_topics=s.appendix_export_topics,
            include_referenced_attachments=s.appendix_export_referenced_attachments,
            include_session_attachments=s.appendix_export_session_attachments,
            include_links=s.appendix_export_links,
        )

    # ---- status bar indicators -------------------------------------------

    def _refresh_status_indicators(self) -> None:
        """Repopulate the right-side status bar pills from current state.

        Pulled out so settings-saved, calendar-config-applied, and startup
        all share one source of truth. Each segment computes its own
        SegmentState (or None to hide); MainWindow paints the dots.
        """
        indicators: dict[str, SegmentState] = {}
        cal = self._calendar_segment()
        if cal is not None:
            indicators["cal"] = cal
        voice = self._voice_segment()
        if voice is not None:
            indicators["voice"] = voice
        det = self._detection_segment()
        if det is not None:
            indicators["det"] = det
        syn = self._synthesis_segment()
        if syn is not None:
            indicators["syn"] = syn
        self.window.set_status_indicators(
            version=__version__,
            indicators=indicators,
        )

    def _calendar_segment(self) -> Optional[SegmentState]:
        """SegmentState for the calendar pill, or None when hidden.

        Hidden when the user has the feature off (no point telling them
        about something they aren't using). Otherwise green=watching,
        yellow=idle, red=Outlook unavailable.
        """
        if not self.config.calendar.watch_calendar:
            return None
        if outlook_calendar.is_available():
            running = (
                self._calendar_monitor is not None
                and self._calendar_monitor.is_running()
            )
            if running:
                return SegmentState(
                    color="green",
                    short_label="Cal",
                    tooltip=(
                        f"Watching Outlook calendar; notifying within "
                        f"+- {self.config.calendar.window_minutes} min of "
                        "each meeting start."
                    ),
                )
            return SegmentState(
                color="yellow",
                short_label="Cal",
                tooltip=(
                    "Calendar watching is enabled but the monitor is not "
                    "running. Try toggling it off and on in Settings."
                ),
            )
        return SegmentState(
            color="red",
            short_label="Cal",
            tooltip=(
                "Calendar watching is enabled, but Outlook (or pywin32) is "
                "not reachable. Help > Diagnose Outlook... reports which "
                "step in the chain is failing."
            ),
        )

    def _synthesis_segment(self) -> Optional[SegmentState]:
        """SegmentState for the synthesis-automation pill, or None.

        Hidden when automation is disabled in Settings. Otherwise the
        dot color tracks the bridge state: green when the extension is
        connected, yellow when Chrome is cold (Send will launch it),
        red when Chrome is up but the extension isn't talking back.
        """
        if not self.config.synthesis.automation_enabled:
            return None
        state = self._synth_state
        return SegmentState(
            color=state.dot_color(),
            short_label="Syn",
            tooltip=state.status_tooltip(),
        )

    def _detection_segment(self) -> Optional[SegmentState]:
        """SegmentState for the ad-hoc-meeting-detect pill, or None.

        Hidden when detection is off; mirrors the Calendar pill's color
        encoding for the enabled states.
        """
        if not self.config.detection.enabled:
            return None
        if not audio_session_monitor.is_available():
            return SegmentState(
                color="red",
                short_label="Det",
                tooltip=(
                    "Ad-hoc meeting detection is enabled, but pycaw / "
                    "psutil are not importable. Install them in this "
                    "environment to use this feature (Windows wheels)."
                ),
            )
        running = self._audio_monitor is not None and self._audio_monitor.is_running()
        if running:
            allowlist_size = len(self.config.detection.app_allowlist)
            return SegmentState(
                color="green",
                short_label="Det",
                tooltip=(
                    f"Watching system audio for {allowlist_size} known "
                    f"meeting app(s); prompting after audio sustains "
                    f"{self.config.detection.min_duration_sec}s."
                ),
            )
        return SegmentState(
            color="yellow",
            short_label="Det",
            tooltip=(
                "Ad-hoc meeting detection is enabled but the monitor is "
                "not running. Try toggling it off and on in Settings."
            ),
        )

    def _voice_segment(self) -> Optional[SegmentState]:
        """SegmentState for the voiceprint-enrollment pill, or None.

        Only surfaces when speaker ID is enabled but no usable voiceprint
        is on disk. Voiceprints recorded under an older encoder (e.g.
        Resemblyzer before the v0.5 ECAPA swap) count as not enrolled
        because their dim is incompatible with the current encoder.
        """
        if not self.config.speakers.enabled:
            return None
        try:
            if user_voiceprint.load() is not None:
                return None
        except Exception:
            log.exception("voice indicator: voiceprint check failed")
            return None
        return SegmentState(
            color="yellow",
            short_label="Voiceprint",
            tooltip=(
                "No voice sample has been recorded. Settings > Speaker "
                "Identification > Record voice sample lets the refiner "
                "tell your microphone from system-audio bleed."
            ),
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
        # Unparented (parent=None) so a quit-during-download (which can
        # take 30+s for the SpeechBrain ECAPA-TDNN encoder) doesn't trip
        # "QThread: Destroyed while running" via parent destruction.
        # aboutToQuit -> _retire_encoder_prewarm waits for the thread
        # to finish before the QApplication tears down.
        self._encoder_prewarm = EncoderPrewarmThread(parent=None)
        self._encoder_prewarm.download_started.connect(
            self._on_encoder_download_started
        )
        self._encoder_prewarm.finished_ok.connect(self._on_encoder_ready)
        self._encoder_prewarm.failed.connect(self._on_encoder_failed)
        self._encoder_prewarm.start()

    def _retire_encoder_prewarm(self) -> None:
        """Join the encoder prewarm thread before app teardown.

        Connected to qt_app.aboutToQuit. If the SpeechBrain encoder is
        mid-download (which can take 30+ s on first install with a slow
        network), letting the parent QApplication tear down while the
        thread is still running aborts the process. Wait up to 2 s for
        a graceful join; longer than that we'd rather log + leak the
        thread than freeze the close.
        """
        worker = self._encoder_prewarm
        if worker is None or not worker.isRunning():
            return
        log.info("waiting up to 2s for encoder prewarm to finish before quit")
        if not worker.wait(2000):
            log.warning(
                "encoder prewarm did not finish within 2s; abandoning "
                "(process may log a Qt warning at exit)"
            )

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
        """Silent weekly check on startup (issue #34).

        Dispatches to a worker QThread so the urllib request can't
        block the main thread for up to 30 s if GitHub is slow or
        unreachable. Only nags the user via QMessageBox when there's
        a newer release; network errors and private-repo 404s
        degrade to silent no-op.
        """
        if self._update_check_worker is not None and self._update_check_worker.isRunning():
            return
        worker = _UpdateCheckWorker(mode="auto")
        worker.result_ready.connect(self._on_auto_update_available)
        # `up_to_date` and `failed` in auto mode -> silent no-op.
        worker.finished.connect(lambda w=worker: self._retire_update_check_worker(w))
        self._update_check_worker = worker
        worker.start()

    def _on_auto_update_available(self, local: str, remote: str) -> None:
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
            + _upgrade_path_blurb(),
        )

    def _on_check_for_updates(self) -> None:
        """Manual Help > Check for Updates... path -- ignores the 7-day cooldown."""
        if self._update_check_worker is not None and self._update_check_worker.isRunning():
            self.window.status("Already checking for updates...", timeout_ms=2000)
            return
        self.window.status("Checking for updates...", timeout_ms=0)
        worker = _UpdateCheckWorker(mode="manual")
        worker.result_ready.connect(self._on_manual_update_available)
        worker.up_to_date.connect(self._on_manual_up_to_date)
        worker.failed.connect(self._on_manual_check_failed)
        worker.finished.connect(lambda w=worker: self._retire_update_check_worker(w))
        self._update_check_worker = worker
        worker.start()

    def _on_manual_update_available(self, local: str, remote: str) -> None:
        self.window.status("Ready", timeout_ms=2000)
        choice = QMessageBox.question(
            self.window,
            "Update Available",
            f"A new version is available.\n\n"
            f"Current version: {local}\n"
            f"Latest version: {remote}\n\n"
            + _upgrade_path_blurb()
            + "\n\nUpgrade now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if choice == QMessageBox.StandardButton.Yes:
            self._on_upgrade()

    def _on_manual_up_to_date(self) -> None:
        self.window.status("Ready", timeout_ms=2000)
        QMessageBox.information(
            self.window,
            "Check for Updates",
            f"You are running the latest version ({__version__}).",
        )

    def _on_manual_check_failed(self, message: str) -> None:
        self.window.status("Ready", timeout_ms=2000)
        QMessageBox.information(self.window, "Check for Updates", message)

    def _retire_update_check_worker(self, worker) -> None:
        """Join + deleteLater the update-check QThread after `finished`.

        Mirrors the _safe_cleanup_thread pattern. wait() blocks at most
        microseconds (finished already fired); the deleteLater drops
        the QObject after Qt's next event loop tick.
        """
        try:
            worker.wait()
        except Exception:
            log.exception("update-check worker wait failed")
        try:
            worker.deleteLater()
        except Exception:
            log.exception("update-check worker deleteLater failed")
        if self._update_check_worker is worker:
            self._update_check_worker = None

    def _on_upgrade(self) -> None:
        """Help > Upgrade... -- confirm + run UpgradeProgressDialog.

        On a frozen install, this downloads the latest release's
        installer asset and launches it silently; Inno Setup's Restart
        Manager hooks close this app, install in place, and relaunch.

        On a source / portable build, the upgrade() call returns a
        guidance message immediately (no download) explaining that the
        user needs to upgrade via their own workflow.
        """
        # Imported here so the static graph stays light when the dialog is
        # never opened.
        from .ui.upgrade_dialog import UpgradeProgressDialog

        if not updater_mod.is_frozen():
            QMessageBox.information(
                self.window,
                "Upgrade",
                "This Meeting Notetaker is running from source, not from "
                "the Inno Setup installer.\n\n"
                "The built-in updater only upgrades installer-managed "
                "installs. To update a source / portable build, use your "
                "own workflow (for example: git pull + .\\build.ps1, or "
                "re-download a portable .exe from the Releases page).",
            )
            return

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
            "This downloads the new release's installer and runs it "
            "silently in the background. Meeting Notetaker will close "
            "shortly after the download finishes; Windows Restart "
            "Manager handles the upgrade and relaunches the app when "
            "the install completes.\n\n"
            "If you originally installed system-wide (Program Files), "
            "Windows will show a single UAC dialog. Per-user installs "
            "upgrade fully silently.",
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
        ok, _message = dialog.result_summary()
        if not ok:
            return
        # Installer is now running detached. Quit cleanly so Restart
        # Manager doesn't have to force-close us; Inno Setup's
        # RestartApplications=yes will relaunch the app once the
        # install finishes.
        self.window.status(
            "Installer launched. Closing for in-place upgrade...",
            timeout_ms=4000,
        )
        QTimer.singleShot(1500, self.qt_app.quit)

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


class _BackupWorker(QThread):
    """Off-thread snapshot worker for the idle-trigger backup (#67).

    The manager's snapshot_now path is blocking (sqlite backup +
    filesystem copy + zip), so running it on the GUI thread would
    freeze the app for the duration. ``parent`` is the MainApp QObject
    so the worker shares its lifetime + cleans up on app quit.

    Emits exactly one of:
      - ``finished_ok(BackupResult)`` on success
      - ``failed(message)`` on BackupError or any unexpected error

    The base QThread ``finished`` signal still fires after either
    branch so callers can connect lifecycle cleanup once.
    """

    finished_ok = pyqtSignal(object)  # BackupResult
    failed = pyqtSignal(str)

    def __init__(self, manager, parent: Optional[QObject] = None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(parent)
        self._manager = manager

    def run(self) -> None:
        from .utils.backup import BackupError  # noqa: PLC0415
        try:
            result = self._manager.snapshot_now()
        except BackupError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("Idle backup worker raised unexpected error")
            self.failed.emit(f"Unexpected error: {exc}")
            return
        self.finished_ok.emit(result)


class _UpdateCheckWorker(QThread):
    """Run the GitHub release-check off the main thread (issue #34).

    Emits exactly one of:
      - `result_ready(local, remote)` when a newer release exists
      - `up_to_date()` when the current version is the latest
      - `failed(message)` on network / parse failure

    `mode='auto'` honors the weekly cooldown (silent startup check);
    `mode='manual'` ignores it (Help > Check for Updates ...). Either
    way the worker is one-shot -- `finished` triggers
    _safe_cleanup_qthread via the call-site connection.
    """

    result_ready = pyqtSignal(str, str)  # (local, remote)
    up_to_date = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, mode: str = "auto") -> None:
        super().__init__(None)
        self._mode = mode

    def run(self) -> None:
        try:
            if self._mode == "auto":
                result = updater_mod.check_for_updates()
                if result is None:
                    self.up_to_date.emit()
                    return
                local, remote = result
                self.result_ready.emit(local, remote)
                return
            release = updater_mod.get_latest_release()
            if release is None:
                self.failed.emit(
                    "Could not check for updates.\n\n"
                    "Possible reasons:\n"
                    "  - No network connectivity\n"
                    "  - The release feed is restricted (private repo)\n"
                    "  - GitHub is unreachable from this network"
                )
                return
            remote = release["tag_name"]
            if updater_mod.is_newer_version(remote, __version__):
                self.result_ready.emit(__version__, remote)
            else:
                self.up_to_date.emit()
        except Exception as exc:
            log.exception("update check worker failed")
            self.failed.emit(f"Update check failed: {exc}")


class _SessionContentLoader(QThread):
    """Off-thread disk-read pass for session selection (issue #39).

    Pulls every disk-backed content blob the right pane needs --
    transcript, live notes, synthesis notes, previous-notes archive
    list, prompt template metadata, highlight markers, attendee
    parse, attachments info -- on a background thread, then hands
    the whole bundle to the main thread for application via
    individual UI setters.

    The main thread isn't completely freed: setPlainText for a
    500 KB transcript still has to compute layout on the GUI thread
    (QTextDocument is not thread-safe). But the disk wait, JSON
    parse, and glob scan move off, which is the dominant cost on
    Windows with antivirus + large meetings.

    Cancellation: every load() bumps the player's load generation;
    the slot receiving content_loaded checks the captured generation
    against the current and drops stale results.
    """

    content_loaded = pyqtSignal(int, object)  # (generation, _LoadedSessionContent)

    def __init__(self, *, session_id: str, generation: int) -> None:
        super().__init__(None)
        self._session_id = session_id
        self._generation = generation

    def run(self) -> None:
        from .models.transcript import TranscriptStore
        from .utils import prompts as prompts_mod
        try:
            store = TranscriptStore(self._session_id)
            transcript = store.read_transcript()
            live_notes = store.read_live_notes()
            notes = store.read_notes()
            previous_notes_paths = store.list_previous_notes()
            template_names = [t.name for t in prompts_mod.list_templates()]
            selected_template = store.read_prompt_template_name()
        except Exception as exc:
            log.exception("session content load failed for %s", self._session_id)
            self.content_loaded.emit(
                self._generation,
                _LoadedSessionContent(
                    session_id=self._session_id,
                    transcript="",
                    live_notes="",
                    notes="",
                    previous_notes_paths=[],
                    template_names=[],
                    selected_template=None,
                    error=str(exc),
                ),
            )
            return
        # Highlights live in a separate JSON file; the load is cheap
        # but folding it into the same off-thread pass means the slot
        # has all the data it needs in one queued event.
        try:
            highlights = HighlightsStore(self._session_id).load()
        except Exception:
            log.exception("highlights load failed for %s", self._session_id)
            highlights = HighlightSet()
        self.content_loaded.emit(
            self._generation,
            _LoadedSessionContent(
                session_id=self._session_id,
                transcript=transcript,
                live_notes=live_notes,
                notes=notes,
                previous_notes_paths=previous_notes_paths,
                template_names=template_names,
                selected_template=selected_template,
                highlights=highlights,
                error=None,
            ),
        )


@dataclass
class _LoadedSessionContent:
    """Container for everything _SessionContentLoader collects off-thread."""
    session_id: str
    transcript: str
    live_notes: str
    notes: str
    previous_notes_paths: list
    template_names: list
    selected_template: Optional[str]
    highlights: Optional["HighlightSet"] = None
    error: Optional[str] = None


class _SearchScanWorker(QThread):
    """Walk every session's content fingerprint, reindex if stale.

    Constructs its own SearchIndex inside run() so the SQLite
    connection lives on the worker thread (sqlite3 connections enforce
    creator-thread ownership by default). WAL mode lets this writer
    interleave safely with the main thread's per-session reindex
    writes from save sites.

    Emits scan_complete(kind, reindexed_count) exactly once before
    finished fires.
    """

    scan_complete = pyqtSignal(str, int)

    def __init__(self, *, session_ids: list, kind: str) -> None:
        super().__init__(None)
        self._session_ids = list(session_ids)
        self._kind = kind

    def run(self) -> None:
        reindexed = 0
        # Local imports keep app.py's top-level import graph small
        # and isolate the SQLite open to this thread.
        try:
            from .models.search_index import SearchIndex
            from .utils.search_indexer import reindex_session
        except Exception:
            log.exception("search scan worker: imports failed")
            self.scan_complete.emit(self._kind, 0)
            return
        try:
            index = SearchIndex()
        except Exception:
            log.exception("search scan worker: SearchIndex open failed")
            self.scan_complete.emit(self._kind, 0)
            return
        try:
            for sid in self._session_ids:
                if self.isInterruptionRequested():
                    break
                try:
                    if reindex_session(index, sid):
                        reindexed += 1
                except Exception:
                    log.exception("search scan worker: reindex %s failed", sid)
        finally:
            try:
                index.close()
            except Exception:
                log.exception("search scan worker: index close failed")
        self.scan_complete.emit(self._kind, reindexed)


class _AudioExportWorker(QThread):
    """Run audio/export.export_mixed off the GUI thread.

    Emits ``finished_with_result(error_message)`` exactly once: an
    empty string means success, anything else is the failure reason
    for the MessageBox.
    """

    finished_with_result = pyqtSignal(str)

    def __init__(self, mic_path, sys_path, target) -> None:
        super().__init__()
        self.setObjectName("AudioExportWorker")
        self._mic = mic_path
        self._sys = sys_path
        self._target = target

    def run(self) -> None:  # type: ignore[override]
        try:
            from .audio.export import export_mixed  # noqa: PLC0415
            export_mixed(self._mic, self._sys, self._target)
            self.finished_with_result.emit("")
        except Exception as exc:  # noqa: BLE001 -- surfaced to the user verbatim
            self.finished_with_result.emit(str(exc))


class _VideoExportWorker(QThread):
    """Run audio/video_export.export_video off the GUI thread.

    Emits ``progress_changed(pct)`` while encoding (0-100) and
    ``finished_with_result(error_message)`` once at the end. The
    progress callback fires from the encoder loop, which runs in this
    thread; Qt marshals the signal back to the GUI thread for the
    QProgressDialog update.
    """

    progress_changed = pyqtSignal(int)
    finished_with_result = pyqtSignal(str)

    # Sentinel reported via finished_with_result when the export was
    # cancelled by the user (#60).
    CANCELLED_RESULT = "<cancelled>"

    def __init__(
        self, mic_path, sys_path, screenshots, transcript_text, target,
        *, quality: str = "medium",
    ) -> None:
        super().__init__()
        self.setObjectName("VideoExportWorker")
        self._mic = mic_path
        self._sys = sys_path
        self._screenshots = screenshots
        self._transcript_text = transcript_text
        self._target = target
        self._quality = quality
        import threading  # noqa: PLC0415
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:  # type: ignore[override]
        from .utils.cancellation import ExportCancelled  # noqa: PLC0415
        try:
            from .audio.video_export import export_video  # noqa: PLC0415
            export_video(
                self._mic, self._sys, self._screenshots,
                self._transcript_text, self._target,
                progress=self.progress_changed.emit,
                quality=self._quality,
                should_cancel=self._cancel_event.is_set,
            )
            self.finished_with_result.emit("")
        except ExportCancelled:
            log.info("video export cancelled by user")
            self.finished_with_result.emit(self.CANCELLED_RESULT)
        except Exception as exc:  # noqa: BLE001 -- surfaced to the user verbatim
            self.finished_with_result.emit(str(exc))


class _HighlightAudioExportWorker(QThread):
    """Run audio/highlights_export.export_highlights_audio off the GUI thread.

    Mirrors _AudioExportWorker's signal shape -- empty string on
    success, error message on failure, CANCELLED_RESULT on user
    cancel (#60).
    """

    progress_changed = pyqtSignal(int)
    finished_with_result = pyqtSignal(str)

    CANCELLED_RESULT = "<cancelled>"

    def __init__(self, mic_path, sys_path, highlights, target) -> None:
        super().__init__()
        self.setObjectName("HighlightAudioExportWorker")
        self._mic = mic_path
        self._sys = sys_path
        self._highlights = highlights
        self._target = target
        import threading  # noqa: PLC0415
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:  # type: ignore[override]
        from .utils.cancellation import ExportCancelled  # noqa: PLC0415
        try:
            from .audio.highlights_export import export_highlights_audio  # noqa: PLC0415
            export_highlights_audio(
                self._mic, self._sys, self._highlights, self._target,
                progress=self.progress_changed.emit,
                should_cancel=self._cancel_event.is_set,
            )
            self.finished_with_result.emit("")
        except ExportCancelled:
            log.info("highlight audio export cancelled by user")
            self.finished_with_result.emit(self.CANCELLED_RESULT)
        except Exception as exc:  # noqa: BLE001
            self.finished_with_result.emit(str(exc))


class _HighlightVideoExportWorker(QThread):
    """Run audio/highlights_export.export_highlights_video off the GUI thread."""

    progress_changed = pyqtSignal(int)
    finished_with_result = pyqtSignal(str)

    CANCELLED_RESULT = "<cancelled>"

    def __init__(
        self, mic_path, sys_path, screenshots, transcript_text,
        highlights, target,
        *,
        session_title: str = "",
        session_started_at_iso: str = "",
        quality: str = "medium",
    ) -> None:
        super().__init__()
        self.setObjectName("HighlightVideoExportWorker")
        self._mic = mic_path
        self._sys = sys_path
        self._screenshots = screenshots
        self._transcript_text = transcript_text
        self._highlights = highlights
        self._target = target
        self._session_title = session_title
        self._session_started_at_iso = session_started_at_iso
        self._quality = quality
        import threading  # noqa: PLC0415
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:  # type: ignore[override]
        from .utils.cancellation import ExportCancelled  # noqa: PLC0415
        try:
            from .audio.highlights_export import export_highlights_video  # noqa: PLC0415
            export_highlights_video(
                self._mic, self._sys, self._screenshots,
                self._transcript_text, self._highlights, self._target,
                session_title=self._session_title,
                session_started_at_iso=self._session_started_at_iso,
                progress=self.progress_changed.emit,
                quality=self._quality,
                should_cancel=self._cancel_event.is_set,
            )
            self.finished_with_result.emit("")
        except ExportCancelled:
            log.info("highlights video export cancelled by user")
            self.finished_with_result.emit(self.CANCELLED_RESULT)
        except Exception as exc:  # noqa: BLE001
            self.finished_with_result.emit(str(exc))


class _ExportPackageWorker(QThread):
    """Run utils.export_package.build_session_package off the GUI thread.

    `status_changed` is emitted on each phase transition (and on
    encoder sub-phases like "Encoding audio" within an mp4 render).
    The progress dialog plumbs these into a scrolling log so the
    user can see what's happening during a long export (#53).

    Cancellation (#60): MainApp connects the dialog's
    cancel_requested signal to `cancel()`. The worker flips a
    threading.Event that the encoder loops poll periodically; on
    the next checkpoint they raise ExportCancelled. The exception
    propagates out, the orchestrator deletes partial output, and
    the worker emits `<cancelled>` as the result so the dialog
    can suppress the post-completion error message.
    """

    progress_changed = pyqtSignal(int)
    status_changed = pyqtSignal(str)
    finished_with_result = pyqtSignal(str)

    # Sentinel reported via finished_with_result when the export was
    # cancelled by the user (vs. failed). Callers check for this
    # exact string before treating the result as an error.
    CANCELLED_RESULT = "<cancelled>"

    def __init__(self, options, target_path, *, compress: bool = True) -> None:
        super().__init__()
        self.setObjectName("ExportPackageWorker")
        self._options = options
        self._target = target_path
        self._compress = compress
        import threading  # noqa: PLC0415
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        """Request cancellation. Safe to call from the GUI thread.

        Encoder loops poll `_cancel_event.is_set` at frame / chunk
        boundaries and raise ExportCancelled when set.
        """
        self._cancel_event.set()

    def run(self) -> None:  # type: ignore[override]
        from .utils.cancellation import ExportCancelled  # noqa: PLC0415
        try:
            from .utils.export_package import build_session_package  # noqa: PLC0415
            build_session_package(
                self._options, self._target,
                progress=self.progress_changed.emit,
                status=self.status_changed.emit,
                should_cancel=self._cancel_event.is_set,
                compress=self._compress,
            )
            self.finished_with_result.emit("")
        except ExportCancelled:
            log.info("export package cancelled by user")
            self.finished_with_result.emit(self.CANCELLED_RESULT)
        except Exception as exc:  # noqa: BLE001
            self.finished_with_result.emit(str(exc))


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

    # Rotate the previous run's log out of the way and prune ancient
    # archives. The current launch gets a fresh meeting_notetaker.log
    # so a problem in this session doesn't get buried under prior
    # noise. See utils.paths.rotate_log_on_launch.
    rotate_log_on_launch()

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
