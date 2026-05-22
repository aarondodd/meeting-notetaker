"""App-level settings dialog.

Exposes: model size, retain-audio default, VAD enable + min-silence threshold,
capture-only mode, audio device pickers, calendar watch, user name. Mutates
the supplied Config in-place on accept; caller is responsible for persisting
and applying the new values. The interface follows the OS dark/light setting
automatically, so there is no theme picker.
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices

from ..audio.devices import AudioDevice, list_input_devices, list_loopback_devices
from ..automation import installer
from ..automation.targets import ALL_TARGETS, get_target
from ..diarization import user_voiceprint
from ..diarization.store import open_speaker_store
from ..utils.config import Config, VALID_MODEL_SIZES
from ..utils.paths import prompts_dir, vocabulary_path
from ..utils.vocabulary import seed_vocabulary_file
from .automation_install_dialog import AutomationInstallDialog
from .speakers_manage_dialog import SpeakersManageDialog
from .voice_enrollment_dialog import VoiceEnrollmentDialog


class SettingsDialog(QDialog):
    def __init__(
        self,
        config: Config,
        parent: Optional[QWidget] = None,
        *,
        ping_extension: Optional[callable] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.resize(560, 600)
        self._config = config
        # The install wizard's Verify step probes the live bridge to
        # confirm the extension is reachable. The Settings dialog
        # doesn't own a bridge; the controller does. Inject the probe
        # function here so the wizard can call it without a circular
        # dependency. ``None`` is fine in tests / off-Windows where the
        # wizard falls back to "is_fully_installed()".
        self._ping_extension = ping_extension

        # Outer layout: scroll area on top, button bar at the bottom outside
        # the scroll region so OK/Cancel are always reachable. The content
        # widget's layout is what every group below adds to.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll, 1)

        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)

        # Transcription group ---------------------------------------------
        tx_group = QGroupBox("Transcription", self)
        tx_form = QFormLayout(tx_group)

        self._model_picker = QComboBox(self)
        for size in VALID_MODEL_SIZES:
            self._model_picker.addItem(size)
        self._model_picker.setCurrentText(config.transcription.model_size)
        self._model_picker.setToolTip(
            "tiny.en: fastest, lowest accuracy.\n"
            "base.en: small download.\n"
            "small.en: recommended default for English meetings.\n"
            "medium.en: slowest but most accurate; ~3x small.en CPU cost."
        )
        tx_form.addRow("Model size:", self._model_picker)

        self._capture_only = QCheckBox(
            "Capture-only mode (skip live transcription; full pass on stop)", self
        )
        self._capture_only.setChecked(config.transcription.capture_only_mode)
        self._capture_only.setToolTip(
            "Useful for long meetings on a slow CPU. Live view stays empty during the "
            "meeting; full transcript appears after you click Stop."
        )
        tx_form.addRow(self._capture_only)

        self._skip_batch = QCheckBox(
            "Skip post-Stop refinement (use live transcript as final)", self
        )
        self._skip_batch.setChecked(config.transcription.skip_batch_refinement)
        self._skip_batch.setToolTip(
            "After Stop, do NOT re-run Whisper over the full recording. The live "
            "transcript (already on disk) becomes the final transcript. Skips the "
            "long post-meeting wait (a 30-min meeting on small.en is ~30 min of CPU "
            "to re-transcribe). Quality is slightly lower than the batch pass -- "
            "small.en's live windows are 10s with 5s overlap, so cross-sentence "
            "context is shorter than what the batch pass gets. Recommended if you "
            "find the live view's quality acceptable."
        )
        tx_form.addRow(self._skip_batch)

        # Default is beam_size=1 (fast). Checking this box opts INTO
        # beam_size=5, which is slower but slightly more accurate. The
        # underlying config field is still `fast_batch` (True = default,
        # fast); the UI is inverted because we now default to the fast
        # path and "High accuracy" is the rare opt-in.
        self._high_accuracy = QCheckBox(
            "High accuracy mode (beam_size=5, ~3x slower)", self
        )
        self._high_accuracy.setChecked(not config.transcription.fast_batch)
        self._high_accuracy.setToolTip(
            "Default is greedy decoding (beam_size=1), which is roughly 3x "
            "faster on the post-Stop refinement pass with a mild WER cost. "
            "For meeting transcripts that feed into an LLM synthesis pass, "
            "the quality drop is usually invisible -- the LLM smooths the "
            "kinds of errors greedy makes (homophones, mild punctuation). "
            "Check this box only if you need verbatim transcripts and never "
            "use the synthesis path."
        )
        tx_form.addRow(self._high_accuracy)

        # CT2 tuning knobs ------------------------------------------------
        ct2_blurb = QLabel(
            "CTranslate2 inference threads. Defaults are tuned to "
            "saturate a multi-core CPU while keeping mic + sys batch "
            "passes from oversubscribing physical cores. cpu_threads=0 "
            "auto-derives from cpu_count / num_workers, minimum 2. "
            "Total OS threads in flight = cpu_threads * num_workers; "
            "keep <= physical core count to avoid L3 cache thrash.",
            self,
        )
        ct2_blurb.setWordWrap(True)
        tx_form.addRow(ct2_blurb)

        self._cpu_threads_spin = QSpinBox(self)
        self._cpu_threads_spin.setRange(0, 128)
        self._cpu_threads_spin.setValue(config.transcription.cpu_threads)
        self._cpu_threads_spin.setToolTip(
            "CT2 cpu_threads: OpenMP threads PER inference call. 0 = auto."
        )
        tx_form.addRow("CPU threads per worker:", self._cpu_threads_spin)

        self._num_workers_spin = QSpinBox(self)
        self._num_workers_spin.setRange(1, 8)
        self._num_workers_spin.setValue(config.transcription.num_workers)
        self._num_workers_spin.setToolTip(
            "CT2 num_workers: number of concurrent transcribe() slots "
            "inside one model instance. 2 lets a two-source meeting "
            "(mic + sys) run both batch passes truly in parallel."
        )
        tx_form.addRow("Parallel workers:", self._num_workers_spin)

        vocab_blurb = QLabel(
            "Custom vocabulary biases the transcriber toward proper nouns and "
            "in-house terms it would otherwise mis-hear (product names, "
            "internal acronyms, vendor names). One phrase per line; '#' is a "
            "comment. Edits take effect on the next session.",
            self,
        )
        vocab_blurb.setWordWrap(True)
        tx_form.addRow(vocab_blurb)
        self._open_vocab_btn = QPushButton("Open Vocabulary File", self)
        self._open_vocab_btn.clicked.connect(self._open_vocabulary_file)
        tx_form.addRow(self._open_vocab_btn)
        layout.addWidget(tx_group)

        # Audio group ------------------------------------------------------
        audio_group = QGroupBox("Audio", self)
        audio_form = QFormLayout(audio_group)
        audio_blurb = QLabel(
            "(System default) follows whatever Windows is configured to use. "
            "Pick a specific device only when you want to override that. "
            "Duplicate entries (e.g. the same mic at index 1 and index 10) "
            "are normal -- Windows exposes each device through multiple host "
            "APIs (MME, WASAPI, WDM-KS); persisting by name picks the right "
            "one across reboots.",
            self,
        )
        audio_blurb.setWordWrap(True)
        audio_form.addRow(audio_blurb)

        self._mic_devices = list_input_devices()
        self._mic_picker = QComboBox(self)
        _populate_device_picker(
            self._mic_picker, self._mic_devices, config.audio.mic_device_name
        )
        self._mic_picker.setToolTip(
            "Microphone capture device. (System default) follows the OS-level "
            "default, which is usually correct. Persists by name so the same "
            "device is picked after replug or reboot."
        )
        audio_form.addRow("Mic device:", self._mic_picker)

        self._loopback_devices = list_loopback_devices()
        self._loopback_picker = QComboBox(self)
        _populate_device_picker(
            self._loopback_picker, self._loopback_devices, config.audio.loopback_device_name
        )
        if not self._loopback_devices:
            # Not on Windows, or pyaudiowpatch missing -- nothing meaningful to pick.
            self._loopback_picker.setEnabled(False)
            self._loopback_picker.setToolTip(
                "System-audio loopback is Windows-only (requires pyaudiowpatch). "
                "Disabled on this platform."
            )
        else:
            self._loopback_picker.setToolTip(
                "System audio capture (WASAPI loopback). (System default) picks "
                "the loopback paired with the OS default output."
            )
        audio_form.addRow("Loopback device:", self._loopback_picker)

        self._retain_default = QCheckBox(
            "Retain audio files after transcription (default for new sessions)", self
        )
        self._retain_default.setChecked(config.audio.retain_audio_default)
        self._retain_default.setToolTip(
            "Default value of the 'Keep recording' checkbox in the New Session dialog. "
            "Can be overridden per session."
        )
        audio_form.addRow(self._retain_default)

        self._vad_enabled = QCheckBox("Enable VAD trimming (recommended)", self)
        self._vad_enabled.setChecked(config.audio.vad_enabled)
        self._vad_enabled.setToolTip(
            "When on, faster-whisper trims silent stretches before decoding. "
            "Saves CPU on quiet loopback streams. Disable if it clips speech."
        )
        audio_form.addRow(self._vad_enabled)

        self._vad_slider = QSlider(Qt.Orientation.Horizontal, self)
        self._vad_slider.setMinimum(100)
        self._vad_slider.setMaximum(2000)
        self._vad_slider.setSingleStep(50)
        self._vad_slider.setPageStep(100)
        self._vad_slider.setValue(config.audio.vad_min_silence_ms)
        self._vad_value_label = QLabel(f"{config.audio.vad_min_silence_ms} ms", self)
        self._vad_slider.valueChanged.connect(
            lambda v: self._vad_value_label.setText(f"{v} ms")
        )
        audio_form.addRow("VAD min silence:", self._vad_slider)
        audio_form.addRow("", self._vad_value_label)
        layout.addWidget(audio_group)

        # Speakers group ---------------------------------------------------
        speakers_group = QGroupBox("Speakers", self)
        speakers_form = QFormLayout(speakers_group)
        speakers_blurb = QLabel(
            "After each meeting, the app runs a speaker-identification "
            "pass on the system-audio loopback channel: it groups the "
            "recording into per-speaker turns and matches each group "
            "against the stored speaker library, prompting you to label "
            "any unrecognized voices. Future meetings auto-recognize "
            "the same speakers and label the transcript with real "
            "names instead of \"Them:\". Disable to skip the post-stop "
            "identification pass entirely. Nothing leaves the machine -- "
            "embeddings are stored locally in speakers.db.",
            self,
        )
        speakers_blurb.setWordWrap(True)
        speakers_form.addRow(speakers_blurb)

        self._speakers_enabled = QCheckBox("Enable speaker identification", self)
        self._speakers_enabled.setChecked(config.speakers.enabled)
        self._speakers_enabled.setToolTip(
            "When off, recordings still transcribe normally but use "
            "the generic \"Them:\" label for everyone besides the mic. "
            "No speaker library is consulted or updated."
        )
        speakers_form.addRow(self._speakers_enabled)

        self._match_threshold_slider = QSlider(Qt.Orientation.Horizontal, self)
        self._match_threshold_slider.setMinimum(50)
        self._match_threshold_slider.setMaximum(95)
        self._match_threshold_slider.setSingleStep(1)
        self._match_threshold_slider.setPageStep(5)
        self._match_threshold_slider.setValue(int(round(config.speakers.match_threshold * 100)))
        self._match_threshold_label = QLabel(
            f"{int(round(config.speakers.match_threshold * 100))}%", self
        )
        self._match_threshold_slider.valueChanged.connect(
            lambda v: self._match_threshold_label.setText(f"{v}%")
        )
        self._match_threshold_slider.setToolTip(
            "Cosine-similarity threshold to auto-match a meeting voice "
            "against a stored speaker. Higher = stricter (fewer false "
            "matches but more unknowns surfaced for manual labeling); "
            "lower = looser (more auto-labels but higher risk of "
            "calling Bob Alice). The ECAPA-TDNN encoder used in v0.5 "
            "puts same-speaker pairs around 0.7-0.9 and different "
            "speakers around 0.1-0.3, so the workable range is "
            "roughly 0.5-0.8; 0.75 is a reasonable default."
        )
        speakers_form.addRow("Match threshold:", self._match_threshold_slider)
        speakers_form.addRow("", self._match_threshold_label)

        self._merge_threshold_slider = QSlider(Qt.Orientation.Horizontal, self)
        self._merge_threshold_slider.setMinimum(50)
        self._merge_threshold_slider.setMaximum(95)
        self._merge_threshold_slider.setSingleStep(1)
        self._merge_threshold_slider.setPageStep(5)
        self._merge_threshold_slider.setValue(int(round(config.speakers.merge_threshold * 100)))
        self._merge_threshold_label = QLabel(
            f"{int(round(config.speakers.merge_threshold * 100))}%", self
        )
        self._merge_threshold_slider.valueChanged.connect(
            lambda v: self._merge_threshold_label.setText(f"{v}%")
        )
        self._merge_threshold_slider.setToolTip(
            "Cosine-similarity threshold for fusing two voiced turns "
            "into the same anonymous cluster (speaker isolation). "
            "Higher = stricter (less risk of merging two real speakers "
            "into one cluster, but a single speaker may split across "
            "2-3 clusters that you then merge in the walker); lower = "
            "looser (cleaner cluster count but more cross-speaker "
            "merges). Distinct from Match threshold above, which only "
            "controls auto-labeling from the speaker library. Raise "
            "this knob first when you see two real people getting "
            "munged into one cluster. With the v0.5 ECAPA-TDNN encoder "
            "the workable range is roughly 0.5-0.8; try 0.78-0.82 if "
            "75% still merges similar voices."
        )
        speakers_form.addRow("Merge threshold:", self._merge_threshold_slider)
        speakers_form.addRow("", self._merge_threshold_label)

        self._manage_speakers_btn = QPushButton("Manage Speakers...", self)
        self._manage_speakers_btn.setToolTip(
            "Open the stored speakers list to rename or remove entries."
        )
        self._manage_speakers_btn.clicked.connect(self._open_manage_speakers)
        speakers_form.addRow(self._manage_speakers_btn)

        # Voice enrollment -- a stored embedding of the user's own voice.
        # Lets the refiner attribute mic-channel speech that actually came
        # from the loopback (bleed) to the right system-audio speaker
        # rather than blanket-labeling everything on the mic as the user.
        self._voice_status_label = QLabel("", self)
        self._voice_status_label.setWordWrap(True)
        self._voice_status_label.setStyleSheet("color: palette(text);")
        self._record_voice_btn = QPushButton("", self)
        self._record_voice_btn.setToolTip(
            "Record a short sample of your voice. The embedding stays on "
            "this machine -- no audio leaves it. Used to disambiguate mic "
            "vs system audio when both pick up the same speaker."
        )
        self._record_voice_btn.clicked.connect(self._open_voice_enrollment)
        self._clear_voice_btn = QPushButton("Clear Sample", self)
        self._clear_voice_btn.setToolTip(
            "Delete the stored voiceprint. Mic-channel speech will go back "
            "to being labeled as the user by default."
        )
        self._clear_voice_btn.clicked.connect(self._clear_voiceprint)
        voice_row = QHBoxLayout()
        voice_row.addWidget(self._record_voice_btn)
        voice_row.addWidget(self._clear_voice_btn)
        voice_row.addStretch(1)
        speakers_form.addRow("Your voice:", self._voice_status_label)
        speakers_form.addRow("", voice_row)
        self._refresh_voiceprint_row()
        layout.addWidget(speakers_group)

        # Calendar group ---------------------------------------------------
        calendar_group = QGroupBox("Calendar (Outlook)", self)
        calendar_form = QFormLayout(calendar_group)
        calendar_blurb = QLabel(
            "When enabled, the app polls your local Outlook profile (via COM, "
            "no network calls) and pops a tray notification a few minutes "
            "before each meeting starts. Click the notification to open New "
            "Session pre-filled with the meeting subject, attendees, and "
            "agenda. Recording is never started automatically -- you click "
            "Start when you're ready.",
            self,
        )
        calendar_blurb.setWordWrap(True)
        calendar_form.addRow(calendar_blurb)

        self._watch_calendar = QCheckBox("Watch Outlook calendar", self)
        self._watch_calendar.setChecked(config.calendar.watch_calendar)
        self._watch_calendar.setToolTip(
            "Requires Outlook installed and running on this machine. "
            "Windows-only; safely no-ops on other platforms."
        )
        calendar_form.addRow(self._watch_calendar)

        self._window_slider = QSlider(Qt.Orientation.Horizontal, self)
        self._window_slider.setMinimum(1)
        self._window_slider.setMaximum(30)
        self._window_slider.setSingleStep(1)
        self._window_slider.setValue(config.calendar.window_minutes)
        self._window_value_label = QLabel(
            f"{config.calendar.window_minutes} min", self
        )
        self._window_slider.valueChanged.connect(
            lambda v: self._window_value_label.setText(f"{v} min")
        )
        calendar_form.addRow("Notify within:", self._window_slider)
        calendar_form.addRow("", self._window_value_label)
        layout.addWidget(calendar_group)

        # Ad-hoc meeting detection group -----------------------------------
        detection_group = QGroupBox("Detect ad-hoc meetings", self)
        detection_form = QFormLayout(detection_group)
        detection_blurb = QLabel(
            "When enabled, the app watches your system audio for an active "
            "session from a known meeting app (Teams, Zoom, etc.). If audio "
            "sustains long enough to look like a call rather than a "
            "notification chirp, the tray pops a toast: click to open New "
            "Session. Recording never auto-starts. Windows-only; no audio "
            "is captured -- the OS already exposes which apps are playing.",
            self,
        )
        detection_blurb.setWordWrap(True)
        detection_form.addRow(detection_blurb)

        self._detect_enabled = QCheckBox("Detect active meeting audio", self)
        self._detect_enabled.setChecked(config.detection.enabled)
        self._detect_enabled.setToolTip(
            "Requires pycaw + psutil (Windows wheels). Safely no-ops on "
            "other platforms or when pycaw is unavailable."
        )
        detection_form.addRow(self._detect_enabled)

        self._detect_duration_slider = QSlider(Qt.Orientation.Horizontal, self)
        self._detect_duration_slider.setMinimum(5)
        self._detect_duration_slider.setMaximum(120)
        self._detect_duration_slider.setSingleStep(5)
        self._detect_duration_slider.setValue(config.detection.min_duration_sec)
        self._detect_duration_label = QLabel(
            f"{config.detection.min_duration_sec} sec", self
        )
        self._detect_duration_slider.valueChanged.connect(
            lambda v: self._detect_duration_label.setText(f"{v} sec")
        )
        detection_form.addRow("Sustained for:", self._detect_duration_slider)
        detection_form.addRow("", self._detect_duration_label)

        self._detect_cooldown_slider = QSlider(Qt.Orientation.Horizontal, self)
        self._detect_cooldown_slider.setMinimum(1)
        self._detect_cooldown_slider.setMaximum(60)
        self._detect_cooldown_slider.setSingleStep(1)
        self._detect_cooldown_slider.setValue(config.detection.cooldown_minutes)
        self._detect_cooldown_label = QLabel(
            f"{config.detection.cooldown_minutes} min", self
        )
        self._detect_cooldown_slider.valueChanged.connect(
            lambda v: self._detect_cooldown_label.setText(f"{v} min")
        )
        detection_form.addRow("Re-prompt after:", self._detect_cooldown_slider)
        detection_form.addRow("", self._detect_cooldown_label)

        self._detect_allowlist_edit = QLineEdit(self)
        self._detect_allowlist_edit.setText(
            ", ".join(config.detection.app_allowlist)
        )
        self._detect_allowlist_edit.setToolTip(
            "Comma-separated list of process executable names to watch "
            "(case-insensitive). Browser-based meetings are intentionally "
            "excluded -- chrome.exe / msedge.exe also play music + video."
        )
        detection_form.addRow("Watch apps:", self._detect_allowlist_edit)
        layout.addWidget(detection_group)

        # UI group ---------------------------------------------------------
        ui_group = QGroupBox("Interface", self)
        ui_form = QFormLayout(ui_group)
        self._user_name_edit = QLineEdit(self)
        self._user_name_edit.setPlaceholderText("e.g. John Smith (leave blank for \"Me\")")
        self._user_name_edit.setText(config.ui.user_name)
        self._user_name_edit.setToolTip(
            "How the user's microphone is labeled in the transcript and synthesis "
            "prompt. When set, the LLM sees your real name and can attribute action "
            "items to you instead of \"Me\" or \"TBD\". On-disk transcripts always "
            "store the neutral \"Me:\" label and get rewritten on display + at "
            "prompt-generation time."
        )
        ui_form.addRow("Your name:", self._user_name_edit)
        layout.addWidget(ui_group)

        # Synthesis Automation group --------------------------------------
        auto_group = QGroupBox("Synthesis Automation (optional)", self)
        auto_layout = QVBoxLayout(auto_group)
        auto_blurb = QLabel(
            "When enabled, the Generate Synthesis Prompt + Paste Response "
            "Back buttons are replaced by a single \"Send to <LLM>\" "
            "button. The browser stays the intermediary -- the extension "
            "drives the same chat window you would use manually. "
            "Requires a one-time install (unpacked Chrome extension).",
            self,
        )
        auto_blurb.setWordWrap(True)
        auto_layout.addWidget(auto_blurb)

        self._auto_enabled = QCheckBox("Enable synthesis automation", self)
        self._auto_enabled.setChecked(config.synthesis.automation_enabled)
        auto_layout.addWidget(self._auto_enabled)

        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("Send to:", self))
        self._auto_target = QComboBox(self)
        for tgt in ALL_TARGETS:
            label = tgt.label
            if not tgt.implemented:
                label = f"{label} (not yet implemented)"
            self._auto_target.addItem(label, tgt.key)
        # Select current.
        for i in range(self._auto_target.count()):
            if self._auto_target.itemData(i) == config.synthesis.llm_target:
                self._auto_target.setCurrentIndex(i)
                break
        target_row.addWidget(self._auto_target, 1)
        auto_layout.addLayout(target_row)

        # Install / Status row
        self._auto_status = QLabel(self)
        self._auto_status.setWordWrap(True)
        self._auto_status.setTextFormat(Qt.TextFormat.RichText)
        auto_layout.addWidget(self._auto_status)

        auto_actions = QHBoxLayout()
        self._auto_install_btn = QPushButton("Install / Verify...", self)
        self._auto_install_btn.clicked.connect(self._on_install_clicked)
        auto_actions.addWidget(self._auto_install_btn)
        self._auto_uninstall_btn = QPushButton("Uninstall bridge", self)
        self._auto_uninstall_btn.setToolTip(
            "Removes the native-messaging bridge registration. Leaves "
            "the extension files on disk so you can re-verify later "
            "without re-extracting. To fully remove the extension from "
            "Chrome, also remove it at chrome://extensions."
        )
        self._auto_uninstall_btn.clicked.connect(self._on_uninstall_clicked)
        auto_actions.addWidget(self._auto_uninstall_btn)
        auto_actions.addStretch(1)
        auto_layout.addLayout(auto_actions)

        layout.addWidget(auto_group)
        self._refresh_automation_status()

        # Prompts group ----------------------------------------------------
        prompts_group = QGroupBox("Synthesis Prompts", self)
        prompts_layout = QVBoxLayout(prompts_group)
        prompts_blurb = QLabel(
            "Prompt templates are Markdown files in the folder below. Edit any file "
            "to change wording, or drop in a new .md file to add a template -- it "
            "appears in the Generate Synthesis Prompt picker on next open. Placeholders: "
            "{{session_title}}, {{date}}, {{transcript}}, {{live_notes}}, {{attendees}}.",
            self,
        )
        prompts_blurb.setWordWrap(True)
        prompts_layout.addWidget(prompts_blurb)
        prompts_row = QHBoxLayout()
        self._open_prompts_btn = QPushButton("Open Prompts Folder", self)
        self._open_prompts_btn.clicked.connect(self._open_prompts_folder)
        prompts_row.addWidget(self._open_prompts_btn)
        prompts_row.addStretch(1)
        prompts_layout.addLayout(prompts_row)
        layout.addWidget(prompts_group)

        layout.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        # Button bar lives outside the scroll area so OK/Cancel are always
        # reachable, even if the content overflows.
        outer.addWidget(buttons)

    def _on_accept(self) -> None:
        self._config.transcription.model_size = self._model_picker.currentText()
        self._config.transcription.capture_only_mode = self._capture_only.isChecked()
        self._config.transcription.skip_batch_refinement = self._skip_batch.isChecked()
        self._config.transcription.fast_batch = not self._high_accuracy.isChecked()
        self._config.transcription.cpu_threads = self._cpu_threads_spin.value()
        self._config.transcription.num_workers = self._num_workers_spin.value()
        self._config.audio.retain_audio_default = self._retain_default.isChecked()
        self._config.audio.vad_enabled = self._vad_enabled.isChecked()
        self._config.audio.vad_min_silence_ms = self._vad_slider.value()
        self._config.audio.mic_device_name = self._mic_picker.currentData() or ""
        self._config.audio.loopback_device_name = self._loopback_picker.currentData() or ""
        self._config.calendar.watch_calendar = self._watch_calendar.isChecked()
        self._config.calendar.window_minutes = int(self._window_slider.value())
        self._config.speakers.enabled = self._speakers_enabled.isChecked()
        self._config.speakers.match_threshold = self._match_threshold_slider.value() / 100.0
        self._config.speakers.merge_threshold = self._merge_threshold_slider.value() / 100.0
        self._config.detection.enabled = self._detect_enabled.isChecked()
        self._config.detection.min_duration_sec = int(self._detect_duration_slider.value())
        self._config.detection.cooldown_minutes = int(self._detect_cooldown_slider.value())
        self._config.detection.app_allowlist = [
            piece.strip()
            for piece in self._detect_allowlist_edit.text().split(",")
            if piece.strip()
        ]
        self._config.ui.user_name = self._user_name_edit.text().strip()
        self._config.synthesis.automation_enabled = self._auto_enabled.isChecked()
        self._config.synthesis.llm_target = (
            self._auto_target.currentData() or "claude"
        )
        self.accept()

    def _open_prompts_folder(self) -> None:
        path = prompts_dir()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _open_vocabulary_file(self) -> None:
        path = seed_vocabulary_file()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _open_manage_speakers(self) -> None:
        store = open_speaker_store()
        try:
            dialog = SpeakersManageDialog(store, parent=self)
            dialog.exec()
        finally:
            store.close()

    def _refresh_voiceprint_row(self) -> None:
        """Sync the Your-voice row to whatever is on disk right now."""
        vp = user_voiceprint.load()
        if vp is None:
            self._voice_status_label.setText("Not enrolled.")
            self._record_voice_btn.setText("Record voice sample...")
            self._clear_voice_btn.setEnabled(False)
        else:
            self._voice_status_label.setText(
                f"Enrolled (recorded {_format_recorded_at(vp.recorded_at)})."
            )
            self._record_voice_btn.setText("Re-record sample...")
            self._clear_voice_btn.setEnabled(True)

    def _open_voice_enrollment(self) -> None:
        device_name = self._mic_picker.currentData() or ""
        dialog = VoiceEnrollmentDialog(device_name=device_name, parent=self)
        dialog.exec()
        # Whether the dialog saved or cancelled, re-read disk so the row
        # always reflects truth.
        self._refresh_voiceprint_row()

    def _clear_voiceprint(self) -> None:
        if user_voiceprint.clear():
            self._refresh_voiceprint_row()

    # ------------------------------------------------------------------
    # Synthesis automation

    def _refresh_automation_status(self) -> None:
        state = installer.installation_state()
        bits: list[str] = []
        if state["extension_extracted"]:
            bits.append(
                f"Extension files: <code>{state['extension_path']}</code>"
            )
        else:
            bits.append("Extension files: not extracted")
        if state["native_manifest_written"]:
            bits.append("Bridge manifest: written")
        else:
            bits.append("Bridge manifest: not written")
        if state.get("registry_chrome"):
            bits.append("Chrome registration: ok")
        elif state["native_manifest_written"]:
            bits.append(
                "<span style='color: #b91c1c;'>Chrome registration: missing</span>"
                if __import__("sys").platform.startswith("win")
                else "Chrome registration: (non-Windows)"
            )
        if installer.is_fully_installed():
            bits.insert(0, "<b style='color: #047857;'>Installed.</b>")
        elif state["extension_extracted"] or state["native_manifest_written"]:
            bits.insert(0, "<b style='color: #b45309;'>Partially installed.</b>")
        else:
            bits.insert(0, "Not installed.")
        self._auto_status.setText("<br>".join(bits))

    def _on_install_clicked(self) -> None:
        # The Chrome native-messaging wrapper has to point at a single
        # executable; CLI args go in the wrapper body, not the manifest.
        # Frozen build: <app.exe> --native-host. Dev: <python> main.py
        # --native-host. We resolve the right shape here and hand the
        # wizard a single zero-arg installer.
        import sys as _sys
        from pathlib import Path as _Path
        from ..utils.paths import package_root

        if getattr(_sys, "frozen", False):
            host_exe = _Path(_sys.executable)
            host_args = ["--native-host"]
        else:
            host_exe = _Path(_sys.executable)
            host_args = [
                str(package_root().parent / "main.py"),
                "--native-host",
            ]

        def do_install() -> dict:
            installer.extract_extension()
            installer.write_native_host_manifest(
                host_executable=host_exe,
                host_args=host_args,
            )
            installer.register_native_host()
            return installer.installation_state()

        wizard = AutomationInstallDialog(
            do_install=do_install,
            ping_extension=self._ping_extension,
            parent=self,
        )
        wizard.exec()
        self._refresh_automation_status()

    def _on_uninstall_clicked(self) -> None:
        installer.uninstall(keep_extension_files=True)
        self._refresh_automation_status()


def _populate_device_picker(
    combo: QComboBox, devices: list[AudioDevice], saved_name: str
) -> None:
    """Fill the combobox with (System default) + each device. Selects saved_name.

    If saved_name doesn't match any currently-available device, prepend a
    "(saved: <name>) -- not currently connected" item so the user sees their
    saved selection wasn't lost when they reopen Settings without the device
    plugged in. Selecting another option overwrites it on accept.
    """
    combo.clear()
    combo.addItem("(System default)", "")

    saved = (saved_name or "").strip()
    matched_index: int | None = None
    saved_lower = saved.lower()
    # Decide if the saved name matches an available device (matches the
    # resolution policy in devices.resolve_device_index).
    if saved:
        for d in devices:
            if d.name == saved:
                matched_index = d.index
                break
        if matched_index is None:
            for d in devices:
                if d.name.lower() == saved_lower:
                    matched_index = d.index
                    break
        if matched_index is None:
            for d in devices:
                if saved_lower in d.name.lower():
                    matched_index = d.index
                    break

    for d in devices:
        suffix = f"  [#{d.index}]"
        combo.addItem(d.name + suffix, d.name)

    if saved and matched_index is None:
        combo.addItem(f"(saved: {saved}) -- not currently connected", saved)
        combo.setCurrentIndex(combo.count() - 1)
    elif saved:
        # Select the entry whose userData matches the saved name (preferring
        # exact device-name match if present, otherwise the substring one).
        for i in range(combo.count()):
            if combo.itemData(i) == saved:
                combo.setCurrentIndex(i)
                return
        # Fall through: substring match -- select the first device whose name
        # contains saved.
        for i in range(1, combo.count()):
            data = combo.itemData(i) or ""
            if saved_lower in data.lower():
                combo.setCurrentIndex(i)
                return


def _format_recorded_at(iso: str) -> str:
    """Render an ISO-8601 UTC timestamp as a local YYYY-MM-DD HH:MM."""
    if not iso:
        return "unknown date"
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso
