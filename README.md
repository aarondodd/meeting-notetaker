# Meeting Notetaker

Granola-style local meeting capture for Windows. Records your microphone and
the system audio (whatever's playing through your speakers, including Teams /
Zoom / Meet calls), transcribes both streams locally with faster-whisper, and
hands the resulting transcript to you for synthesis by a company-approved
chatbot via clipboard. No audio leaves the machine; no API key required.

```
+----------------+      mic.wav      +-----------------+
|   Microphone   | ----------------> |                 |
+----------------+                   |  Local Whisper  |  --> raw.transcript.md
+----------------+      sys.wav      |  (faster-whisper|
| System audio   | ----------------> |   on CPU, int8) |  --> Generate Prompt
| (WASAPI loop.) |                   |                 |       |
+----------------+                   +-----------------+       v
                                                          [Clipboard]
                                                               |
                                                               v
                                                   Claude.ai / Copilot
                                                               |
                                                               v
                                                       Paste Response Back
                                                               |
                                                               v
                                                          notes.md
```

## Why this exists

The work environment forbids:

- Sending audio or transcript content out to any external API.
- Synthesising notes via any LLM other than a company-approved chatbot.

Granola, Otter, Fireflies, Meetily-with-API-LLM, and similar SaaS tools all
violate one or both. This app moves transcription on-device and keeps the
synthesis step manual: you paste the prompt + transcript into Claude.ai or
Copilot, and paste the response back. Aaron stays in the loop as the explicit
transport.

## Status

