# Meeting Notetaker

Granola-style local meeting capture + clipboard-mediated LLM synthesis for
Windows. PyQt6 desktop app. No audio or transcript leaves the machine; final
note synthesis routes through an approved chatbot via clipboard, not API.

## Project Structure

```
main.py                          # entry: --check-deps + --native-host + GUI default
meeting_notetaker/
  app.py                         # MainApp -- wires UI + controller + tray + store + bridge
  controller.py                  # SessionController -- session lifecycle, recorders, workers
  version.py
  automation/                    # synthesis automation bridge (v0.6.3+)
    messages.py                  # typed request/response/status messages
    protocol.py                  # length-prefixed JSON framing (Chrome native messaging)
    bridge.py                    # in-app TCP loopback server; one peer at a time
    native_host.py               # --native-host mode: stdio <-> TCP bridge
    targets.py                   # LLMTarget catalog (Claude, Copilot stub)
    installer.py                 # extension extract + native-host manifest + HKCU keys
  models/
    session.py                   # Session, Folder dataclasses + SessionStore (SQLite WAL)
    transcript.py                # TranscriptSegment + TranscriptStore (transcript + live_notes + notes)
  audio/
    mic_recorder.py              # PyAudio input
    loopback_recorder.py         # PyAudioWPatch WASAPI loopback (Win-only, cribbed from WhisperType)
    chunk_buffer.py              # rolling per-source PCM buffers + dedupe_overlap()
    resample.py                  # mono downmix + linear resample (no scipy)
    vad.py                       # silero-vad wrapper (with webrtcvad + energy fallback)
  transcription/
    model_manager.py             # lazy-loaded faster-whisper instance, per-size cache
    worker.py                    # LiveTranscriptionWorker QThread + batch_transcribe()
  integrations/
    outlook_calendar.py          # Outlook COM monitor + meeting-imminent toast
    audio_session_monitor.py     # pycaw-based ad-hoc meeting detect (opt-in)
  ui/
    main_window.py               # left pane: session list + bulk delete + new
    session_view.py              # right pane: transcript / my-notes / synthesis / previous + controls
    live_notes_widget.py         # Markdown editor + formatting toolbar + Preview/Edit toggle
    new_session_dialog.py        # title + per-session "Keep recording" toggle
    prompt_dialog.py             # Generate Synthesis Prompt + Paste Response Back
    settings_dialog.py           # model, VAD, batch toggles, retain default, name, prompts-folder, automation toggle
    automation_install_dialog.py # three-step wizard for Path-3 unpacked-extension install
    tray.py                      # QSystemTrayIcon wrapper with state coloring
  utils/
    paths.py                     # %APPDATA% / XDG resolution; MEETING_NOTETAKER_DATA_DIR override
    config.py                    # TOML round-trip + validation
    prompts.py                   # bundled + user-editable templates + render(); upgrades stale files
    live_notes.py                # live-notes seed body + attendee parser
    icons.py                     # QPainter-drawn tray + app icons (no PNGs)
    single_instance.py           # lockfile-based single-instance enforcement
  resources/
    prompts/
      default.md                 # generic meeting template
      one-on-one.md
      standup.md
    extension/                   # bundled Chrome MV3 extension (v0.6.3+); installer copies to %LOCALAPPDATA%
      manifest.json              # deterministic key + extension id (see installer.EXTENSION_ID)
      background.js              # service worker; native messaging port; tab orchestration
      popup.html / popup.js
      content/
        common.js                # shared helpers: interstitial detection, paste, streaming watch
        claude.js                # Claude.ai composer + response scraper
        copilot.js               # M365 Copilot stub (returns 'not implemented' in 0.6.3)
      icons/icon{16,48,128}.png
tests/                           # pure-Python (no Qt, no audio); 42 tests
build.ps1                        # Windows production build
build.sh                         # Linux/macOS smoke build
meeting_notetaker.spec           # pyinstaller spec (mirrors progman-py)
requirements.txt
requirements-dev.txt
pyproject.toml
.pre-commit-config.yaml
```

## Commands

