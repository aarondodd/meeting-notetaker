# Meeting Notetaker

Local meeting capture for Windows. Records your microphone and the system
audio (whatever's playing through your speakers, including Teams / Zoom /
Meet calls), transcribes both streams locally with faster-whisper, and
hands the resulting transcript to you for synthesis by any LLM you trust,
via clipboard. No audio leaves the machine; no API key required.

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
                                                  Your chosen chatbot
                                                  (Claude.ai, Copilot,
                                                   ChatGPT, etc.)
                                                               |
                                                               v
                                                       Paste Response Back
                                                               |
                                                               v
                                                          notes.md
```

## Screenshots

The main window splits a session list (left) and a per-session view (right).
Recording controls sit above four tabs: the live transcript, your own notes,
the synthesized result, and any earlier synthesis runs archived for the
same session.

**Transcript -- interleaved mic + system audio, timestamped per line.**
Your microphone is labeled with the name you set in Settings (`John` in
the example below); the other side of the call is labeled `Them`.

![Main window -- Transcript tab](docs/screenshots/01-main-transcript.png)

The session list has two narrow indicator columns to the left of each
title: a speaker icon if the audio recording is still on disk, and a
state dot (red while recording, yellow while the post-Stop refinement
pass is running, green when refined). Hover any cell to see what that
column conveys.

**My Notes -- your running buffer alongside the transcript.** Markdown
source with a small formatting toolbar. Sections (`# Attendees / # Agenda
/ # Notes / # Action Items`) auto-seed on first open. Everything you
type auto-saves continuously and is fed back into the synthesis prompt.

![Main window -- My Notes (edit mode)](docs/screenshots/02-main-my-notes-edit.png)

Click **Preview** in the toolbar to render the Markdown source in place:

![Main window -- My Notes (preview mode)](docs/screenshots/03-main-my-notes-preview.png)

**Synthesis -- the response from your chatbot, rendered as Markdown.**
Populated after you click *Paste Response Back* with the LLM's reply.

![Main window -- Synthesis tab](docs/screenshots/04-main-synthesis.png)

**Previous Notes -- earlier synthesis runs, kept as an audit trail.** Any
time you paste a new response, the prior `notes.md` is archived to
`notes-YYYYMMDD-HHMM.md` and listed here.

![Main window -- Previous Notes tab](docs/screenshots/05-main-previous-notes.png)

### Dialogs

**New Session** -- title plus a per-session "Keep recording" override.
When Outlook is reachable, a **Pick from Calendar...** button appears so
you can pre-create a session for an upcoming meeting:

![New Session dialog](docs/screenshots/06-dialog-new-session.png)

**Pick from Calendar** -- lists today's remaining meetings; select one
to pre-fill the session title (still editable) and queue the attendees
+ agenda for the new session's My Notes:

![Calendar picker](docs/screenshots/11-dialog-calendar-picker.png)

**New Session pre-filled from an Outlook calendar invite** -- click the
"Meeting starting" tray notification and the dialog opens with the
meeting subject already entered; attendees + agenda land in My Notes
when you accept:

![New Session dialog (calendar pre-fill)](docs/screenshots/10-dialog-new-session-calendar-prefill.png)

**Settings** -- model size, capture / refinement toggles, custom
vocabulary, audio device picker, Outlook calendar watch, VAD, your
name, theme, and a shortcut to the prompt-templates folder. The dialog
scrolls if your screen is short:

![Settings dialog](docs/screenshots/07-dialog-settings.png)

**Generate Synthesis Prompt** -- pick a template, preview the rendered
prompt, copy to clipboard. The preview shows the live transcript merged
with your notes and the attendee list parsed from `# Attendees`:

![Generate Synthesis Prompt dialog](docs/screenshots/08-dialog-generate-prompt.png)

**Paste Response Back** -- paste the LLM's reply; on Save the prior
notes file is auto-archived (toggle controls whether):

![Paste Response Back dialog](docs/screenshots/09-dialog-paste-response.png)

