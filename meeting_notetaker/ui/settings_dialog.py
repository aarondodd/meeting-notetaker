"""App-level settings dialog.

Exposes: model size, retain-audio default, VAD enable + min-silence threshold,
capture-only mode, audio device pickers, calendar watch, user name. Mutates
the supplied Config in-place on accept; caller is responsible for persisting
and applying the new values. The interface follows the OS dark/light setting
automatically, so there is no theme picker.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices, QFontDatabase

from ..audio.devices import AudioDevice, list_input_devices, list_loopback_devices
from ..automation import installer
from ..automation.targets import ALL_TARGETS, get_target
from ..diarization import user_voiceprint
from ..diarization.store import open_speaker_store
from ..utils.config import (
    Config,
    VALID_MODEL_SIZES,
    VALID_OBSIDIAN_LOCATION_TEMPLATES,
)
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
        classification_store=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(True)
        # Wide enough by default to fit the longest row (Backups folder
        # picker + Browse button, Synthesis Claude project ID) without
        # horizontal scrolling. The nav list on the left takes ~180 px
        # and the right pane needs ~620 px for the widest content.
        # v0.7.5: switched from single long-scrolling pane to nav +
        # stacked pages (#67 follow-up). The right pane carries its
        # own scroll for sections that still overflow vertically
        # (Speakers, Backups, Synthesis).
        self.resize(900, 650)
        self._config = config
        # The install wizard's Verify step probes the live bridge to
        # confirm the extension is reachable. The Settings dialog
        # doesn't own a bridge; the controller does. Inject the probe
        # function here so the wizard can call it without a circular
        # dependency. ``None`` is fine in tests / off-Windows where the
        # wizard falls back to "is_fully_installed()".
        self._ping_extension = ping_extension
        # Phase 2: optional classification store passed through to
        # the Manage Speakers dialog so its Contact-link column
        # renders Contact display_names rather than raw ids. None
        # is fine in tests; production callers (MainApp) pass the
        # live store.
        self._classification_store = classification_store

        # Outer layout: nav (left) + stacked content (right) + button bar
        # below. Each section is built below as a QGroupBox or composite
        # widget and registered via _add_section(label, widget); the call
        # to _assemble_sections at the bottom of __init__ sorts the
        # accumulated list alphabetically and builds the splitter +
        # stack + nav at once.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # Accumulator: each entry is (display label, content widget).
        # Synthesis Automation + Synthesis Prompts are registered once
        # under "Synthesis" via a composite wrapper widget.
        self._sections: list[tuple[str, QWidget]] = []

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
        self._add_section("Transcription", tx_group)

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
                "the loopback paired with the OS default output. Ignored when "
                "'Capture all audio outputs' is on -- that mode opens every "
                "output endpoint and mixes at finalize."
            )
        audio_form.addRow("Loopback device:", self._loopback_picker)

        # Multi-endpoint capture toggle (#85). Default ON. When on,
        # the orchestrator opens one stream per WASAPI output endpoint
        # and mixes sidecars at finalize so a meeting app routing to
        # a non-default endpoint mid-call still gets captured. Users
        # who hit WASAPI quirks on their hardware can flip off to
        # fall back to the single-endpoint path.
        self._multi_endpoint = QCheckBox("Capture all audio outputs (recommended)", self)
        self._multi_endpoint.setChecked(config.audio.multi_endpoint_capture)
        self._multi_endpoint.setToolTip(
            "When on (default), the recorder opens every WASAPI output "
            "endpoint -- laptop speakers, headphones, monitor speakers, "
            "HDMI -- and mixes them into one sys.wav at stop. Protects "
            "against Windows / meeting-app routing changes mid-call. "
            "Sidecar WAVs are temporarily created during recording and "
            "removed at stop. Flip off to fall back to single-endpoint "
            "capture if you hit WASAPI errors on your hardware."
        )
        audio_form.addRow(self._multi_endpoint)
        if not self._loopback_devices:
            self._multi_endpoint.setEnabled(False)
        # Caption beneath the toggle so the trade-off is visible
        # without hovering the tooltip.
        multi_caption = QLabel(
            "<i>Captures every Windows output endpoint and mixes at stop "
            "(default). Off: captures only the picked loopback device.</i>",
            self,
        )
        multi_caption.setWordWrap(True)
        multi_caption.setStyleSheet("color: palette(mid);")
        audio_form.addRow(multi_caption)

        self._retain_default = QCheckBox(
            "Retain audio files after transcription (default for new sessions)", self
        )
        self._retain_default.setChecked(config.audio.retain_audio_default)
        self._retain_default.setToolTip(
            "Default value of the 'Keep recording' checkbox in the New Session dialog. "
            "Can be overridden per session."
        )
        audio_form.addRow(self._retain_default)

        # Retained-recording format. Opus shaves ~96% off WAV at no
        # practical quality loss for speech; FLAC is lossless but
        # ~50% only; WAV is the v0.6.4 escape hatch.
        self._retain_format = QComboBox(self)
        self._retain_format.addItem("Opus (best size, lossy)", "opus")
        self._retain_format.addItem("FLAC (lossless)", "flac")
        self._retain_format.addItem("WAV (no re-encode)", "wav")
        _format_to_index = {"opus": 0, "flac": 1, "wav": 2}
        self._retain_format.setCurrentIndex(
            _format_to_index.get(config.audio.retain_format, 0)
        )
        self._retain_format.setToolTip(
            "Format used for retained recordings. Opus is 24x smaller "
            "than WAV with near-transparent quality for speech. FLAC is "
            "exactly lossless but only 2x smaller. WAV keeps the source "
            "file unchanged (matches v0.6.4 behavior)."
        )
        audio_form.addRow("Retained format:", self._retain_format)

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
        self._add_section("Audio", audio_group)

        # Screen Capture group ---------------------------------------------
        screencap_group = QGroupBox("Screen Capture", self)
        screencap_form = QFormLayout(screencap_group)
        screencap_blurb = QLabel(
            "While recording, the Start Screen Capture button lets "
            "you draw a region on screen and the My Notes sidebar "
            "Capture / Insert buttons snapshot that region. Optional "
            "auto-capture mode snapshots periodically; near-duplicate "
            "captures are filtered out so only meaningful slide / "
            "screen changes are kept.",
            self,
        )
        screencap_blurb.setWordWrap(True)
        screencap_form.addRow(screencap_blurb)

        self._screencap_auto_interval = QSlider(Qt.Orientation.Horizontal, self)
        self._screencap_auto_interval.setMinimum(5)
        self._screencap_auto_interval.setMaximum(300)
        self._screencap_auto_interval.setSingleStep(5)
        self._screencap_auto_interval.setPageStep(15)
        self._screencap_auto_interval.setValue(
            int(config.ui.screen_capture_auto_interval_sec)
        )
        self._screencap_auto_interval_label = QLabel(
            f"{int(config.ui.screen_capture_auto_interval_sec)} s", self,
        )
        self._screencap_auto_interval.valueChanged.connect(
            lambda v: self._screencap_auto_interval_label.setText(f"{v} s")
        )
        self._screencap_auto_interval.setToolTip(
            "Auto-capture cadence -- how often to snapshot the armed "
            "region. 30 s is a good fit for slide-driven meetings; "
            "10-15 s for fast-changing content; 60 s+ when most of "
            "the meeting is talking heads."
        )
        screencap_form.addRow("Auto-capture interval:", self._screencap_auto_interval)
        screencap_form.addRow("", self._screencap_auto_interval_label)

        self._screencap_dedup_threshold = QSlider(Qt.Orientation.Horizontal, self)
        self._screencap_dedup_threshold.setMinimum(0)
        self._screencap_dedup_threshold.setMaximum(32)
        self._screencap_dedup_threshold.setSingleStep(1)
        self._screencap_dedup_threshold.setPageStep(5)
        self._screencap_dedup_threshold.setValue(
            int(config.ui.screen_capture_auto_dedup_threshold)
        )
        self._screencap_dedup_threshold_label = QLabel(
            f"{int(config.ui.screen_capture_auto_dedup_threshold)} bits", self,
        )
        self._screencap_dedup_threshold.valueChanged.connect(
            lambda v: self._screencap_dedup_threshold_label.setText(
                f"{v} bits"
            )
        )
        self._screencap_dedup_threshold.setToolTip(
            "Auto-capture dedup sensitivity. Each fresh capture's "
            "perceptual dHash is compared against the most-recently-"
            "kept image; captures within this many bits (out of 64) "
            "are treated as duplicates and discarded. 0 = only "
            "byte-identical images dedup; 10 = ignore cursor / minor "
            "animation; 20+ = treat moderately-different slides as "
            "the same. Manual Capture / Insert always keep their "
            "image regardless."
        )
        screencap_form.addRow("Dedup threshold:", self._screencap_dedup_threshold)
        screencap_form.addRow("", self._screencap_dedup_threshold_label)
        self._add_section("Screen Capture", screencap_group)

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
        self._add_section("Speakers", speakers_group)

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
        self._add_section("Calendar", calendar_group)

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
        self._add_section("Meeting Detection", detection_group)

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
        self._add_section("Interface", ui_group)

        # Fonts ---------------------------------------------------------
        # Editor face is restricted to monospace fonts (column
        # alignment matters for Markdown tables, code blocks,
        # bulleted lists). Preview face is unrestricted.
        # `editor_font_size == 0` / `preview_font_size == 0` mean
        # "use the platform default" so existing installs that
        # never opened this section don't suddenly downsize their
        # text on first launch of v0.7.7.
        fonts_group = QGroupBox("Fonts", self)
        fonts_layout = QVBoxLayout(fonts_group)
        fonts_blurb = QLabel(
            "Font face and size used by the My Notes editor and the "
            "Markdown preview panes. Editor face is restricted to "
            "monospace fonts so Markdown tables, code blocks, and "
            "bulleted lists line up cleanly. Changes apply on Save.",
            self,
        )
        fonts_blurb.setWordWrap(True)
        fonts_layout.addWidget(fonts_blurb)

        editor_group = QGroupBox("Editor (My Notes)", fonts_group)
        editor_form = QFormLayout(editor_group)
        self._editor_font_picker = QComboBox(self)
        self._editor_font_picker.setEditable(False)
        # Populate with monospace families only. QFontDatabase.families()
        # returns every installed family; the isFixedPitch() filter keeps
        # the dropdown manageable.
        mono_families = sorted(
            f for f in QFontDatabase.families()
            if QFontDatabase.isFixedPitch(f)
        )
        # Auto-default entry sentinel comes first; its data is the
        # empty string so the round-trip into config matches.
        self._editor_font_picker.addItem("(auto: Consolas / monospace)", "")
        for fam in mono_families:
            self._editor_font_picker.addItem(fam, fam)
        # Restore the saved family if it's still available; otherwise
        # land on the auto sentinel.
        saved_editor_family = config.ui.editor_font_family
        idx = self._editor_font_picker.findData(saved_editor_family)
        self._editor_font_picker.setCurrentIndex(idx if idx >= 0 else 0)
        editor_form.addRow("Font:", self._editor_font_picker)
        self._editor_font_size = QSpinBox(self)
        # 0 = use platform default (signaled as "(default)" placeholder
        # via specialValueText so the user sees what the empty value
        # means).
        self._editor_font_size.setRange(0, 72)
        self._editor_font_size.setSpecialValueText("(default)")
        self._editor_font_size.setValue(config.ui.editor_font_size)
        self._editor_font_size.setSuffix(" pt")
        editor_form.addRow("Size:", self._editor_font_size)
        fonts_layout.addWidget(editor_group)

        preview_group = QGroupBox("Preview (Synthesis / Live Notes preview)", fonts_group)
        preview_form = QFormLayout(preview_group)
        self._preview_font_picker = QComboBox(self)
        self._preview_font_picker.setEditable(False)
        self._preview_font_picker.addItem("(auto: system default)", "")
        # Preview face is unrestricted: every family is on offer.
        for fam in sorted(QFontDatabase.families()):
            self._preview_font_picker.addItem(fam, fam)
        saved_preview_family = config.ui.preview_font_family
        idx = self._preview_font_picker.findData(saved_preview_family)
        self._preview_font_picker.setCurrentIndex(idx if idx >= 0 else 0)
        preview_form.addRow("Font:", self._preview_font_picker)
        self._preview_font_size = QSpinBox(self)
        self._preview_font_size.setRange(0, 72)
        self._preview_font_size.setSpecialValueText("(default)")
        self._preview_font_size.setValue(config.ui.preview_font_size)
        self._preview_font_size.setSuffix(" pt")
        preview_form.addRow("Size:", self._preview_font_size)
        fonts_layout.addWidget(preview_group)

        # Markdown rich source view (#91). Sits under Fonts because it
        # changes how the editor renders, parallel to the font picker.
        self._markdown_rich_editor = QCheckBox(
            "Style markdown source in the editor", fonts_group,
        )
        self._markdown_rich_editor.setChecked(config.ui.markdown_rich_editor)
        self._markdown_rich_editor.setToolTip(
            "Render markdown structure cues in the My Notes editor: "
            "headings sized + bold, bold/italic shown through, code "
            "monospace, links underlined. The markdown syntax stays "
            "visible and editable. Turn off for a plain monospace "
            "editor."
        )
        fonts_layout.addWidget(self._markdown_rich_editor)

        self._add_section("Fonts", fonts_group)

        # Synthesis (combined Automation + Prompt Templates) ---------
        # Both sub-groups live on a single page so the user finds
        # everything LLM-related in one place. The sub-groups keep
        # their own QGroupBox so the visual subdivision survives.
        auto_group = QGroupBox("Automation", self)
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

        # Optional Claude project ID. When set, syntheses land in the
        # named project on Claude.ai rather than the default chat list.
        # Aaron's use case: stop flooding the default conversation list
        # with one chat per meeting; pin them all to a "Meeting Notes"
        # project he can browse later.
        proj_row = QHBoxLayout()
        proj_row.addWidget(QLabel("Claude project ID:", self))
        self._claude_project_id = QLineEdit(self)
        self._claude_project_id.setText(config.synthesis.claude_project_id)
        self._claude_project_id.setPlaceholderText(
            "Optional UUID (e.g. 019e5077-c745-7541-b2c8-08caeb0f3051)"
        )
        self._claude_project_id.setToolTip(
            "Optional. Paste the UUID portion of a Claude.ai project "
            "URL. When set, every Send-to-Claude opens that project "
            "(https://claude.ai/project/<id>) instead of /new, so "
            "synthesized meeting notes accumulate inside the project. "
            "Leave blank to land in the default chat list."
        )
        proj_row.addWidget(self._claude_project_id, 1)
        auto_layout.addLayout(proj_row)

        # #102 bug 6: user-tunable timeouts that flow through the
        # SynthesizeRequest into the Chrome extension. Defaults
        # match the extension's built-in values; raise either
        # field when slow / long LLM responses surface as a
        # 'clipboard read failed' message that doesn't actually
        # describe a permissions issue.
        timeouts_row = QHBoxLayout()
        timeouts_row.addWidget(QLabel("LLM response wait (min):", self))
        self._llm_response_timeout = QSpinBox(self)
        self._llm_response_timeout.setRange(1, 30)
        self._llm_response_timeout.setSuffix(" min")
        self._llm_response_timeout.setValue(
            max(1, int(config.synthesis.llm_response_timeout_seconds / 60)),
        )
        self._llm_response_timeout.setToolTip(
            "How long the Chrome extension waits for the LLM's "
            "response to finish streaming. Default 10 minutes. "
            "Raise this if your prompts routinely take longer and "
            "you see 'response didn't settle' timeouts."
        )
        timeouts_row.addWidget(self._llm_response_timeout)
        timeouts_row.addSpacing(20)
        timeouts_row.addWidget(QLabel("Clipboard read wait (sec):", self))
        self._clipboard_read = QSpinBox(self)
        self._clipboard_read.setRange(1, 30)
        self._clipboard_read.setSuffix(" s")
        self._clipboard_read.setValue(
            int(config.synthesis.clipboard_read_seconds),
        )
        self._clipboard_read.setToolTip(
            "How long the extension polls the clipboard after "
            "clicking the LLM's Copy button. Default 3 seconds. "
            "Raise this if long responses surface as a 'couldn't "
            "read the clipboard' error -- the Copy serialization "
            "can take longer than the read budget on huge outputs."
        )
        timeouts_row.addWidget(self._clipboard_read)
        timeouts_row.addStretch(1)
        auto_layout.addLayout(timeouts_row)

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

        self._refresh_automation_status()

        # Prompt Templates sub-group of Synthesis -----------------------
        prompts_group = QGroupBox("Prompt Templates", self)
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

        # Default template picker. Sessions that don't have a per-session
        # template override (set via the SessionView dropdown) use this
        # value. Blank entry means "use the bundled default.md".
        default_row = QHBoxLayout()
        default_row.addWidget(QLabel("Default template:", self))
        self._default_template_picker = QComboBox(self)
        self._default_template_picker.addItem(
            "default (bundled generic template)", "",
        )
        try:
            from ..utils import prompts as _prompts_mod  # noqa: PLC0415
            for tpl in _prompts_mod.list_templates():
                self._default_template_picker.addItem(tpl.display_name, tpl.name)
        except Exception:
            # If the prompts folder isn't readable for some reason, leave
            # the picker with just the bundled-default fallback entry.
            pass
        current_default = self._config.synthesis.default_template_name or ""
        idx = self._default_template_picker.findData(current_default)
        if idx >= 0:
            self._default_template_picker.setCurrentIndex(idx)
        self._default_template_picker.setToolTip(
            "Used by new sessions that haven't picked a template via the "
            "session view's Template dropdown."
        )
        default_row.addWidget(self._default_template_picker, 1)
        prompts_layout.addLayout(default_row)

        # Attendee details extraction toggle (issue #51 Phase 4).
        # Off by default -- the request appends a paragraph asking the
        # LLM to also extract per-attendee details into a JSON appendix
        # the app parses. Aaron's 2026-05-29 feedback: keep this opt-in
        # since Outlook calendar enrichment already covers the common
        # case and the appended ask adds prompt bulk.
        self._extract_attendees = QCheckBox(
            "Ask the LLM to also extract attendee details (title / "
            "company / email / phone) into a structured appendix",
            self,
        )
        self._extract_attendees.setChecked(
            self._config.synthesis.auto_extract_attendee_details
        )
        self._extract_attendees.setToolTip(
            "When ON, every synthesis prompt includes a paragraph "
            "asking the LLM to pull per-attendee details out of the "
            "meeting content and emit a final '## Attendee Details "
            "(auto-extracted)' JSON section. The app reads that and "
            "fills in any Contact fields you don't already have."
        )
        prompts_layout.addWidget(self._extract_attendees)

        prompts_row = QHBoxLayout()
        # Prompt editor (#89). Replaces the AppData-folder edit workflow
        # with an in-app editor that archives every save so reverts are
        # one click. Open Prompts Folder stays available as an escape
        # hatch for power users / external editors.
        self._edit_prompts_btn = QPushButton("Edit Prompts...", self)
        self._edit_prompts_btn.clicked.connect(self._open_prompt_editor)
        prompts_row.addWidget(self._edit_prompts_btn)
        self._open_prompts_btn = QPushButton("Open Prompts Folder", self)
        self._open_prompts_btn.clicked.connect(self._open_prompts_folder)
        prompts_row.addWidget(self._open_prompts_btn)
        prompts_row.addStretch(1)
        prompts_layout.addLayout(prompts_row)
        # Compose both sub-groups into a single "Synthesis" page so
        # they share the same nav entry. Automation sits above
        # Prompt Templates because users typically configure
        # automation once and then iterate on templates.
        synth_page = QWidget(self)
        synth_page_layout = QVBoxLayout(synth_page)
        synth_page_layout.setContentsMargins(0, 0, 0, 0)
        synth_page_layout.addWidget(auto_group)
        synth_page_layout.addWidget(prompts_group)
        self._add_section("Synthesis", synth_page)

        # Export group ----------------------------------------------------
        # MP4 quality preset for the per-session video exports + the
        # full-session ZIP export (#54). Medium is the new default;
        # users who want the v0.7.2-era file sizes pick High.
        export_group = QGroupBox("Export", self)
        export_form = QFormLayout(export_group)

        # Default export folder (v0.7.5). Empty == legacy fallback
        # (session dir for PDFs + recording / video, Documents for
        # full-session). When set, every Export dialog opens here.
        export_folder_row = QHBoxLayout()
        self._export_default_folder_edit = QLineEdit(self)
        self._export_default_folder_edit.setText(
            getattr(config.synthesis, "export_default_folder", "") or ""
        )
        self._export_default_folder_edit.setPlaceholderText(
            "(leave blank to use the session folder / Documents)"
        )
        self._export_default_folder_edit.setToolTip(
            "Default location for the Save As / Choose Folder dialog "
            "that appears when you export a recording, video, full "
            "session, or PDF. The dialog still lets you navigate "
            "elsewhere; this just sets the starting point. Leave "
            "blank to fall back to the session's own folder."
        )
        export_folder_row.addWidget(self._export_default_folder_edit, 1)
        export_folder_browse = QPushButton("Browse...", self)
        export_folder_browse.clicked.connect(
            self._on_export_default_folder_browse,
        )
        export_folder_row.addWidget(export_folder_browse)
        export_form.addRow("Default folder:", export_folder_row)

        export_blurb = QLabel(
            "Quality preset for MP4 outputs (highlights, recording, "
            "full-session export). Slideshow-style screenshot content "
            "compresses well at low / medium; raise to high for "
            "motion-heavy sessions or when archival fidelity matters.",
            self,
        )
        export_blurb.setWordWrap(True)
        export_form.addRow(export_blurb)
        self._video_quality_picker = QComboBox(self)
        self._video_quality_picker.addItem(
            "Low -- 600 kbps video, 64 kbps audio (smallest)", "low",
        )
        self._video_quality_picker.addItem(
            "Medium -- 1.5 Mbps video, 96 kbps audio (default)", "medium",
        )
        self._video_quality_picker.addItem(
            "High -- 2.5 Mbps video, 128 kbps audio (largest)", "high",
        )
        current_quality = (
            getattr(config.synthesis, "video_quality", "medium") or "medium"
        )
        idx = self._video_quality_picker.findData(current_quality)
        if idx >= 0:
            self._video_quality_picker.setCurrentIndex(idx)
        self._video_quality_picker.setToolTip(
            "Lower presets shrink the file dramatically -- a 1-hour "
            "meeting drops from ~1.2 GB at High to ~300 MB at Low. "
            "Every preset still produces an MP4 that plays in the "
            "default Windows Media Player."
        )
        export_form.addRow("Video quality:", self._video_quality_picker)

        # Full-session export packaging (#62). Off by default: write
        # an uncompressed folder so the user can drop it on OneDrive
        # / a shared drive without zip overhead. Toggle ON for the
        # traditional single-zip output.
        self._compress_full_export = QCheckBox(
            "Compress full-session export into a single .zip file",
            self,
        )
        self._compress_full_export.setChecked(
            getattr(
                config.synthesis, "compress_full_session_export", False,
            )
        )
        self._compress_full_export.setToolTip(
            "When ON, the full-session export bundles everything "
            "into a single .zip the user picks the filename for. "
            "When OFF (default), the export goes into a subfolder "
            "under the user-chosen parent directory -- handy for "
            "OneDrive / shared drives where unzipping isn't needed."
        )
        export_form.addRow(self._compress_full_export)

        # Appendix-inclusion defaults (#65/#66 followup). These set
        # which Appendix sub-sections come pre-checked in the
        # AppendixInclusionDialog that fires before every PDF /
        # Print / full-session export. Aaron's chosen defaults
        # surface the user-curated context surfaces (attendee
        # context + documents + links) and suppress the noisier
        # per-person field dump and topic-suggestion list.
        appendix_blurb = QLabel(
            "Default Appendix sections to include when exporting "
            "(PDF / Print / full-session). Individual exports can "
            "still override these via the pre-export prompt.",
            self,
        )
        appendix_blurb.setWordWrap(True)
        export_form.addRow(appendix_blurb)
        self._appendix_export_include = QCheckBox("Include Appendix", self)
        af = self._appendix_export_include.font()
        af.setBold(True)
        self._appendix_export_include.setFont(af)
        self._appendix_export_include.setChecked(
            getattr(config.synthesis, "appendix_export_include", True),
        )
        self._appendix_export_include.toggled.connect(
            self._on_appendix_export_master_toggled,
        )
        export_form.addRow(self._appendix_export_include)
        self._appendix_section_checkboxes: dict[str, QCheckBox] = {}
        # Each row: config field name, label, current default.
        for field_name, label, attr in (
            ("appendix_export_attendee_context",       "    Attendee Context",       "appendix_export_attendee_context"),
            ("appendix_export_attendee_details",       "    Attendee Details",       "appendix_export_attendee_details"),
            ("appendix_export_topics",                 "    Suggested Topics",       "appendix_export_topics"),
            ("appendix_export_referenced_attachments", "    Referenced Attachments", "appendix_export_referenced_attachments"),
            ("appendix_export_session_attachments",    "    Session Attachments",    "appendix_export_session_attachments"),
            ("appendix_export_links",                  "    Links",                  "appendix_export_links"),
        ):
            cb = QCheckBox(label, self)
            cb.setChecked(getattr(config.synthesis, attr, True))
            cb.setEnabled(self._appendix_export_include.isChecked())
            export_form.addRow(cb)
            self._appendix_section_checkboxes[field_name] = cb

        # Document outline transforms (#92).
        outline_blurb = QLabel(
            "<b>Document outline</b>", self,
        )
        outline_blurb.setTextFormat(Qt.TextFormat.RichText)
        export_form.addRow(outline_blurb)

        self._heading_numbering = QCheckBox(
            "Auto-number headings in preview and exports", self,
        )
        self._heading_numbering.setChecked(
            getattr(config.synthesis, "heading_numbering", False),
        )
        self._heading_numbering.setToolTip(
            "Prepends dotted-decimal numbers to every heading: "
            "first H1 becomes '1 Heading', nested H2 becomes '1.1 "
            "Sub-heading', etc. Applies at render and export time; "
            "the on-disk notes.md file is unchanged."
        )
        export_form.addRow(self._heading_numbering)

        self._toc_in_exports = QCheckBox(
            "Generate table of contents in exports", self,
        )
        self._toc_in_exports.setChecked(
            getattr(config.synthesis, "toc_in_exports", False),
        )
        self._toc_in_exports.setToolTip(
            "Adds an auto-generated '## Contents' section at the top "
            "of every export (PDF / Notion / Confluence). The Preview "
            "already has a sidebar table of contents, so no inline "
            "TOC is added there. PDF TOC entries link to anchors -- "
            "click-to-navigate depends on the PDF viewer."
        )
        export_form.addRow(self._toc_in_exports)

        # Sub-option: TOC max depth.
        toc_depth_row = QHBoxLayout()
        toc_depth_row.addWidget(QLabel("    Max depth:", self))
        self._toc_max_depth = QSpinBox(self)
        self._toc_max_depth.setRange(1, 6)
        self._toc_max_depth.setValue(
            getattr(config.synthesis, "toc_max_depth", 3),
        )
        self._toc_max_depth.setToolTip(
            "Heading levels to include in the TOC. 3 (H1-H3) is a "
            "reasonable default; deeper exports include sub-sub-"
            "sections at the cost of a longer TOC."
        )
        self._toc_max_depth.setEnabled(self._toc_in_exports.isChecked())
        self._toc_in_exports.toggled.connect(self._toc_max_depth.setEnabled)
        toc_depth_row.addWidget(self._toc_max_depth)
        toc_depth_row.addStretch(1)
        export_form.addRow(toc_depth_row)

        # #94: route PDF export through Word when available so the
        # PDF gets Word's native clickable TOC + sidebar bookmarks
        # without any post-processing. Disabled if win32com isn't
        # importable (non-Windows or Windows without pywin32) -- the
        # cross-platform Qt + pypdf path stays the default fallback.
        from ..utils.word_export import is_word_com_available  # noqa: PLC0415
        word_com_ok = is_word_com_available()
        self._use_word_for_pdf = QCheckBox(
            "Use Word for PDF export (Windows only)", self,
        )
        self._use_word_for_pdf.setChecked(
            getattr(config.synthesis, "use_word_for_pdf", False)
            and word_com_ok,
        )
        self._use_word_for_pdf.setEnabled(word_com_ok)
        if word_com_ok:
            self._use_word_for_pdf.setToolTip(
                "Render the PDF via Word's native PDF export instead "
                "of Qt's PDF backend. Produces a PDF with Word's "
                "native sidebar bookmarks and clickable table of "
                "contents -- no post-processing needed. Requires "
                "Word to be installed."
            )
        else:
            self._use_word_for_pdf.setToolTip(
                "Disabled because Word COM (pywin32 + installed "
                "Word) is unavailable on this host. The Qt PDF path "
                "still emits clickable TOC entries via the #94 post-"
                "process."
            )
        export_form.addRow(self._use_word_for_pdf)

        self._add_section("Export", export_group)

        # Backups group (#67) ---------------------------------------------
        backup_group = QGroupBox("Backups", self)
        backup_layout = QVBoxLayout(backup_group)
        backup_blurb = QLabel(
            "Creates a zip archive of the internal application folder "
            "(under %APPDATA%\MeetingNotetaker on Windows) with all "
            "meeting notes, synthesis, and application settings "
            "(speakers, addressbook, app config, etc.). ",
            self,
        )
        backup_blurb.setWordWrap(True)
        backup_layout.addWidget(backup_blurb)

        folder_row = QHBoxLayout()
        folder_label = QLabel("Backup folder:", self)
        folder_row.addWidget(folder_label)
        self._backup_folder_edit = QLineEdit(self)
        self._backup_folder_edit.setText(config.backup.folder or "")
        self._backup_folder_edit.setPlaceholderText(
            "(leave blank to disable scheduled backups)"
        )
        folder_row.addWidget(self._backup_folder_edit, 1)
        browse_btn = QPushButton("Browse...", self)
        browse_btn.clicked.connect(self._on_backup_folder_browse)
        folder_row.addWidget(browse_btn)
        backup_layout.addLayout(folder_row)

        sched_label = QLabel("Schedule:", self)
        backup_layout.addWidget(sched_label)
        self._backup_sched_manual = QRadioButton(
            "Manual only (Tools > Backup Now)", self,
        )
        self._backup_sched_on_close = QRadioButton(
            "On app close", self,
        )
        self._backup_sched_when_idle = QRadioButton(
            "When idle after configured time", self,
        )
        backup_layout.addWidget(self._backup_sched_manual)
        backup_layout.addWidget(self._backup_sched_on_close)
        backup_layout.addWidget(self._backup_sched_when_idle)
        sched = config.backup.schedule or "manual"
        if sched == "on_close":
            self._backup_sched_on_close.setChecked(True)
        elif sched == "when_idle":
            self._backup_sched_when_idle.setChecked(True)
        else:
            self._backup_sched_manual.setChecked(True)

        idle_form = QFormLayout()
        self._backup_idle_minutes = QSpinBox(self)
        self._backup_idle_minutes.setRange(1, 720)
        self._backup_idle_minutes.setSuffix(" min")
        self._backup_idle_minutes.setValue(
            int(config.backup.idle_after_minutes or 30)
        )
        self._backup_idle_minutes.setToolTip(
            "How long the app must sit without user input before the "
            "idle scheduler fires a snapshot. Resets on any mouse or "
            "keyboard activity."
        )
        idle_form.addRow("Idle after:", self._backup_idle_minutes)
        self._backup_idle_hour = QSpinBox(self)
        self._backup_idle_hour.setRange(0, 23)
        self._backup_idle_hour.setValue(
            int(config.backup.idle_after_hour or 19)
        )
        self._backup_idle_hour.setSuffix(":00 local")
        self._backup_idle_hour.setToolTip(
            "Earliest local hour the idle trigger may fire. Default "
            "19:00 (7pm) so the backup doesn't kick in during the "
            "workday."
        )
        idle_form.addRow("Only after:", self._backup_idle_hour)
        backup_layout.addLayout(idle_form)

        retention_form = QFormLayout()
        self._backup_retention_count = QSpinBox(self)
        self._backup_retention_count.setRange(0, 365)
        self._backup_retention_count.setValue(
            int(config.backup.retention_count or 7)
        )
        self._backup_retention_count.setSuffix(" snapshots")
        self._backup_retention_count.setToolTip(
            "Keep at most N most-recent snapshots. 0 disables this "
            "gate (rely on the days cutoff instead). Pruning happens "
            "silently after every snapshot."
        )
        retention_form.addRow(
            "Keep newest:", self._backup_retention_count,
        )
        self._backup_retention_days = QSpinBox(self)
        self._backup_retention_days.setRange(0, 3650)
        self._backup_retention_days.setValue(
            int(config.backup.retention_days or 30)
        )
        self._backup_retention_days.setSuffix(" days")
        self._backup_retention_days.setToolTip(
            "Drop snapshots older than D days. 0 disables this gate. "
            "Applied alongside the snapshot count -- a snapshot is "
            "kept only when it passes both gates."
        )
        retention_form.addRow(
            "Drop older than:", self._backup_retention_days,
        )
        backup_layout.addLayout(retention_form)

        backup_btn_row = QHBoxLayout()
        self._backup_now_btn = QPushButton("Backup Now...", self)
        self._backup_now_btn.setToolTip(
            "Snapshot the data dir into the configured destination "
            "right now. Settings changes since the last save are NOT "
            "included until you click OK first."
        )
        # Wired by MainApp via inject_backup_now_handler so the dialog
        # stays independent of MainApp imports.
        self._backup_now_handler: Optional[callable] = None
        self._backup_now_btn.clicked.connect(self._on_backup_now_clicked)
        backup_btn_row.addStretch(1)
        backup_btn_row.addWidget(self._backup_now_btn)
        backup_layout.addLayout(backup_btn_row)

        self._add_section("Backups", backup_group)

        # Integrations (#79) -------------------------------------------------
        # Notion + Confluence export targets. Both surface only when the
        # user provides credentials AND the Verify button has reported
        # success since the credentials last changed (verify writes
        # last_verified_at; the export menu reads this). Shipped as
        # "(Experimental)" in v0.7.6-dev; tag dropped on 2026-06-03 after
        # end-to-end verification on Cloud Notion + Cloud Confluence.
        integrations_page = self._build_integrations_section(config)
        self._add_section("Integrations", integrations_page)

        # Assemble nav + stack from the accumulated sections, sorted
        # alphabetically by label. Restores last-active section so the
        # user lands back where they were when they reopen Settings.
        self._assemble_sections(outer)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _on_accept(self) -> None:
        self._config.transcription.model_size = self._model_picker.currentText()
        self._config.transcription.capture_only_mode = self._capture_only.isChecked()
        self._config.transcription.skip_batch_refinement = self._skip_batch.isChecked()
        self._config.transcription.fast_batch = not self._high_accuracy.isChecked()
        self._config.transcription.cpu_threads = self._cpu_threads_spin.value()
        self._config.transcription.num_workers = self._num_workers_spin.value()
        self._config.audio.retain_audio_default = self._retain_default.isChecked()
        self._config.audio.retain_format = self._retain_format.currentData() or "opus"
        self._config.audio.vad_enabled = self._vad_enabled.isChecked()
        self._config.audio.vad_min_silence_ms = self._vad_slider.value()
        self._config.audio.mic_device_name = self._mic_picker.currentData() or ""
        self._config.audio.loopback_device_name = self._loopback_picker.currentData() or ""
        self._config.audio.multi_endpoint_capture = self._multi_endpoint.isChecked()
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
        self._config.ui.screen_capture_auto_interval_sec = int(
            self._screencap_auto_interval.value()
        )
        self._config.ui.screen_capture_auto_dedup_threshold = int(
            self._screencap_dedup_threshold.value()
        )
        # Font preferences. Empty data string = auto sentinel.
        self._config.ui.editor_font_family = (
            self._editor_font_picker.currentData() or ""
        )
        self._config.ui.editor_font_size = int(self._editor_font_size.value())
        self._config.ui.preview_font_family = (
            self._preview_font_picker.currentData() or ""
        )
        self._config.ui.preview_font_size = int(self._preview_font_size.value())
        self._config.ui.markdown_rich_editor = self._markdown_rich_editor.isChecked()
        self._config.synthesis.automation_enabled = self._auto_enabled.isChecked()
        self._config.synthesis.llm_target = (
            self._auto_target.currentData() or "claude"
        )
        self._config.synthesis.claude_project_id = (
            self._claude_project_id.text().strip()
        )
        self._config.synthesis.llm_response_timeout_seconds = (
            int(self._llm_response_timeout.value()) * 60
        )
        self._config.synthesis.clipboard_read_seconds = (
            int(self._clipboard_read.value())
        )
        self._config.synthesis.auto_extract_attendee_details = (
            self._extract_attendees.isChecked()
        )
        self._config.synthesis.default_template_name = (
            self._default_template_picker.currentData() or ""
        )
        self._config.synthesis.video_quality = (
            self._video_quality_picker.currentData() or "medium"
        )
        self._config.synthesis.export_default_folder = (
            self._export_default_folder_edit.text().strip()
        )
        self._config.synthesis.compress_full_session_export = (
            self._compress_full_export.isChecked()
        )
        self._config.synthesis.appendix_export_include = (
            self._appendix_export_include.isChecked()
        )
        for field, cb in self._appendix_section_checkboxes.items():
            setattr(self._config.synthesis, field, cb.isChecked())
        self._config.synthesis.heading_numbering = self._heading_numbering.isChecked()
        self._config.synthesis.toc_in_exports = self._toc_in_exports.isChecked()
        self._config.synthesis.toc_max_depth = int(self._toc_max_depth.value())
        self._config.synthesis.use_word_for_pdf = (
            self._use_word_for_pdf.isChecked()
        )
        self._config.backup.folder = self._backup_folder_edit.text().strip()
        if self._backup_sched_on_close.isChecked():
            self._config.backup.schedule = "on_close"
        elif self._backup_sched_when_idle.isChecked():
            self._config.backup.schedule = "when_idle"
        else:
            self._config.backup.schedule = "manual"
        self._config.backup.idle_after_minutes = int(
            self._backup_idle_minutes.value()
        )
        self._config.backup.idle_after_hour = int(
            self._backup_idle_hour.value()
        )
        self._config.backup.retention_count = int(
            self._backup_retention_count.value()
        )
        self._config.backup.retention_days = int(
            self._backup_retention_days.value()
        )
        # Issue #79 -- Notion + Confluence integrations. Token edits
        # persist whether or not Verify ran (the user can save partial
        # state and verify later). Any change to the credential fields
        # clears the last_verified_at stamp so a stale "Connected"
        # label doesn't outlive the credentials it described.
        new_notion_token = self._notion_token_edit.text().strip()
        if new_notion_token != self._config.notion.api_token:
            self._config.notion.api_token = new_notion_token
            self._config.notion.last_verified_at = ""
        new_cf_base = self._confluence_base_url_edit.text().strip()
        new_cf_email = self._confluence_email_edit.text().strip()
        new_cf_token = self._confluence_token_edit.text().strip()
        cf_changed = (
            new_cf_base != self._config.confluence.base_url
            or new_cf_email != self._config.confluence.email
            or new_cf_token != self._config.confluence.api_token
        )
        if cf_changed:
            self._config.confluence.base_url = new_cf_base
            self._config.confluence.email = new_cf_email
            self._config.confluence.api_token = new_cf_token
            self._config.confluence.last_verified_at = ""
        # Issue #96 -- Obsidian. Vault path changes clear last_verified_at
        # the same way the Notion/Confluence credential edits do.
        new_vault_root = self._obsidian_vault_edit.text().strip()
        if new_vault_root != self._config.obsidian.vault_root:
            self._config.obsidian.vault_root = new_vault_root
            self._config.obsidian.last_verified_at = ""
            self._config.obsidian.vault_name = ""
        self._config.obsidian.location_template_name = (
            self._obsidian_location_picker.currentData()
            or "year_month"
        )
        self._config.obsidian.location_template_custom = (
            self._obsidian_custom_template.text().strip()
        )
        self._config.obsidian.write_frontmatter = (
            self._obsidian_write_fm.isChecked()
        )
        self._config.obsidian.wikilink_attendees = (
            self._obsidian_wikilink_att.isChecked()
        )
        self._config.obsidian.wikilink_series = (
            self._obsidian_wikilink_series.isChecked()
        )
        self._config.obsidian.include_classification = (
            self._obsidian_include_class.isChecked()
        )
        self._config.obsidian.default_include_attachments = (
            self._obsidian_default_attach.isChecked()
        )
        self._config.obsidian.daily_note_backlink = (
            self._obsidian_daily_backlink.isChecked()
        )
        self._config.obsidian.open_after_save = (
            self._obsidian_open_after.isChecked()
        )
        # Remember which section the user was on so reopening Settings
        # lands them right back. Stored in ui.settings_active_section.
        self._config.ui.settings_active_section = self._active_section_label()
        self.accept()

    # ---- integrations (#79) -------------------------------------------

    def _build_integrations_section(self, config: Config) -> QWidget:
        """Build the Notion + Confluence credential rows.

        Opt-in: users not in the Notion / Confluence path see no
        export options that don't apply to them. Both targets verify
        their credentials before the Save to menu surfaces them.
        """
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)

        # Header explainer.
        header = QLabel(
            "Save the active My Notes or Synthesis tab to Notion or "
            "Confluence as a new page under a parent you pick. Tokens "
            "stay local in your settings.toml. The Save to menu surfaces "
            "these destinations only after Verify succeeds.",
            self,
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        # ---- Notion --------------------------------------------------------
        notion_group = QGroupBox("Notion", self)
        notion_form = QFormLayout(notion_group)

        self._notion_token_edit = QLineEdit(self)
        self._notion_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._notion_token_edit.setText(config.notion.api_token)
        self._notion_token_edit.setPlaceholderText("secret_XXXXXXXXXXXX")
        self._notion_token_edit.setToolTip(
            "Create an internal integration at notion.so/my-integrations, "
            "copy the Internal Integration Token here, and share each "
            "destination page with that integration."
        )
        # "Show" toggle so the user can confirm what they pasted.
        notion_token_row = QHBoxLayout()
        notion_token_row.addWidget(self._notion_token_edit, 1)
        self._notion_show_token = QPushButton("Show", self)
        self._notion_show_token.setCheckable(True)
        self._notion_show_token.setMaximumWidth(60)
        self._notion_show_token.toggled.connect(
            lambda checked: self._notion_token_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )
        notion_token_row.addWidget(self._notion_show_token)
        notion_form.addRow("Integration token:", notion_token_row)

        notion_verify_row = QHBoxLayout()
        self._notion_verify_btn = QPushButton("Verify connection", self)
        self._notion_verify_btn.clicked.connect(self._on_verify_notion)
        notion_verify_row.addWidget(self._notion_verify_btn)
        self._notion_status_label = QLabel(self)
        self._notion_status_label.setWordWrap(True)
        notion_verify_row.addWidget(self._notion_status_label, 1)
        notion_form.addRow(notion_verify_row)
        self._refresh_notion_status_label()
        layout.addWidget(notion_group)

        # ---- Confluence ---------------------------------------------------
        confluence_group = QGroupBox("Confluence", self)
        confluence_form = QFormLayout(confluence_group)

        self._confluence_base_url_edit = QLineEdit(self)
        self._confluence_base_url_edit.setText(config.confluence.base_url)
        self._confluence_base_url_edit.setPlaceholderText(
            "https://your-org.atlassian.net/wiki"
        )
        confluence_form.addRow("Base URL:", self._confluence_base_url_edit)

        self._confluence_email_edit = QLineEdit(self)
        self._confluence_email_edit.setText(config.confluence.email)
        self._confluence_email_edit.setPlaceholderText("you@example.com")
        confluence_form.addRow("Email:", self._confluence_email_edit)

        self._confluence_token_edit = QLineEdit(self)
        self._confluence_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._confluence_token_edit.setText(config.confluence.api_token)
        self._confluence_token_edit.setPlaceholderText("Atlassian API token")
        self._confluence_token_edit.setToolTip(
            "Generate at id.atlassian.com -> Security -> API tokens. "
            "Confluence Cloud authenticates as email + token via Basic auth."
        )
        confluence_token_row = QHBoxLayout()
        confluence_token_row.addWidget(self._confluence_token_edit, 1)
        self._confluence_show_token = QPushButton("Show", self)
        self._confluence_show_token.setCheckable(True)
        self._confluence_show_token.setMaximumWidth(60)
        self._confluence_show_token.toggled.connect(
            lambda checked: self._confluence_token_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )
        confluence_token_row.addWidget(self._confluence_show_token)
        confluence_form.addRow("API token:", confluence_token_row)

        confluence_verify_row = QHBoxLayout()
        self._confluence_verify_btn = QPushButton("Verify connection", self)
        self._confluence_verify_btn.clicked.connect(self._on_verify_confluence)
        confluence_verify_row.addWidget(self._confluence_verify_btn)
        self._confluence_status_label = QLabel(self)
        self._confluence_status_label.setWordWrap(True)
        confluence_verify_row.addWidget(self._confluence_status_label, 1)
        confluence_form.addRow(confluence_verify_row)
        self._refresh_confluence_status_label()
        layout.addWidget(confluence_group)

        # ---- Obsidian (#96) -----------------------------------------------
        obsidian_group = QGroupBox("Obsidian", self)
        obsidian_form = QFormLayout(obsidian_group)

        vault_row = QHBoxLayout()
        self._obsidian_vault_edit = QLineEdit(self)
        self._obsidian_vault_edit.setText(config.obsidian.vault_root)
        self._obsidian_vault_edit.setPlaceholderText(
            "Path to your Obsidian vault folder"
        )
        vault_row.addWidget(self._obsidian_vault_edit, 1)
        obsidian_browse_btn = QPushButton("Browse...", self)
        obsidian_browse_btn.clicked.connect(self._on_browse_obsidian_vault)
        vault_row.addWidget(obsidian_browse_btn)
        obsidian_form.addRow("Vault path:", vault_row)

        obsidian_verify_row = QHBoxLayout()
        self._obsidian_verify_btn = QPushButton("Verify vault", self)
        self._obsidian_verify_btn.clicked.connect(self._on_verify_obsidian)
        obsidian_verify_row.addWidget(self._obsidian_verify_btn)
        self._obsidian_status_label = QLabel(self)
        self._obsidian_status_label.setWordWrap(True)
        obsidian_verify_row.addWidget(self._obsidian_status_label, 1)
        obsidian_form.addRow(obsidian_verify_row)

        self._obsidian_location_picker = QComboBox(self)
        for name, label in (
            ("year_month", "Year / Month -- Meetings/{YYYY}/{MM}"),
            ("by_series", "By series -- Meetings/{series}"),
            (
                "by_series_dated",
                "By series + date -- Meetings/{series}/{YYYY}-{MM}-{DD} - {title}",
            ),
            ("flat", "Flat -- Meetings/"),
            ("custom", "Custom..."),
        ):
            self._obsidian_location_picker.addItem(label, name)
        current_idx = [
            i for i in range(self._obsidian_location_picker.count())
            if self._obsidian_location_picker.itemData(i)
            == config.obsidian.location_template_name
        ]
        if current_idx:
            self._obsidian_location_picker.setCurrentIndex(current_idx[0])
        obsidian_form.addRow("Note location:", self._obsidian_location_picker)

        self._obsidian_custom_template = QLineEdit(self)
        self._obsidian_custom_template.setText(
            config.obsidian.location_template_custom,
        )
        self._obsidian_custom_template.setPlaceholderText(
            "e.g. Meetings/{series}/{YYYY}-{MM}-{DD} - {title}"
        )
        obsidian_form.addRow("Custom template:", self._obsidian_custom_template)
        self._obsidian_location_picker.currentIndexChanged.connect(
            self._refresh_obsidian_custom_enabled
        )
        self._refresh_obsidian_custom_enabled()

        self._obsidian_write_fm = QCheckBox(
            "Write YAML frontmatter (date, attendees, series, tags)"
        )
        self._obsidian_write_fm.setChecked(config.obsidian.write_frontmatter)
        obsidian_form.addRow("", self._obsidian_write_fm)

        self._obsidian_wikilink_att = QCheckBox(
            "Use [[wikilinks]] for attendee names in frontmatter"
        )
        self._obsidian_wikilink_att.setChecked(
            config.obsidian.wikilink_attendees,
        )
        obsidian_form.addRow("", self._obsidian_wikilink_att)

        self._obsidian_wikilink_series = QCheckBox(
            "Use [[wikilink]] for the series in frontmatter"
        )
        self._obsidian_wikilink_series.setChecked(
            config.obsidian.wikilink_series,
        )
        obsidian_form.addRow("", self._obsidian_wikilink_series)

        self._obsidian_include_class = QCheckBox(
            "Include classification in frontmatter"
        )
        self._obsidian_include_class.setChecked(
            config.obsidian.include_classification,
        )
        obsidian_form.addRow("", self._obsidian_include_class)

        self._obsidian_default_attach = QCheckBox(
            "Include session attachments by default on save"
        )
        self._obsidian_default_attach.setChecked(
            config.obsidian.default_include_attachments,
        )
        obsidian_form.addRow("", self._obsidian_default_attach)

        self._obsidian_daily_backlink = QCheckBox(
            "Append a backlink to today's daily note on save"
        )
        self._obsidian_daily_backlink.setChecked(
            config.obsidian.daily_note_backlink,
        )
        obsidian_form.addRow("", self._obsidian_daily_backlink)

        self._obsidian_open_after = QCheckBox(
            "Open the note in Obsidian after save"
        )
        self._obsidian_open_after.setChecked(config.obsidian.open_after_save)
        obsidian_form.addRow("", self._obsidian_open_after)

        self._refresh_obsidian_status_label()
        layout.addWidget(obsidian_group)

        layout.addStretch(1)
        return page

    def _refresh_notion_status_label(self) -> None:
        when = self._config.notion.last_verified_at
        if not self._notion_token_edit.text().strip():
            self._notion_status_label.setText("Not configured.")
        elif when:
            self._notion_status_label.setText(f"Connected (verified {when}).")
        else:
            self._notion_status_label.setText(
                "Token entered. Click Verify to confirm + enable export."
            )

    def _refresh_confluence_status_label(self) -> None:
        when = self._config.confluence.last_verified_at
        configured = (
            self._confluence_base_url_edit.text().strip()
            and self._confluence_email_edit.text().strip()
            and self._confluence_token_edit.text().strip()
        )
        if not configured:
            self._confluence_status_label.setText("Not configured.")
        elif when:
            self._confluence_status_label.setText(f"Connected (verified {when}).")
        else:
            self._confluence_status_label.setText(
                "Credentials entered. Click Verify to confirm + enable export."
            )

    def _on_verify_notion(self) -> None:
        from ..integrations.notion_api import NotionAPIError, NotionClient  # noqa: PLC0415
        from datetime import datetime as _dt  # noqa: PLC0415

        token = self._notion_token_edit.text().strip()
        if not token:
            self._notion_status_label.setText("Enter a token first.")
            return
        self._notion_status_label.setText("Verifying...")
        self._notion_verify_btn.setEnabled(False)
        try:
            user = NotionClient(token).verify()
        except NotionAPIError as exc:
            self._notion_status_label.setText(
                f"Failed ({exc.status}): check the token + that the integration is shared with at least one page."
            )
            return
        except Exception as exc:
            self._notion_status_label.setText(f"Failed: {exc}")
            return
        finally:
            self._notion_verify_btn.setEnabled(True)
        name = user.get("name") or user.get("bot", {}).get("owner", {}).get("user", {}).get("name") or "(unnamed)"
        when = _dt.now().strftime("%Y-%m-%dT%H:%M:%S")
        self._config.notion.api_token = token  # store immediately so future Verify reuses
        self._config.notion.last_verified_at = when
        self._notion_status_label.setText(f"Connected as {name} (verified {when}).")

    def _on_verify_confluence(self) -> None:
        from ..integrations.confluence_api import (  # noqa: PLC0415
            ConfluenceAPIError,
            ConfluenceClient,
        )
        from datetime import datetime as _dt  # noqa: PLC0415

        base_url = self._confluence_base_url_edit.text().strip()
        email = self._confluence_email_edit.text().strip()
        token = self._confluence_token_edit.text().strip()
        if not (base_url and email and token):
            self._confluence_status_label.setText(
                "Provide base URL, email, and token first."
            )
            return
        self._confluence_status_label.setText("Verifying...")
        self._confluence_verify_btn.setEnabled(False)
        try:
            user = ConfluenceClient(base_url, email, token).verify()
        except ConfluenceAPIError as exc:
            hint = ""
            if exc.status == 404:
                hint = (
                    " (404 typically means the base URL is wrong -- "
                    "Cloud expects https://your-org.atlassian.net/wiki)"
                )
            elif exc.status == 401:
                hint = " (401 typically means the email or API token is wrong)"
            self._confluence_status_label.setText(
                f"Failed ({exc.status}){hint}: check the base URL, email, and token."
            )
            return
        except Exception as exc:
            self._confluence_status_label.setText(f"Failed: {exc}")
            return
        finally:
            self._confluence_verify_btn.setEnabled(True)
        name = (
            user.get("displayName")
            or user.get("publicName")
            or user.get("accountId")
            or "(unknown user)"
        )
        when = _dt.now().strftime("%Y-%m-%dT%H:%M:%S")
        self._config.confluence.base_url = base_url
        self._config.confluence.email = email
        self._config.confluence.api_token = token
        self._config.confluence.last_verified_at = when
        self._confluence_status_label.setText(f"Connected as {name} (verified {when}).")

    # ---- Obsidian (#96) ----------------------------------------------

    def _on_browse_obsidian_vault(self) -> None:
        start = self._obsidian_vault_edit.text().strip() or str(
            Path.home()
        )
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose Obsidian vault", start,
        )
        if chosen:
            # Normalize separators (Qt returns forward slashes on
            # Windows) so the canonical form matches what Path stores
            # at Verify time and Accept doesn't see a spurious diff
            # that wipes last_verified_at.
            self._obsidian_vault_edit.setText(str(Path(chosen)))
            self._config.obsidian.last_verified_at = ""
            self._refresh_obsidian_status_label()

    def _on_verify_obsidian(self) -> None:
        from ..integrations.obsidian_vault import (  # noqa: PLC0415
            is_vault_registered,
            vault_is_valid,
            vault_name_for_path,
        )
        from datetime import datetime as _dt  # noqa: PLC0415

        raw = self._obsidian_vault_edit.text().strip()
        if not raw:
            self._obsidian_status_label.setText("Enter a vault path first.")
            return
        vault_root = Path(raw).expanduser()
        if not vault_is_valid(vault_root):
            self._obsidian_status_label.setText(
                "Folder is not readable / writable, or does not exist."
            )
            return
        vault_name = vault_name_for_path(vault_root)
        registered = is_vault_registered(vault_root)
        when = _dt.now().strftime("%Y-%m-%dT%H:%M:%S")
        canonical = str(vault_root)
        # Sync the line edit to the canonical form so the next pass
        # through _on_accept's "did the user edit the field?" check
        # compares apples to apples. Without this, raw user input
        # like "~/Vaults/X" or a path with forward slashes survives
        # in the widget while the resolved form lands in _config,
        # the compare fails, and last_verified_at gets wiped.
        self._obsidian_vault_edit.setText(canonical)
        self._config.obsidian.vault_root = canonical
        self._config.obsidian.vault_name = vault_name
        self._config.obsidian.last_verified_at = when
        if registered:
            self._obsidian_status_label.setText(
                f"Vault '{vault_name}' verified (registered with Obsidian)."
            )
        else:
            self._obsidian_status_label.setText(
                f"Vault folder OK -- not yet registered with Obsidian. "
                f"Open it once in Obsidian to enable the 'open after save' URI."
            )

    def _refresh_obsidian_status_label(self) -> None:
        if not self._obsidian_vault_edit.text().strip():
            self._obsidian_status_label.setText("Not configured.")
            return
        when = self._config.obsidian.last_verified_at
        if when:
            self._obsidian_status_label.setText(
                f"Vault verified ({when})."
            )
        else:
            self._obsidian_status_label.setText(
                "Vault path entered. Click Verify vault to confirm + enable save."
            )

    def _refresh_obsidian_custom_enabled(self) -> None:
        is_custom = self._obsidian_location_picker.currentData() == "custom"
        self._obsidian_custom_template.setEnabled(is_custom)

    def inject_backup_now_handler(self, handler) -> None:
        """Caller (MainApp) wires the manual-backup action here so the
        Backup Now button in the Backups group can fire without the
        dialog needing a back-pointer to MainApp."""
        self._backup_now_handler = handler

    def _on_backup_folder_browse(self) -> None:
        start = self._backup_folder_edit.text().strip() or ""
        picked = QFileDialog.getExistingDirectory(
            self, "Pick backup destination folder", start,
        )
        if picked:
            self._backup_folder_edit.setText(picked)

    def _on_export_default_folder_browse(self) -> None:
        start = self._export_default_folder_edit.text().strip() or ""
        picked = QFileDialog.getExistingDirectory(
            self, "Pick default export folder", start,
        )
        if picked:
            self._export_default_folder_edit.setText(picked)

    def _on_backup_now_clicked(self) -> None:
        handler = self._backup_now_handler
        if handler is None:
            return
        handler()

    # ---- section nav (v0.7.5 redesign) -------------------------------

    def _add_section(self, label: str, content: QWidget) -> None:
        """Register a section page. Called from __init__ as each
        group's widgets finish building. ``content`` is whatever the
        section's body is -- a QGroupBox in most cases, or a composite
        QWidget (e.g. Synthesis combining Automation + Prompt Templates)."""
        self._sections.append((label, content))

    def _assemble_sections(self, outer: QVBoxLayout) -> None:
        """Build the nav + stack from ``self._sections`` and attach
        them to ``outer``. Called at the bottom of __init__.

        Layout:
          - QSplitter (horizontal, user-resizable)
            - QListWidget  (nav, alphabetized)
            - QStackedWidget (content, one page per section)
          - QDialogButtonBox (added by caller after this method)

        Per-page scroll: each section gets wrapped in a QScrollArea so
        tall sections (Speakers, Backups, Synthesis) stay usable on
        small screens without forcing the whole dialog to scroll.
        """
        sections = sorted(self._sections, key=lambda s: s[0].lower())

        self._nav = QListWidget(self)
        self._nav.setFrameShape(QFrame.Shape.NoFrame)
        self._nav.setMaximumWidth(200)
        self._nav.setMinimumWidth(150)
        # Slight padding around each row so the labels breathe.
        self._nav.setStyleSheet(
            "QListWidget::item { padding: 6px 8px; }"
        )

        self._stack = QStackedWidget(self)

        for label, content in sections:
            page = QWidget(self)
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(8, 8, 8, 8)
            scroll = QScrollArea(page)
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            # Horizontal scroll off entirely -- the dialog's default
            # width is sized to fit the widest section's contents, so
            # the only reason to scroll is vertical overflow.
            scroll.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
            )
            inner = QWidget()
            inner_layout = QVBoxLayout(inner)
            inner_layout.setContentsMargins(0, 0, 0, 0)
            inner_layout.addWidget(content)
            inner_layout.addStretch(1)
            scroll.setWidget(inner)
            page_layout.addWidget(scroll)
            self._stack.addWidget(page)
            self._nav.addItem(label)

        # Click-to-switch wiring.
        self._nav.currentRowChanged.connect(self._stack.setCurrentIndex)

        # Restore the section the user was on last time Settings was
        # open. Falls back to the first entry (alphabetical = "Audio")
        # on first launch.
        target_label = self._config.ui.settings_active_section or ""
        target_row = 0
        for idx, (label, _) in enumerate(sections):
            if label == target_label:
                target_row = idx
                break
        self._nav.setCurrentRow(target_row)

        # Splitter so power users can widen the nav for long labels.
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(self._nav)
        splitter.addWidget(self._stack)
        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([180, 720])
        outer.addWidget(splitter, 1)

    def _active_section_label(self) -> str:
        """Return the label of the currently-selected nav entry, or
        empty string if nothing is selected (shouldn't happen in
        practice -- the assembly step always selects row 0)."""
        row = self._nav.currentRow()
        if row < 0:
            return ""
        item = self._nav.item(row)
        return item.text() if item is not None else ""

    def _on_appendix_export_master_toggled(self, on: bool) -> None:
        """Enable / disable the per-section checkboxes alongside the
        master toggle so unchecking "Include Appendix" visibly
        greys out every section."""
        for cb in self._appendix_section_checkboxes.values():
            cb.setEnabled(on)

    def _open_prompts_folder(self) -> None:
        path = prompts_dir()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _open_prompt_editor(self) -> None:
        """Open the in-app prompt editor (#89). On close, repopulate
        the default-template dropdown so any new / deleted prompts
        surface immediately without reopening Settings."""
        from .prompt_editor_dialog import PromptEditorDialog  # noqa: PLC0415

        dlg = PromptEditorDialog(parent=self)
        dlg.exec()
        self._refresh_default_template_picker()
        # The parent is the MainWindow; MainApp's _on_edit_prompts
        # owns the SessionView refresh when the editor is opened from
        # the Tools menu. Settings opens the editor inline; we let the
        # next New Session / session-switch tick refresh the in-meeting
        # picker. A future enhancement could re-emit a signal here to
        # force the refresh immediately; keeping the surface narrow
        # for now.

    def _refresh_default_template_picker(self) -> None:
        """Repopulate the default-template dropdown from the current
        prompt list, preserving the selection when possible. Called
        after the prompt editor closes so newly-created prompts show
        up and deleted ones drop out without reopening Settings."""
        from ..utils import prompts as _prompts_mod  # noqa: PLC0415

        current = self._default_template_picker.currentData() or ""
        self._default_template_picker.blockSignals(True)
        self._default_template_picker.clear()
        for tpl in _prompts_mod.list_templates():
            self._default_template_picker.addItem(tpl.display_name, tpl.name)
        if current:
            idx = self._default_template_picker.findData(current)
            if idx >= 0:
                self._default_template_picker.setCurrentIndex(idx)
        self._default_template_picker.blockSignals(False)

    def _open_vocabulary_file(self) -> None:
        path = seed_vocabulary_file()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _open_manage_speakers(self) -> None:
        store = open_speaker_store()
        try:
            dialog = SpeakersManageDialog(
                store, parent=self,
                classification_store=self._classification_store,
            )
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