```bash
# Install dependencies (Linux/macOS dev or Windows)
python -m venv .venv && source .venv/bin/activate  # or .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt

# Run tests (42 pure-Python tests)
python -m pytest tests/ -v

# Run a single test file
python -m pytest tests/test_chunk_buffer.py -v

# Run integration tests (need real audio hardware; skipped by default)
python -m pytest tests/ -v -m audio

# Run the app
python main.py

# Build the Windows executable
./build.ps1                # PowerShell
./build.sh                 # Linux/macOS dev smoke build
```

## Architecture

### Data flow during a recording

```
MicRecorder  --int16 PCM, native rate-->  mic.wav
                                          mono downmix + 16k resample -> ChunkBuffer[mic]

LoopbackRecorder --int16 PCM, native rate-> sys.wav
                                           mono downmix + 16k resample -> ChunkBuffer[sys]

ChunkBuffer (per source) --10s windows, 5s overlap-->  LiveTranscriptionWorker (one per source)
                                                       |
                                                       v
                                          chunk_done signal --> SessionView.append_segment
                                                            --> SessionController._live_segments

[on Stop]                                Stop recorders -> WAVs close.
                                         Drain workers -> last partial windows transcribed.
                                         Write _live_segments to raw.transcript.md.
                                         _BatchTranscribeThread runs final pass on full WAVs.
                                         On completion, replace raw.transcript.md with final.
                                         Delete audio/ unless retain_audio is set.
```

### Key invariants

- The WAV files are the source of truth. Live transcription output is best-effort
  and gets replaced by the final batch pass on Stop.
- `SessionController._live_segments` is the in-memory mirror of what the live
  view shows. It is persisted to `raw.transcript.md` at Stop and then
  overwritten by the batch result.
- `retain_audio` is per-session, not global. The Settings checkbox sets the
  *default* for new sessions; the New Session dialog and the SessionView
  expose a per-session override.
- Crash recovery: any session in state `recording` / `paused` / `processing`
  at app startup is marked `error` and surfaced via dialog. The on-disk
  WAVs survive; you can manually re-trigger transcription later.
- WASAPI stale-handle bug (per WhisperType, whispertype.py:1390-1500): may
  surface on second-or-later `LoopbackRecorder.start()` in one process. v0.1
  raises a clear error; later releases will add a subprocess-isolated
  fallback if the bug shows up in practice.
- **live_notes.md vs notes.md.** Two distinct files per session: `live_notes.md`
  is the user's own running buffer (Attendees / Agenda / Notes / Action Items),
  edited in the My Notes tab with debounced auto-save on every keystroke.
  `notes.md` is the LLM-synthesized output, written when the user clicks
  Paste Response Back. The synthesis prompt receives both (transcript +
  live_notes) and is instructed to merge them.
- **Prompt upgrades are hash-gated.** `seed_user_prompts` refreshes a user
  prompt file only if its hash matches a known prior-bundled version listed
  in `_PRIOR_BUNDLED_HASHES`. Any user-modified file is preserved.
  Whenever a bundled prompt body changes, add the *previous* bundled body's
  SHA-256 to `_PRIOR_BUNDLED_HASHES[<filename>]` so existing installs pick
  up the new body on next launch.
- **Synthesis is not gated on batch refinement.** As of v0.3 the Generate /
  Paste / Copy buttons unlock as soon as the live transcript is committed
  to disk (i.e. at Stop). The batch pass runs in the background and emits
  `batch_progress` from `SessionController`; the SessionView state label
  updates with the percentage. If the user generates during refinement,
  they get the live-transcript version; when refinement finishes, the
  on-disk transcript is replaced, and the next generate pass uses it.
- **Batch pass runs sources concurrently.** `_BatchTranscribeThread` uses
  a ThreadPoolExecutor to run mic + sys through `batch_transcribe` in
  parallel. faster-whisper's `transcribe()` is thread-safe when sharing
  one model instance. Two-source recordings get a free ~2x wall-clock
  speedup; single-source recordings are unaffected.
- **`worker.batch_transcribe` is importable without Qt.** The Qt-dependent
  `LiveTranscriptionWorker` class is only defined when `PyQt6` imports.
  This is why pure-Python tests can import `batch_transcribe` for unit
  testing without `pip install PyQt6` in the test env.

### Why PyAudio + PyAudioWPatch (not sounddevice)

