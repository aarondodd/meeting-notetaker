# Meeting Notetaker

Granola-style local meeting capture + clipboard-mediated LLM synthesis for
Windows. PyQt6 desktop app. No audio or transcript leaves the machine; final
note synthesis routes through a company-approved chatbot via clipboard, not
API.

The full Technical Design Document lives on your wiki at your-workspace > Projects >
Meeting Notetaker > "design doc: Meeting Notetaker". Read that before any
substantive design change.

## Project Structure

```
main.py                          # 3-line entry: delegates to meeting_notetaker.app.main
meeting_notetaker/
  app.py                         # MainApp -- wires UI + controller + tray + store
  controller.py                  # SessionController -- session lifecycle, recorders, workers
  version.py
  models/
    session.py                   # Session, Folder dataclasses + SessionStore (SQLite WAL)
    transcript.py                # TranscriptSegment + TranscriptStore (per-session md files)
  audio/
    mic_recorder.py              # PyAudio input
    loopback_recorder.py         # PyAudioWPatch WASAPI loopback (Win-only, cribbed from WhisperType)
    chunk_buffer.py              # rolling per-source PCM buffers + dedupe_overlap()
    resample.py                  # mono downmix + linear resample (no scipy)
    vad.py                       # webrtcvad wrapper (optional)
  transcription/
    model_manager.py             # lazy-loaded faster-whisper instance, per-size cache
    worker.py                    # LiveTranscriptionWorker QThread + batch_transcribe()
  ui/
    main_window.py               # left pane: session list + bulk delete + new
    session_view.py              # right pane: transcript / notes / previous notes + controls
    new_session_dialog.py        # title + per-session "Keep recording" toggle
    prompt_dialog.py             # Generate Synthesis Prompt + Paste Response Back
    settings_dialog.py           # model size, VAD, retain default, capture-only, theme
    tray.py                      # QSystemTrayIcon wrapper with state coloring
  utils/
    paths.py                     # %APPDATA% / XDG resolution; MEETING_NOTETAKER_DATA_DIR override
    config.py                    # TOML round-trip + validation
    prompts.py                   # bundled + user-editable templates + render()
    icons.py                     # QPainter-drawn tray + app icons (no PNGs)
    single_instance.py           # lockfile-based single-instance enforcement
  resources/
    prompts/
      default.md                 # generic meeting template
      one-on-one.md
      standup.md
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
  raises a clear error; v0.2+ will add the subprocess-isolated fallback per
  design doc Open Question 10.

### Why PyAudio + PyAudioWPatch (not sounddevice)

PyAudioWPatch's WASAPI loopback support is the proven path on Windows
(WhisperType, multiple other small projects). `sounddevice` can be coaxed
into loopback via device-index hacks, but the API is less ergonomic and the
matrix of Windows audio API quirks is poorly documented. We keep
`sounddevice` in `requirements.txt` purely for device enumeration as a
sanity-check tool.

### Why linear resample instead of scipy

scipy is ~120 MB of native code. Whisper accepts somewhat imprecise input
(the model itself was trained on 16 kHz mono with various provenance).
Linear interpolation through numpy is plenty for our 48 kHz -> 16 kHz
downsample step. Test coverage in `tests/test_resample.py` includes the
basic correctness assertions.

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
- **Avoid scipy / librosa.** The marginal accuracy improvement is not worth
  the install footprint or the IT-review pain of additional native wheels.
- **Keep PortAudio off the critical path for unit tests.** Anything that
  imports `pyaudio` or `pyaudiowpatch` lives behind a local import so
  pure-Python modules can be tested without PortAudio installed.

## Status (v0.1)

- 42 unit tests passing on Linux.
- App imports cleanly with PyQt6 absent (pure-Python modules) and is
  expected to launch on Windows with the full requirements.
- Live transcription, batch transcription, prompt copy, paste-back, crash
  recovery, bulk delete, settings, single-instance lock all wired.
- Not yet tested end-to-end against a real meeting on Windows.
- Subprocess-isolated loopback fallback (design doc Open Question 10) is stubbed
  but not implemented; lands in v0.2 if WASAPI stale-handle bug surfaces.