v0.1 alpha. Built on Linux, runs on Windows. Not yet tested against a real
meeting end-to-end; the WASAPI loopback path is cribbed from
[Danaor/WhisperType](https://github.com/Danaor/WhisperType) and works there.

## Requirements

- Windows 10 / 11 (the WASAPI loopback capture is Windows-only).
- Python 3.10+ (3.11+ recommended).
- A working microphone and a default speaker / output device.
- ~500 MB of disk for the default `small.en` Whisper model. The model
  downloads on first run.

The dev environment also installs PyAudio's PortAudio binding, which builds
against PortAudio. On Windows the pip wheel ships PortAudio binaries; no
extra system install is needed.

## Install (dev)

```powershell
# Windows PowerShell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
python main.py
```

```bash
# Linux / macOS dev environment (UI runs; loopback capture stays disabled)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python main.py
```

The non-Windows path is for development only -- it works for everything
except real system-audio capture, since `pyaudiowpatch` only ships Windows
wheels. The mic recorder works cross-platform.

## Run (packaged)

```powershell
.\build.ps1                 # produces dist\meeting-notetaker.exe
.\dist\meeting-notetaker.exe
```

The executable bundles the Python runtime, PyQt6, and faster-whisper. The
Whisper model itself is downloaded on first run into
`%APPDATA%\MeetingNotetaker\models\`.

## Usage (golden path)

1. Launch the app. A blue dot appears in the system tray; the main window
   shows an empty session list.
2. **File -> New Session...** (or `Ctrl+N`). Enter a title (e.g. "1:1 with
   Manager"). Optionally tick "Keep the audio recording after transcription"
   if you want this specific session to retain its WAV files.
3. Select the new session in the list. Click **Start**.
4. The tray icon turns red and pulses. The transcript pane fills with lines
   labeled `Me:` (your mic) and `Them:` (system audio), interleaved by time.
5. **Pause** / **Resume** at any time. The recorder drops buffers during
   pause without writing them to the WAV.
6. **Stop** when the meeting ends. The tray shows a purple "processing" dot
   while the final transcription pass runs. When it finishes the tray
   returns to blue and the transcript view shows the cleaned-up final
   transcript.
7. Click **Generate Synthesis Prompt**. Pick a template (default, one-on-one,
   standup, or any you added). The rendered prompt + transcript is copied to
   your clipboard.
8. Switch to Claude.ai or Copilot, paste, send.
9. Copy the response, come back to Meeting Notetaker, click **Paste
   Response Back...**, paste, save. The Notes tab now shows the rendered
   response. If a prior notes file existed it is auto-archived as
   `notes-YYYYMMDD-HHMM.md` and shown under the Previous Notes tab.

If the app crashes mid-meeting, on next launch the recovery dialog lists any
sessions left in a transient state. The WAV files survive; you can re-run
the transcription against them, or delete the session.

## Where things live

```
%APPDATA%\MeetingNotetaker\
  sessions.db                    # SQLite session + folder metadata (WAL)
  config.toml                    # user settings
  instance.lock                  # single-instance pidfile
  meeting_notetaker.log          # rotating app log
  models\                        # faster-whisper model cache
  prompts\                       # user-editable synthesis prompts (auto-seeded)
  sessions\
    <session-uuid>\
      raw.transcript.md          # interleaved transcript, source-labeled
      notes.md                   # latest LLM-generated notes
      notes-YYYYMMDD-HHMM.md     # archived prior notes (if any)
      metadata.json
      audio\                     # mic.wav + sys.wav (deleted after transcription
                                 # unless 'Keep audio' was set for the session)
```

On Linux/macOS dev installs the equivalent root is
`$XDG_CONFIG_HOME/MeetingNotetaker/` (defaults to `~/.config/MeetingNotetaker/`).
Set `MEETING_NOTETAKER_DATA_DIR` to override anywhere.

## Settings

Open via **File -> Settings...** (`Ctrl+,`) or the tray menu. All of these
are also editable directly in `config.toml`.

| Setting | Default | What it does |
|---|---|---|
| Model size | `small.en` | Which faster-whisper model to use. See "Choosing a model" below. |
| Capture-only mode | off | Skip live transcription; run a single Whisper pass when you click Stop. Lower CPU during the meeting, no live view. |
| Retain audio (default) | off | Default state of the "Keep recording" checkbox for new sessions. Per-session override stays available. |
| Enable VAD | on | Trim silent stretches before Whisper decodes them. Saves CPU. Disable if it ever clips speech. |
| VAD min silence (ms) | 500 | How quiet a stretch has to be (in ms) before VAD treats it as silence. 250-2000 ms range. |
| Theme | auto | UI theme. (Stub for v0.1; `auto` follows the OS.) |

### Choosing a model

| Size | Disk | Quality | Notes |
|---|---|---|---|
| `tiny.en` | ~75 MB | Low | Useful only if your CPU is very slow or you just need keyword-level signal. |
| `base.en` | ~145 MB | OK | A reasonable smaller default. |
| `small.en` | ~480 MB | Good | Recommended for real meetings. |
| `medium.en` | ~1.5 GB | Best | ~3x slower than small.en. Use for high-stakes recordings if you have CPU headroom. |

Switching models reloads at start of the next recording. Already-recorded
sessions are not re-transcribed automatically.

### Adding or editing synthesis prompts

Bundled prompts seed `%APPDATA%\MeetingNotetaker\prompts\` on first run:

- `default.md` -- generic meeting (TL;DR, decisions, action items, quotes)
- `one-on-one.md` -- 1:1 retrospective shape (topics, commitments by side)
- `standup.md` -- yesterday / today / blockers per speaker

Add your own: drop any `*.md` file into the prompts directory. It will
appear in the Generate Synthesis Prompt template picker on next open.

Three placeholders are substituted:

- `{{session_title}}` -- the session's title
- `{{date}}` -- the session's creation date (`YYYY-MM-DD HH:MM`)
- `{{transcript}}` -- the full transcript body

Any other `{{...}}` text passes through unchanged.

## IT review notes (your org or equivalent)

All third-party dependencies are pip-installable Python wheels. None are
standalone EXEs or system binaries outside the Python ecosystem. The wheels
that ship compiled native code are:

| Package | Compiled artifact | License |
|---|---|---|
| PyQt6 | Qt 6 binaries | LGPL-3.0 |
| PyAudio | PortAudio | MIT |
| PyAudioWPatch | PortAudio fork with WASAPI loopback patch | MIT |
| faster-whisper -> ctranslate2 | CTranslate2 inference engine | MIT |
| faster-whisper -> tokenizers | Hugging Face tokenizers (Rust) | Apache-2.0 |
| numpy | OpenBLAS | BSD-3-Clause |
| webrtcvad-wheels | WebRTC VAD | BSD-3-Clause |

The design doc appendix on your wiki captures exact wheel filenames + SHA256s for the
IT review packet. Regenerate the manifest after any version bump:

```powershell
pip download -r requirements.txt -d wheels\
Get-ChildItem wheels\ | ForEach-Object {
    "$($_.Name)  $((Get-FileHash $_.FullName -Algorithm SHA256).Hash)"
}
```

### Network egress

The app makes exactly one kind of outbound network call: downloading the
selected faster-whisper model from `huggingface.co` on first run. After
that, no network. All other paths (clipboard handoff, file I/O,
transcription) are on-device.

### Corporate proxies (Netskope, Zscaler, etc.)

If your workstation routes through a MITM proxy that re-signs TLS, the
first-run model download will fail with `CERTIFICATE_VERIFY_FAILED` even
though Edge / Outlook / npm all work. Python's `httpx` (used by
`huggingface_hub`) doesn't see the corporate CA -- it has its own bundled
trust list.

The app ships `truststore` (Windows only, in `requirements.txt`) and
injects it at startup in `main.py`. `truststore` makes Python's `ssl`
module use the OS certificate store, which already trusts the corporate
CA. **`pip install -r requirements.txt`** is enough; no manual CA wrangling
needed.

If `truststore` isn't enough (e.g. the proxy uses pinning, or you're on a
Python ssl build with weird defaults), pre-stage the model offline:

1. On a machine that can reach `huggingface.co`, download the model files.
   Easiest: `pip install faster-whisper` in a clean venv and run
   `python -c "from faster_whisper import WhisperModel; WhisperModel('small.en')"`
   then copy the snapshot directory to a thumb drive.
2. On the work machine, drop the model files into
   `%APPDATA%\MeetingNotetaker\models\small.en\` (or whichever size).
   That directory should contain `model.bin`, `config.json`, and either
   `tokenizer.json` or `vocabulary.txt`.
3. Start the app. The model manager detects the local snapshot and never
   calls Hugging Face.

You can also set `HF_HUB_OFFLINE=1` in the environment to force offline
mode against the standard HF cache layout (`models--Systran--faster-whisper-
<size>/`). Either approach works.

## Architecture

The full Technical Design Document lives on your wiki (look under your-workspace >
Projects > Meeting Notetaker > design doc: Meeting Notetaker). Short version:

- `meeting_notetaker.app` -- entry point, sets up Qt, lock, controller, UI.
- `meeting_notetaker.controller.SessionController` -- bridges the UI to the
  audio + transcription pipeline. Owns session lifecycle.
- `meeting_notetaker.models.session.SessionStore` -- SQLite (WAL) wrapper
  for session + folder metadata.
- `meeting_notetaker.models.transcript.TranscriptStore` -- per-session
  Markdown files (`raw.transcript.md`, `notes.md`, archived notes,
  `metadata.json`).
- `meeting_notetaker.audio.mic_recorder.MicRecorder` -- PyAudio capture.
- `meeting_notetaker.audio.loopback_recorder.LoopbackRecorder` -- WASAPI
  loopback via PyAudioWPatch (Windows only). Cribbed from WhisperType.
- `meeting_notetaker.audio.chunk_buffer.ChunkBuffer` -- per-source 10s
  rolling buffers with 5s overlap. `dedupe_overlap()` collapses the seam
  between consecutive Whisper outputs.
- `meeting_notetaker.transcription.model_manager` -- lazy-loaded
  faster-whisper model with per-size cache.
- `meeting_notetaker.transcription.worker.LiveTranscriptionWorker` --
  QThread per source, polling the ChunkBuffer and emitting segments.
- `meeting_notetaker.transcription.worker.batch_transcribe` -- final-pass
  transcription on stop; also the only path in capture-only mode.

## Development

Run tests (pure-Python; no PyQt6, no audio hardware required):

```bash
pytest tests/ -v
```

The integration-marker tests need real audio hardware and are skipped by
default:

```bash
pytest tests/ -v -m audio       # opt in
```

## License

MIT. See `LICENSE`.