## Why this exists

Many workstations forbid:

- Sending audio or transcript content out to an external API.
- Synthesizing via any LLM other than an approved one.

SaaS meeting-note tools (Granola, Otter, Fireflies, and similar)
generally violate one or both. This app moves transcription on-device and
keeps the synthesis step manual: paste the prompt and transcript into
your approved chatbot, then paste the response back. The user is the
explicit transport between the local transcript and the LLM.

Tighter LLM integration is a future possibility, but the initial release
deliberately keeps audio on-device and routes synthesis through a human
hand-off.

## Status

v0.4 alpha. End-to-end capture + transcription + synthesis works;
v0.4 adds calendar-aware capture (Outlook COM polling -> tray
notification -> pre-filled New Session) plus custom vocabulary (global
plus per-session hotwords from attendees + agenda) plus an explicit
audio-device picker. Performance tuning is ongoing; the final
refinement pass can take a while on CPU (see "Why transcription can
take a while" below).

The WASAPI loopback path is cribbed from
[Danaor/WhisperType](https://github.com/Danaor/WhisperType).

## Requirements

- Windows 10 / 11 (the WASAPI loopback capture is Windows-only).
- Python 3.10+ (3.11+ recommended).
- A working microphone and a default speaker / output device.
- ~500 MB of disk for the default `small.en` Whisper model. The model
  downloads on first run.

The dev environment installs PyAudio's PortAudio binding. On Windows the
pip wheel ships PortAudio binaries; no extra system install is needed.

## Install (dev)

```powershell
# Windows PowerShell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
python main.py
```

**Important on Windows:** make sure the `python` you use is from
[python.org](https://www.python.org/downloads/), **not** the Microsoft
Store. Microsoft Store Python runs in a UWP AppContainer sandbox that
blocks microphone access at the OS level -- you'll get "No input audio
devices found" no matter what you do in Windows Privacy settings. The
app detects this at startup and shows a warning dialog. To check which
Python you have:

```powershell
where.exe python
# Look at the FIRST hit. If it contains "WindowsApps", it is the Store Python.
# The python.org Python is typically under
#   C:\Users\<you>\AppData\Local\Programs\Python\Python312\python.exe
# or C:\Program Files\Python312\python.exe (system-wide install).
```

## Run (packaged)

The packaged build produces a clickable `.exe` -- the intended end-user
experience.

```powershell
.\build.ps1                 # produces dist\meeting-notetaker.exe
.\dist\meeting-notetaker.exe
```

The executable bundles the Python runtime, PyQt6, and faster-whisper.
The Whisper model itself is downloaded on first run into
`%APPDATA%\MeetingNotetaker\models\`.

## Usage

1. Launch the app. A blue dot appears in the system tray; the main
   window shows an empty session list.
2. **File -> New Session...** (or `Ctrl+N`). Enter a title (e.g. "1:1
   with Manager"). Optionally tick "Keep the audio recording after
   transcription" if you want this specific session to retain its WAV
   files.
3. Select the new session in the list. Click **Start**.
4. The tray icon turns red and pulses. The transcript pane fills with
   lines labeled `Me:` (your mic) and `Them:` (system audio),
   interleaved by time.
5. **Pause** / **Resume** at any time. The recorder drops buffers
   during pause without writing them to the WAV.
6. **Stop** when the meeting ends. The tray shows a purple "processing"
   dot while the final transcription pass runs. When it finishes the
   tray returns to blue and the transcript view shows the cleaned-up
   final transcript.
7. Click **Generate Synthesis Prompt**. Pick a template (default,
   one-on-one, standup, or any you added). The rendered prompt + your
   live notes + transcript is copied to your clipboard.
8. Switch to your chatbot of choice, paste, send.
9. Copy the response, come back to Meeting Notetaker, click **Paste
   Response Back...**, paste, save. The Synthesis tab now shows the
   rendered response. If a prior notes file existed it is auto-archived
   as `notes-YYYYMMDD-HHMM.md` and shown under the Previous Notes tab.
10. Click **Copy Notes to Clipboard** any time after step 9 to grab the
    raw Markdown -- ready to paste into whichever note-taking app you
    use long-term.

### Taking notes in parallel (the My Notes tab)

The **My Notes** tab is an editable Markdown buffer that lives next to
the transcript. A small toolbar above the editor handles common
formatting:

- **B** / **I** -- bold (`Ctrl+B`) and italic (`Ctrl+I`); wraps
  selection in `**...**` or `*...*`
- **H1 / H2 / H3** -- replaces the current line's heading marker
- **List** / **1. List** / **Task** -- prefixes selected lines with
  `- `, `1. 2. ...`, or `- [ ] `
- **Quote** -- prefixes selected lines with `> `
- **Code** -- inline code (`Ctrl+\``); **Code Block** -- fenced
  ` ``` ` block
- **Link** -- inserts `[text](url)` (`Ctrl+K`)
- **HR** -- inserts a `---` divider
- **Preview** -- toggle between Markdown source (edit) and rendered
  view; the button label flips to **Edit** while previewing

Open a new session and the editor is pre-seeded with:

```
# Attendees
-

# Agenda

# Notes

# Action Items
```

Use it the way you would use Granola: paste in the meeting agenda, jot
down who is in the room, type your own observations and to-dos as the
meeting runs. Everything you type auto-saves continuously (no Save
button).

When you click **Generate Synthesis Prompt**, your live notes are
included in the prompt alongside the transcript. The default template
asks the LLM to *merge* the two -- using your notes as the source of
truth for intent, framing, and any pre-meeting context that does not
appear in the transcript. Names from the `# Attendees` bulleted list
are parsed out separately and passed in as `{{attendees}}`, so
action-item owners come from the people who were actually in the
meeting instead of "TBD".

If you do not touch the My Notes tab at all, the prompt substitutes
"(none -- user did not take live notes)" for the live-notes block and
falls back to a transcript-only synthesis.

If the app crashes mid-meeting, on next launch the recovery dialog
lists any sessions left in a transient state. The WAV files (and your
live notes) survive; you can re-run the transcription against them, or
delete the session.

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
      live_notes.md              # your own running notes (auto-seeded, auto-saved)
      notes.md                   # latest LLM-generated (merged) notes
      notes-YYYYMMDD-HHMM.md     # archived prior notes (if any)
      metadata.json
      audio\                     # mic.wav + sys.wav (deleted after transcription
                                 # unless 'Keep audio' was set for the session)
```

## Settings

Open via **File -> Settings...** (`Ctrl+,`) or the tray menu. All of
these are also editable directly in `config.toml`.

| Setting | Default | What it does |
|---|---|---|
| Your name | (empty -> "Me") | Replaces "Me:" in the transcript display and in the synthesis prompt. When set, the LLM sees your real name and assigns action items by name instead of "TBD". The on-disk transcript is always stored with the neutral "Me:" label and rewritten on display, so changing your name later does not break old sessions. |
| Model size | `small.en` | Which faster-whisper model to use. See "Choosing a model" below. |
| Capture-only mode | off | Skip live transcription; run a single Whisper pass when you click Stop. Lower CPU during the meeting, no live view. |
| Skip post-Stop refinement | off | Make the live transcript final. No second Whisper pass after you click Stop. See "Why transcription can take a while" below for the trade-off. |
| Fast batch mode | off | When the refinement pass runs, use beam_size=1 (greedy decoding) instead of beam_size=5 (beam search). About 3x faster, modest quality drop on English-only models. Ignored when "Skip refinement" is on. |
| Retain audio (default) | off | Default state of the "Keep recording" checkbox for new sessions. Per-session override stays available. |
| Enable VAD | on | Trim silent stretches before Whisper decodes them. Saves CPU. Disable if it ever clips speech. |
| VAD min silence (ms) | 500 | How quiet a stretch has to be (in ms) before VAD treats it as silence. 250-2000 ms range. |
| Theme | auto | UI theme. (Stub for v0.1; `auto` follows the OS.) |
| Mic device | (System default) | Which input device to capture from. Persists by name so the same device is picked after replug or reboot. |
| Loopback device | (System default) | Which WASAPI loopback device to capture system audio from. Windows-only. |
| Custom vocabulary | (empty) | One phrase per line in `vocabulary.txt`. Biases the transcriber toward proper nouns and corporate terms it would otherwise mis-hear ("Plantronics", "EDAPA-737", "Snowflake Cortex"). Per-session hotwords are also auto-derived from your `# Attendees` block and the agenda body when present. |
| Watch Outlook calendar | off | Poll Outlook for upcoming meetings and pop a tray notification a few minutes before each one starts. Click the notification to open New Session with the meeting subject, attendees, and agenda pre-filled. Recording is never started automatically. Windows + Outlook only. |
| Notify within (min) | 5 | How far in advance to surface the calendar notification. |

### Why transcription can take a while (and what to do about it)

Two Whisper passes run for every recording:

- **Live transcription**, while recording. Each source (mic + system
  audio) runs as its own background worker, popping 10-second windows
  with 5-second overlap from a ring buffer. Each window is decoded as
  it arrives. By the time you click Stop, the transcript pane is
  almost always within a few seconds of the actual end of audio. This
  is the "good enough" version -- it works well in practice but does
  not see cross-sentence context across window boundaries.
- **Batch refinement**, after Stop. The full mic + sys WAV files are
  re-transcribed end-to-end so Whisper has the entire context to lean
  on. Quality is slightly higher (better punctuation, fewer split
  sentences, fewer mis-heard words at chunk boundaries). On CPU this
  is roughly **real-time** -- a 30-minute meeting on `small.en` takes
  ~30 minutes of CPU to refine.

You no longer have to wait for the refinement pass before
synthesizing. The status label switches from "Recording" to
**"Refining transcript -- you can synthesize now"** the moment Stop
completes, the Generate / Paste / Copy buttons are immediately
available, and the percentage updates live as the refinement runs in
the background. Anything you do during refinement uses the live
transcript; if you re-generate after refinement finishes, the better
version is used automatically.

If the refinement wait still bothers you:

- **Skip post-Stop refinement.** Best if you find live quality
  acceptable. Settings -> Skip post-Stop refinement = on. No CPU cost
  after Stop; transcription is "done" immediately.
- **Fast batch mode.** Cuts the refinement wall-clock by roughly two
  thirds. Quality drop is modest for English-only models. Settings ->
  Fast batch mode = on.
- **Smaller model.** `base.en` is roughly 3x faster than `small.en`
  for both live and batch passes. Quality is a real step down, but
  workable for many meetings.
- **Retain the audio.** Per-session "Keep audio" toggle keeps the WAV
  files. If you ever want to re-run Whisper with a different model or
  settings, you have the source material to do it.

Two-source recordings (mic + system audio, e.g. a Teams call with
system audio captured) get a free 2x speedup on the refinement pass:
both sources run in parallel on different CPU cores. Single-source
recordings (mic only) get no benefit from this.

### Choosing a model

| Size | Disk | Quality | Notes |
|---|---|---|---|
| `tiny.en` | ~75 MB | Low | Useful only if your CPU is very slow or you just need keyword-level signal. |
| `base.en` | ~145 MB | OK | A reasonable smaller default. |
| `small.en` | ~480 MB | Good | Recommended for real meetings. |
| `medium.en` | ~1.5 GB | Best | ~3x slower than small.en. Use for high-stakes recordings if you have CPU headroom. |

Switching models reloads at the start of the next recording.
Already-recorded sessions are not re-transcribed automatically.

### Adding or editing synthesis prompts

Bundled prompts seed `%APPDATA%\MeetingNotetaker\prompts\` on first
run:

- `default.md` -- generic meeting (Attendees / Agenda / TL;DR /
  Decisions / Notes / Action Items / Open Questions / Verbatim Quotes;
  merges live notes with transcript)
- `one-on-one.md` -- 1:1 retrospective shape (topics, commitments by
  side)
- `standup.md` -- yesterday / today / blockers per speaker

**Editing existing prompts.** Open Settings (`Ctrl+,`) and click
**Open Prompts Folder** -- or browse to
`%APPDATA%\MeetingNotetaker\prompts\` yourself. Edit any `.md` file in
your favorite text editor and save. Changes are picked up the next
time you open the Generate Synthesis Prompt dialog (no restart
needed).

**Adding your own prompts.** Drop any `*.md` file into the prompts
directory. It appears in the template picker on next open. Filename
(sans extension) becomes the display name; `my-team-retro.md` shows as
"My Team Retro".

**Available placeholders** (any other `{{...}}` text passes through
unchanged):

| Placeholder | What it expands to |
|---|---|
| `{{session_title}}` | The session's title |
| `{{date}}` | The session's creation date (`YYYY-MM-DD HH:MM`) |
| `{{transcript}}` | The full transcript body. If "Your name" is set, every `[HH:MM:SS] Me: ` line prefix is rewritten to `[HH:MM:SS] <Your name>: ` before substitution, so the LLM sees you by name. |
| `{{live_notes}}` | The body of the My Notes tab. If the user did not take live notes, expands to `(none -- user did not take live notes)`. |
| `{{attendees}}` | Comma-joined list of names from the `# Attendees` bullets in My Notes. Empty -> `(none specified)`. |
| `{{user_name}}` | The value of the "Your name" setting. Empty setting -> `Me`. |

**Upgrading.** Bundled prompts get new revisions across releases. If
you have not edited a template (its on-disk body matches the version
that shipped with a prior release), the app refreshes it automatically
on startup. Any prompt you have actually edited is never touched -- it
stays exactly as you left it. To force a refresh of a customized
prompt, delete the file and restart; the latest bundled body re-seeds
on next launch.

**Tip:** start from `default.md` as your base when writing a new
prompt -- the section headings (`# Attendees`, `# Agenda`, `# Action
Items`, etc.) match the live-notes template, so the LLM's output drops
into your final notes shape cleanly.

### Network egress

The app makes exactly one kind of outbound network call: downloading
the selected faster-whisper model from `huggingface.co` on first run.
After that, no network. All other paths (clipboard hand-off, file
I/O, transcription) are on-device.

### Corporate proxies (Netskope, Zscaler, etc.)

If your workstation routes through a MITM proxy that re-signs TLS, the
first-run model download will fail with `CERTIFICATE_VERIFY_FAILED`
even though Edge / Outlook / npm all work. Python's `httpx` (used by
`huggingface_hub`) does not see the corporate CA -- it has its own
bundled trust list.

The app ships `truststore` (Windows only, in `requirements.txt`) and
injects it at startup in `main.py`. `truststore` makes Python's `ssl`
module use the OS certificate store, which already trusts the
corporate CA. **`pip install -r requirements.txt`** is enough; no
manual CA wrangling needed.

If `truststore` is not enough (e.g. the proxy uses pinning, or you are
on a Python ssl build with weird defaults), pre-stage the model
offline:

1. On a machine that can reach `huggingface.co`, download the model
   files. Easiest: `pip install faster-whisper` in a clean venv and
   run
   `python -c "from faster_whisper import WhisperModel; WhisperModel('small.en')"`,
   then copy the snapshot directory to a thumb drive.
2. On the work machine, drop the model files into
   `%APPDATA%\MeetingNotetaker\models\small.en\` (or whichever size).
   That directory should contain `model.bin`, `config.json`, and
   either `tokenizer.json` or `vocabulary.txt`.
3. Start the app. The model manager detects the local snapshot and
   never calls Hugging Face.

You can also set `HF_HUB_OFFLINE=1` in the environment to force
offline mode against the standard Hugging Face cache layout
(`models--Systran--faster-whisper-<size>/`). Either approach works.

## License

MIT. See `LICENSE`.
