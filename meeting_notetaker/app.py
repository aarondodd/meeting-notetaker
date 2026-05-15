"""MainApp -- the top-level orchestrator that wires UI, controller, tray, and store.

main() is the entry point used by main.py and the pyinstaller spec.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import Optional

from PyQt6.QtCore import QObject, Qt
from PyQt6.QtWidgets import QApplication, QMessageBox

from .controller import SessionController
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
from .models.transcript import TranscriptStore
from .transcription import model_manager
from .ui.devices_dialog import DevicesDialog
from .ui.main_window import MainWindow
from .ui.new_session_dialog import NewSessionDialog
from .ui.progress import run_with_progress
from .ui.prompt_dialog import GeneratePromptDialog, PasteNotesDialog
from .ui.settings_dialog import SettingsDialog
from .ui.tray import TrayIcon
from .utils import prompts as prompts_mod
from .utils.config import Config
from .utils.icons import app_icon
from .utils.paths import app_data_dir, log_path
from .utils.single_instance import acquire as acquire_lock, release as release_lock


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
        self.store = SessionStore()
        self.controller = SessionController(self.store, self.config, parent=self)
        self.window = MainWindow()
        self.tray = TrayIcon(self.window)

        self._wire_signals()
        self._refresh_session_list()
        self._handle_crash_recovery()
        self._warn_if_store_python()

        self.window.show()
        self.tray.set_state("idle")

    # ---- wiring ------------------------------------------------------------

    def _wire_signals(self) -> None:
        self.window.new_session_requested.connect(self._on_new_session)
        self.window.open_settings_requested.connect(self._on_settings)
        self.window.open_devices_dialog_requested.connect(self._on_devices)
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

        sv = self.window.session_view
        sv.start_clicked.connect(self._on_start_clicked)
        sv.pause_clicked.connect(lambda _sid: self.controller.pause_session())
        sv.resume_clicked.connect(lambda _sid: self.controller.resume_session())
        sv.stop_clicked.connect(lambda _sid: self.controller.stop_session())
        sv.generate_prompt_clicked.connect(self._on_generate_prompt)
        sv.paste_notes_clicked.connect(self._on_paste_notes)
        sv.retain_audio_toggled.connect(self.controller.set_retain_audio)

        self.controller.state_changed.connect(self._on_session_state_changed)
        self.controller.segment_arrived.connect(self._on_segment_arrived)
        self.controller.transcript_replaced.connect(self._on_transcript_replaced)
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
        self._refresh_session_list(select=session.id)

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
        try:
            when = datetime.fromisoformat(session.created_at.replace("Z", "+00:00"))
        except ValueError:
            when = datetime.now(timezone.utc)
        dialog = GeneratePromptDialog(
            session_title=session.title,
            session_date=when,
            transcript=transcript,
            templates=prompts_mod.list_templates(),
            parent=self.window,
        )
        dialog.exec()

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

    def _on_settings(self) -> None:
        dialog = SettingsDialog(self.config, parent=self.window)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        errors = self.config.validate()
        if errors:
            QMessageBox.warning(self.window, "Settings", "\n".join(errors))
            return
        self.config.save()
        self.window.status("Settings saved.", timeout_ms=4000)

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


def main() -> int:
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
    qt_app.setOrganizationName("Aaron Dodd")
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