PyAudioWPatch's WASAPI loopback support is the proven path on Windows
(WhisperType, multiple other small projects). `sounddevice` can be coaxed
into loopback via device-index hacks, but the API is less ergonomic and the
matrix of Windows audio API quirks is poorly documented. We keep
`sounddevice` in `requirements.txt` purely for device enumeration as a
sanity-check tool.

### Why linear resample instead of scipy

Whisper accepts somewhat imprecise input (the model itself was trained
on 16 kHz mono with various provenance). Linear interpolation through
numpy is plenty for our 48 kHz -> 16 kHz downsample step, and keeps
`audio/resample.py` testable without dragging scipy into the pure-Python
test path. Test coverage in `tests/test_resample.py` includes the basic
correctness assertions.

## Conventions

- ASCII only. Em dashes get rewritten as `--`, smart quotes as `"`, no
  ellipsis chars. The project follows the same writing-style rules as the
  parent Azron workspace.
- PyQt6 imports stay inside the modules that need them. `models/`, `audio/
  {chunk_buffer,vad,resample}`, and `utils/` are pure-Python and tested on
  Linux without Qt or PortAudio installed.
- Tests use the `isolated_data_dir` autouse fixture in
  `tests/conftest.py` to redirect `%APPDATA%` to a tmp directory. Never
  touch the real user data dir from tests.
- The `audio` pytest marker gates integration tests that need real
  microphone / loopback hardware. They are skipped by default; run with
  `pytest -m audio` to opt in.
- All new tracked WAV / model / dist artifacts go through `.gitignore`. Do
  not commit binary blobs.

## Lessons to remember

- **The WhisperType reference is the trustworthy WASAPI pattern.** When in
  doubt about pyaudiowpatch behaviour, mirror whispertype.py:1390-1500
  literally rather than rebuilding from PyAudioWPatch docs.
- **faster-whisper is thread-safe for `transcribe()`.** Two workers can
  share one model instance. Do not instantiate per-thread; CPU + memory
  cost is too high.
- **Keep PortAudio off the critical path for unit tests.** Anything that
  imports `pyaudio` or `pyaudiowpatch` lives behind a local import so
  pure-Python modules can be tested without PortAudio installed.

## Status

### v0.1 (tag `v0.1.0`)

- 42 unit tests passing on Linux.
- App imports cleanly with PyQt6 absent (pure-Python modules) and is
  expected to launch on Windows with the full requirements.
- Live transcription, batch transcription, prompt copy, paste-back, crash
  recovery, bulk delete, settings, single-instance lock all wired.
- Microsoft Store Python detection + Audio Devices diagnostic dialog.
- Corporate MITM proxy handled via truststore.

### v0.2

- Live notes tab (Attendees / Agenda / Notes / Action Items) with debounced
  auto-save.
- Synthesis prompt merges live notes with transcript; attendees parsed from
  bulleted list and passed as `{{attendees}}`.
- `{{user_name}}` placeholder; "Your name" setting replaces "Me:" labels
  everywhere it matters (display + prompt + action-item attribution).
- Copy Notes to Clipboard button on the session view.
- Open Prompts Folder shortcut in Settings.
- Hash-gated bundled-prompt upgrade with line-ending normalization so a
  CRLF Windows checkout upgrades cleanly.

### v0.6.4 (current)

Two small bug fixes against the v0.6.3 preview panes:

- **Right-click Copy Image** in the My Notes / Synthesis / Previous Notes
  preview now copies the rendered image to the system clipboard. The
  default QTextBrowser context menu only exposes Copy as a text-selection
  action, which made the image-copy affordance look broken.
- **Images clamp to viewport width** in the in-app preview, and to the
  printer's paint rect (in points) on PDF / hard-copy export. The
  preview re-clamps on resize so growing the window restores native
  image size when there's room. Earlier draft pinned doc.pageSize to
  printer device-pixels and shrank all body text by the
  printerPageRect / pageSize ratio; the final version leaves pageSize
  alone so the print engine's own pagination is preserved.

Shared `clamp_image_widths(doc, max_width)` helper lives in
`ui/markdown_preview.py` and is reused by `PrintTextDocument.
clamp_images_to_printer(printer)`. 582 tests passing.

### v0.6.3

Synthesis automation lands behind a per-install Settings toggle. When
enabled, the Generate Synthesis Prompt + Paste Response Back buttons
are replaced by a single **Send to Claude.ai** button; the Copy
button stays visible regardless. The flow goes:

```
SessionView "Send" click
  -> MainApp._on_send_to_llm renders default prompt template
  -> Bridge (TCP loopback)
  -> Native host (main.py --native-host)
  -> Chrome native-messaging port
  -> background.js service worker
  -> chrome.tabs.create(claude.ai/new) + content/claude.js
  -> paste, watch streaming, scrape response
  -> back up the chain
  -> TranscriptStore.save_notes(..., archive_existing=True)
  -> SessionView reloads Synthesis tab
```

Key design choices:

- **Path 3 install only.** Chrome blocks silent unpacked-extension
  installs. The wizard guides the user through chrome://extensions +
  Load unpacked, then writes the native-host manifest + HKCU keys
  on Verify.
- **Deterministic extension ID.** manifest.json carries a `key` field
  with a base64-encoded SPKI public key; the derived ID
  (`gmnecenhibfigbpldhacjhgmooopeelo`) is hard-coded as
  `installer.EXTENSION_ID` so the native-host's allowed_origins is
  stable across machines. Regenerate both together if the key ever
  changes.
- **Proxy interstitial = browser toast + auto-resume.** The content
  script detects the FHB outbound-proxy "PROCEED" page, shows a toast,
  and polls for the URL to change. No automation clicks PROCEED -- the
  human-in-the-loop ack stays intact.
- **One bridge peer.** A second concurrent native-host connection is
  rejected. The token in `%LOCALAPPDATA%\MeetingNotetaker\automation\bridge.json`
  rotates per app launch so a stale handshake file from a crashed
  prior run can't authenticate.
- **Copilot is plumbed but not wired.** Settings dropdown lists it;
  selecting it surfaces "not yet implemented" instead of running the
  synthesis. The Copilot adapter is the next thing to ship.

### v0.3

- Markdown editor toolbar on the My Notes tab (bold/italic/headings/
  lists/quote/code/link/HR) plus a Preview/Edit toggle that renders the
  source as Markdown via QTextBrowser.setMarkdown.
- Batch refinement decoupled from synthesis: Generate / Paste / Copy
  enabled immediately after Stop; progress percentage shown in the state
  label.
- Concurrent mic + sys batch passes via ThreadPoolExecutor inside the
  batch QThread.
- Per-segment progress callback in `batch_transcribe` (uses
  `info.duration` + `seg.end`).
- Two new settings: "Skip post-Stop refinement" (live transcript is final)
  and "Fast batch mode" (beam_size=1 in the batch pass).
- 79 unit tests passing on Linux.

### Open follow-ups

- Subprocess-isolated loopback fallback is stubbed but not implemented;
  lands later if the WASAPI stale-handle bug surfaces in practice.
- Tighter integration with the chosen synthesis chatbot (M365 Copilot,
  Claude.ai, etc.) for users without admin rights to automate the
  clipboard hand-off.
- **scipy polyphase resample in `audio/resample.py`.** Replace the
  numpy linear interp with `scipy.signal.resample_poly` (or
  `librosa.resample` with `res_type="soxr_hq"`) for proper
  anti-aliased 48k -> 16k downsampling. Whisper-small.en is tolerant
  enough that the win is modest; medium.en benefits more. Keep the
  linear path behind an env var so pure-Python tests still pass
  without scipy in a stripped venv. Deferred from v0.5 (scipy + torch
  came in for SpeechBrain; this is incremental).
- **scipy hierarchical clustering in `diarization/cluster.py`.** Swap
  the greedy O(N^2) agglomerative sweep for
  `scipy.cluster.hierarchy.linkage` (average or Ward) plus a single
  threshold cut. Deterministic and slightly more accurate on
  conversational audio. The existing
  `tests/test_diarization_cluster.py` pins current behavior so a swap
  surfaces as a reviewable test diff. Deferred from v0.5.
- **WhisperX word-level alignment.** Word-level speaker attribution
  (instead of segment-level) would meaningfully improve transcripts
  with overlapping speech. Considered + rejected for v0.5 because the
  wav2vec2 forced-alignment pass adds ~30% wall-clock to the batch
  refinement (10 min -> 13 min on a 30-min meeting at small.en) plus
  a ~360MB model download. Revisit if user feedback shows
  overlapping-speech misattribution is a real problem.
