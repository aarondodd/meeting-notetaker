# Meeting Notetaker

Local meeting capture for Windows. Records your microphone and the system
audio (whatever is playing through your speakers, including Teams / Zoom /
Meet calls), transcribes both streams on-device with faster-whisper, and
hands the resulting transcript to you for synthesis by any LLM you trust,
via clipboard. No audio leaves the machine; no API key required.

> **Status:** v0.5 alpha. End-to-end capture, transcription, and synthesis
> work. Performance tuning is ongoing.

## Features

- **On-device transcription** via faster-whisper (CPU, int8). Runs live
  during the meeting and refines after Stop.
- **Mic + system-audio capture** through WASAPI loopback, so both sides
  of a Teams / Zoom / Meet call are recorded.
- **Clipboard-mediated synthesis.** Generate a prompt, paste it into any
  approved chatbot (Claude.ai, Copilot, ChatGPT, ...), then paste the
  reply back. The audio and transcript never touch a third-party API.
- **Speaker identification.** SpeechBrain ECAPA-TDNN embeddings cluster
  the loopback channel into per-speaker turns and match them against a
  local library that grows as you label voices.
- **Outlook calendar awareness.** Tray notifications a few minutes before
  scheduled meetings; clicking pre-fills the New Session dialog with the
  invite's title, attendees, and agenda.
- **Ad-hoc meeting detection.** Optional: watches the Windows audio
  mixer for sustained audio from a known meeting app (Teams, Zoom,
  WebEx, ...) and offers to start a session. The OS already tracks which
  apps are playing; we read the metadata, not the stream.
- **Live notes alongside the transcript.** Markdown editor with image
  paste, auto-save, and a section template (Attendees / Agenda / Notes /
  Action Items). Notes merge into the synthesis prompt.
- **Editable synthesis output.** The LLM reply lands in an editable
  Markdown tab so it can be tweaked in place before sharing.
- **PDF export and printing.** Native Qt PDF backend preserves embedded
  images and clickable links.
- **Crash-resilient.** Sessions left in a transient state at startup
  surface in a recovery dialog; WAV files are preserved.
- **Self-updating.** Weekly check against GitHub releases; one-click
  download + rebuild + in-place install.

## Architecture

```mermaid
flowchart LR
    mic[Microphone]
    sys["System audio<br/>(WASAPI loopback)"]
    whisper["Local Whisper<br/>faster-whisper, CPU, int8"]
    transcript[raw.transcript.md]
    diar["Speaker ID pass"]
    prompt[Generate<br/>Synthesis Prompt]
    clip[Clipboard]
    chatbot["Your chosen chatbot<br/>(Claude.ai, Copilot, ChatGPT, ...)"]
    paste[Paste Response Back]
    notes[notes.md]

    mic -->|mic.wav| whisper
    sys -->|sys.wav| whisper
    whisper --> transcript
    sys -. loopback only .-> diar
    diar -->|rewrites with<br/>speaker names| transcript
    transcript --> prompt --> clip --> chatbot --> paste --> notes
```

The WASAPI loopback path is adapted from
[Danaor/WhisperType](https://github.com/Danaor/WhisperType).

## Screenshots

The main window splits a session list (left) and a per-session view (right).
Recording controls sit above four tabs: the live transcript, your own
notes, the synthesized result, and any earlier synthesis runs archived for
the same session.

**Transcript -- interleaved mic + system audio, timestamped per line.**

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
Paste an image from the clipboard (or click **Image** in the toolbar to
insert a file) and the app copies it into the session's `images/` folder
and inserts a Markdown reference.

![Main window -- My Notes (edit mode)](docs/screenshots/02-main-my-notes-edit.png)

Click **Preview** in the toolbar to render the Markdown in place:

![Main window -- My Notes (preview mode)](docs/screenshots/03-main-my-notes-preview.png)

**Synthesis -- the response from your chatbot, editable in place.**
Populated after you click *Paste Response Back* with the LLM's reply.
Same Preview / Edit toggle as My Notes, so the generated note can be
tweaked before sharing. The **Print...** button above the tabs sends
the active tab to a physical printer. For a PDF copy use **Export
PDF...** -- it writes a PDF directly via Qt's PDF backend, which
preserves images and clickable links. The **Copy** button copies
whichever tab is currently active.

![Main window -- Synthesis tab](docs/screenshots/04-main-synthesis.png)

**Previous Notes -- earlier synthesis runs, kept as an audit trail.**
Every time you paste a new response, the prior `notes.md` is archived to
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

**New Session pre-filled from a calendar invite** -- click the
"Meeting starting" tray notification and the dialog opens with the
meeting subject already entered; attendees + agenda land in My Notes
when you accept:

![New Session dialog (calendar pre-fill)](docs/screenshots/10-dialog-new-session-calendar-prefill.png)

**Settings** -- model size, capture / refinement toggles, custom
vocabulary, audio device picker, Outlook calendar watch, ad-hoc meeting
detection, VAD, your name, and a shortcut to the prompt-templates
folder. The interface follows the OS dark/light setting automatically.

![Settings dialog](docs/screenshots/07-dialog-settings.png)

**Generate Synthesis Prompt** -- pick a template, preview the rendered
prompt, copy to clipboard:

![Generate Synthesis Prompt dialog](docs/screenshots/08-dialog-generate-prompt.png)

**Paste Response Back** -- paste the LLM's reply; on Save the prior
notes file is auto-archived:

![Paste Response Back dialog](docs/screenshots/09-dialog-paste-response.png)

**Label Unknown Speakers** -- pops automatically after the post-meeting
speaker-identification pass if any voice in the recording did not match
the speaker library. Each card shows the top example transcript lines
for that cluster plus a name field that autocompletes from Outlook
attendees and known speakers:

![Label Unknown Speakers dialog](docs/screenshots/12-dialog-label-unknown-speakers.png)

**Review Speakers** -- a button on the session view that walks every
detected speaker for after-the-fact correction. Rename, forget, or
confirm; corrections feed back into the store:

![Review Speakers dialog](docs/screenshots/13-dialog-review-speakers.png)

**Manage Speakers** -- the speaker library lives at
`%APPDATA%/MeetingNotetaker/speakers.db`. Open via Settings >
Manage Speakers...; Rename / Forget per row, with a Forget All
escape hatch:

![Manage Speakers dialog](docs/screenshots/14-dialog-manage-speakers.png)

## Why this exists

Many workstations forbid:

- Sending audio or transcript content out to an external API.
- Synthesizing via any LLM other than an approved one.

SaaS meeting-note tools (Granola, Otter, Fireflies, and similar)
generally violate one or both. This app moves transcription on-device
and keeps the synthesis step manual: paste the prompt into your
approved chatbot, then paste the response back. The user is the
explicit transport between the local transcript and the LLM.

Tighter LLM integration is a future possibility, but the initial
release deliberately keeps audio on-device and routes synthesis through
a human hand-off.

## Requirements

- Windows 10 / 11 (the WASAPI loopback capture is Windows-only).
- Python 3.10+ (3.11+ recommended).
- A working microphone and a default speaker / output device.
- ~500 MB of disk for the default `small.en` Whisper model. The model
  downloads on first run.

The dev environment installs PyAudio's PortAudio binding. On Windows the
pip wheel ships PortAudio binaries; no extra system install is needed.

> **Microsoft Store Python is unsupported.** Microsoft Store Python runs
> in a UWP AppContainer sandbox that blocks microphone access at the OS
> level. The app detects this at startup and shows a warning dialog. Use
> [python.org](https://www.python.org/downloads/) instead. To check:
>
> ```powershell
> where.exe python
> # If the first hit contains "WindowsApps", it is the Store Python.
> # python.org installs to:
> #   C:\Users\<you>\AppData\Local\Programs\Python\Python312\python.exe
> # or C:\Program Files\Python312\python.exe (system-wide install).
> ```

## Installation

### From source (dev)

```powershell
# Windows PowerShell
.\install-deps.ps1        # creates .venv, installs everything
.\.venv\Scripts\Activate.ps1
python main.py
```

```bash
# Linux / macOS
./install-deps.sh
source .venv/bin/activate
python main.py
```

The install wrappers run `pip install -r requirements-dev.txt`. The
speaker-embedding encoder (SpeechBrain ECAPA-TDNN,
`speechbrain/spkrec-ecapa-voxceleb`, Apache-2.0) installs cleanly from
PyPI. Its checkpoint is ~22 MB and downloads from Hugging Face Hub on
first batch refinement, then caches under `<app_data>/models/ecapa/` for
offline runs after that.

### Packaged build (Windows)

The packaged build produces a clickable `.exe`:

```powershell
.\build.ps1                 # produces dist\meeting-notetaker.exe
.\dist\meeting-notetaker.exe
```

The executable bundles the Python runtime, PyQt6, and faster-whisper.
The Whisper model itself is downloaded on first run into
`%APPDATA%\MeetingNotetaker\models\`.

## Quick start

1. Launch the app. A blue dot appears in the system tray; the main
   window shows an empty session list.
2. **File -> New Session...** (or `Ctrl+N`). Enter a title. Optionally
   tick "Keep the audio recording after transcription" to retain the
   WAV files for this session.
3. Select the new session in the list. Click **Start**.
4. The tray icon turns red and pulses. The transcript pane fills with
   lines labeled `Me:` (your mic) and `Them:` (system audio),
   interleaved by time.
5. **Pause** / **Resume** at any time. The recorder drops buffers
   during pause without writing them to the WAV.
6. **Stop** when the meeting ends. The tray shows a purple "processing"
   dot while the final transcription pass runs. When it finishes the
   tray returns to blue and the transcript view shows the refined
   final transcript.
7. Click **Generate Synthesis Prompt**. Pick a template (default,
   one-on-one, standup, or any you added). The rendered prompt + your
   live notes + transcript is copied to your clipboard.
8. Switch to your chatbot of choice, paste, send.
9. Copy the response, come back to Meeting Notetaker, click **Paste
   Response Back...**, paste, save. The Synthesis tab now shows the
   rendered response. If a prior notes file existed it is auto-archived
   as `notes-YYYYMMDD-HHMM.md` and shown under the Previous Notes tab.
10. Click **Copy** any time to grab the active tab's contents in raw
    text form. The button label updates to match the active tab.
11. Click **Print...** to print whichever of My Notes / Synthesis is
    active, or **Export PDF...** to save a PDF that keeps images
    embedded and Markdown links clickable.

## Taking notes in parallel (My Notes)

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

Everything you type auto-saves continuously (no Save button).

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

## Speaker identification

After each recording, the app tries to label the *system audio* side of
the transcript with real names ("Alice:", "Bob:") instead of the
generic "Them:". It learns who is who over time -- the first meeting
with a new colleague needs one quick labeling pass; subsequent meetings
recognize that voice automatically. All processing is on-device. No
audio, transcript, or voice embedding ever leaves the machine.

### The pipeline

```mermaid
flowchart TD
    A[sys.wav] --> B[Segment by voice activity<br/>silero-vad, ~30ms frames]
    B --> C[Embed each turn<br/>SpeechBrain ECAPA-TDNN, 192-dim vector]
    C --> D[Cluster by cosine similarity<br/>greedy agglomerative]
    D --> E{Match centroid<br/>vs speakers.db<br/>at threshold}
    E -->|matched| F[Auto-label cluster<br/>with stored name]
    E -->|no match| G[Label Speaker N<br/>and prompt user]
    F --> H[Rewrite raw.transcript.md<br/>with real names]
    G --> H
    G -. user assigns name .-> I[Save embedding to<br/>speakers.db]
    I -. next meeting .-> E
    H --> J[diarization.json<br/>cluster -> name map for<br/>Review Speakers later]
```

The mic channel is always the user; it does not go through this
pipeline and keeps the `{{user_name}}:` / `Me:` rendering it always had.

### What runs automatically

When you click **Stop** on a recording, the controller chains:

1. **Final transcription** (existing batch pass; rewrites the live
   transcript with a cleaner version).
2. **Voice segmentation** on `sys.wav`: silero-vad chops the loopback
   channel into per-turn voiced spans (typical meeting: 30-200 turns).
   Falls back to webrtcvad and then an energy threshold if silero or
   torch can't load.
3. **Embedding**: SpeechBrain's ECAPA-TDNN produces a 192-dim vector
   per turn. Voiced turns shorter than 1.0 s are skipped because
   short-clip embeddings are unreliable and tend to pull two real
   speakers' centroids together.
4. **Clustering**: greedy agglomerative cosine merge collapses turns
   into per-speaker clusters (Merge threshold tunable in Settings,
   default 0.75). Raise this knob when two real speakers keep getting
   merged into one cluster; lower it if a single speaker keeps
   splitting into multiple clusters.
5. **Matching**: each cluster centroid is compared against every name
   in `speakers.db`. Above the threshold (default 0.75 cosine
   similarity), the cluster auto-labels with the stored name.
6. **Transcript rewrite**: `raw.transcript.md` is rewritten with the
   resolved names where matched, `Speaker N` fallback where not.
7. **`diarization.json` saved** alongside the transcript so the Review
   Speakers walker works on this session forever, even after `sys.wav`
   is cleaned up.
8. **Unknown-speaker dialog**: if any cluster did not match, the
   Label Unknown Speakers dialog pops with example transcript lines
   per cluster and a name picker seeded from Outlook attendees +
   names already in your speaker library.

All of this is automatic. The pipeline runs unless **Settings > Enable
speaker identification** is unchecked, the recording has no system
audio (mic-only session), or SpeechBrain fails to load.

### Improving accuracy over time

The library starts empty. It grows as you label voices, and the
running-average centroid update tightens each stored embedding with
every confirmation. A few minutes of attention up front pays off
across every subsequent meeting with the same colleagues.

- **Label voices the first time you see them.** When the Label Unknown
  Speakers dialog pops at end of meeting, type or pick a name for each
  card and click OK. Use a consistent form (`Alice Smith` every time,
  not `Alice` sometimes and `Alice S.` other times) -- the store keys
  on the exact string, so two variants become two separate records.
- **Skip what you don't recognize.** A bad label trains the wrong
  embedding into the store and has to be Forget-ed later. Skip the
  dialog (or specific cards) when you're not sure; the cluster keeps
  its `Speaker N` label and the embedding is not persisted.
- **Use Review Speakers when something looks wrong.** The button
  appears on any session that has a `diarization.json`. If the
  auto-labeling called Bob "Alice", click Rename on the card, pick the
  correct name, OK. The transcript rewrites on disk and the centroid
  update goes to the *new* name's record (it does not undo the old
  one -- if Alice now has a Bob-sounding embedding in her history, use
  Manage Speakers > Forget Alice and start her over).
- **Lean on the Outlook attendee list.** When you record a meeting that
  has a calendar invite, the attendee names are pre-populated in the
  name combo box. This is the easiest way to keep your library
  consistent across colleagues.
- **Periodically prune the library.** Settings > Manage Speakers shows
  the full list with sample counts and last-seen dates. Forget anyone
  you'll never recognize again -- a smaller library is faster to match
  against and keeps the auto-recognition cleaner.

### Limits

This is a small CPU-only model running on recorded audio. It is good at
distinguishing two or three clearly-different voices in a quiet
meeting. It struggles with:

- **Overlapping speech.** Cross-talk or two people speaking at the
  same time produces a turn with mixed embeddings; the cluster goes
  somewhere ambiguous. Most of the time this surfaces as an extra
  `Speaker N` cluster you can ignore or forget.
- **The same person on different microphones.** A colleague who
  joins one meeting from a headset and another from a phone speaker
  may not match across meetings. The match threshold (Settings,
  default 75%) is the knob; loosening it improves recall on this
  case at the cost of more false matches.
- **Very short utterances.** A "thanks" or "agreed" turn under about
  half a second gets dropped by the segmenter (too little audio for a
  reliable embedding). The transcript still has the line; diarization
  just doesn't try to attribute it.
- **Channel changes.** Different Teams / Zoom audio paths,
  bluetooth-vs-USB, codec changes, or aggressive noise suppression
  can shift the embedding enough to miss a match. Re-label once on
  the new channel and the running-average will accommodate.

### Privacy and footprint

- Audio, embeddings, and the speaker library all live on the local
  machine. The ECAPA-TDNN model checkpoint (~22 MB) is downloaded once
  from Hugging Face Hub on first batch refinement and cached under
  `%APPDATA%/MeetingNotetaker/models/ecapa/`. No Hugging Face account
  or token is required. After the first download, the speaker-ID path
  makes no network calls.
- The speaker library is just `%APPDATA%/MeetingNotetaker/speakers.db`
  (sqlite). To wipe it, delete the file or use **Settings > Manage
  Speakers > Forget All**.
- SpeechBrain transitively pulls `huggingface_hub`, `sentencepiece`,
  `torchaudio`, `scipy`, and `torch`. The frozen `.exe` is
  correspondingly larger (~1 GB).

## Settings

Open via **File -> Settings...** (`Ctrl+,`) or the tray menu. All of
these are also editable directly in `config.toml`.

| Setting | Default | What it does |
|---|---|---|
| Your name | (empty -> "Me") | Replaces "Me:" in the transcript display and in the synthesis prompt. When set, the LLM sees your real name and assigns action items by name instead of "TBD". The on-disk transcript is always stored with the neutral "Me:" label and rewritten on display, so changing your name later does not break old sessions. |
| Model size | `small.en` | Which faster-whisper model to use. See [Choosing a model](#choosing-a-model). |
| Capture-only mode | off | Skip live transcription; run a single Whisper pass when you click Stop. Lower CPU during the meeting, no live view. |
| Skip post-Stop refinement | off | Make the live transcript final. No second Whisper pass after you click Stop. See [Performance](#performance) for the trade-off. |
| Fast batch mode | off | When the refinement pass runs, use beam_size=1 (greedy decoding) instead of beam_size=5 (beam search). About 3x faster, modest quality drop on English-only models. Ignored when "Skip refinement" is on. |
| Retain audio (default) | off | Default state of the "Keep recording" checkbox for new sessions. Per-session override stays available. |
| Enable VAD | on | Trim silent stretches before Whisper decodes them. Saves CPU. Disable if it ever clips speech. |
| VAD min silence (ms) | 500 | How quiet a stretch has to be (in ms) before VAD treats it as silence. 250-2000 ms range. |
| Mic device | (System default) | Which input device to capture from. Persists by name so the same device is picked after replug or reboot. |
| Loopback device | (System default) | Which WASAPI loopback device to capture system audio from. Windows-only. |
| Custom vocabulary | (empty) | One phrase per line in `vocabulary.txt`. Biases the transcriber toward proper nouns and corporate terms it would otherwise mis-hear. Per-session hotwords are also auto-derived from your `# Attendees` block and the agenda body when present. |
| Watch Outlook calendar | off | Poll Outlook for upcoming meetings and pop a tray notification a few minutes before each one starts. Click the notification to open New Session with the meeting subject, attendees, and agenda pre-filled. Recording is never started automatically. Windows + Outlook only. |
| Notify within (min) | 5 | How far in advance to surface the calendar notification. |
| Detect ad-hoc meetings | off | Watch the Windows audio mixer for sustained audio from a known meeting app (Teams, Zoom, Slack, WebEx, ...). When audio sustains long enough to look like a call rather than a notification chirp, a tray toast offers to open New Session pre-filled with the app name and timestamp. Recording never auto-starts. No audio is captured. |
| Enable speaker identification | on | After each meeting, group the system-audio loopback channel into per-speaker turns and try to match each group against the stored speaker library. Unmatched groups prompt the Label Unknown Speakers dialog so you can name them. Disable to keep the generic "Them:" labels. |
| Match threshold | 75% | Cosine-similarity floor for auto-matching a meeting voice against a stored speaker. Higher = stricter (more unknowns surfaced for manual labeling); lower = looser (more auto-labels, higher risk of calling Bob Alice). 50-95% range. |
| Merge threshold | 75% | Cosine-similarity floor for merging two clusters into one during the agglomerative clustering pass. Raise this when two real speakers keep getting merged into one cluster; lower it when a single speaker keeps splitting into multiple. 50-95% range. |
| Manage Speakers... | -- | Opens a list of every stored speaker with sample count, last-seen date, Rename + Forget per row, and Forget All. The library lives at `speakers.db` and never leaves the machine. |

## Performance

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

You do not have to wait for the refinement pass before synthesizing.
The status label switches from "Recording" to **"Refining transcript --
you can synthesize now"** the moment Stop completes, the Generate /
Paste / Copy buttons are immediately available, and the percentage
updates live as the refinement runs in the background. Anything you do
during refinement uses the live transcript; if you re-generate after
refinement finishes, the better version is used automatically.

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

## Synthesis prompts

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

**Placeholders** (any other `{{...}}` text passes through unchanged):

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

## Storage layout

```
%APPDATA%\MeetingNotetaker\
  sessions.db                    # SQLite session + folder metadata (WAL)
  speakers.db                    # SQLite speaker library (name + centroid + samples)
  config.toml                    # user settings
  instance.lock                  # single-instance pidfile
  meeting_notetaker.log          # rotating app log
  models\                        # faster-whisper model cache
  prompts\                       # user-editable synthesis prompts (auto-seeded)
  vocabulary.txt                 # user-editable global hotwords
  sessions\
    <session-uuid>\
      raw.transcript.md          # interleaved transcript, source-labeled
      live_notes.md              # your own running notes (auto-seeded, auto-saved)
      notes.md                   # latest LLM-generated (merged) notes
      notes-YYYYMMDD-HHMM.md     # archived prior notes (if any)
      diarization.json           # per-cluster speaker mapping + centroids
      metadata.json
      audio\                     # mic.wav + sys.wav (deleted after transcription
                                 # unless 'Keep audio' was set for the session)
    images\                    # images pasted/inserted into live_notes.md
                               # or notes.md (referenced as images/<name>)
```

## Updates

The Help menu has **Check for Updates...** (manual) and **Upgrade...**
(download + rebuild + install in place via pyinstaller). On startup
the app also runs a silent weekly check against GitHub releases for
`aarondodd/meeting-notetaker`; if a newer tag exists you will see a
prompt. Network failures or a restricted release feed degrade to
silent no-op, so the check never blocks startup.

The Upgrade flow:

1. Downloads the release zipball and extracts it to a temp dir.
2. Runs `build.ps1` (Windows) or `build.sh` (POSIX) -- the same
   script you would run for a manual build.
3. Copies the freshly built `dist/meeting-notetaker.exe` over the
   running executable. On Windows the running .exe cannot be
   deleted, but NTFS does allow renaming it; the old binary is
   renamed to `meeting-notetaker.exe.old` and the new one takes
   the canonical name.
4. Offers a **Restart now?** prompt. On Yes, the app launches the
   new build as a detached subprocess and quits; on No, the user
   keeps working and picks up the new build on the next manual
   launch.
5. On the next startup, the old `.exe.old` sibling is cleaned up
   automatically.

When running from source (dev mode), there is no installed .exe to
replace -- the upgrade reports where the new `dist/meeting-notetaker`
binary was written and leaves it to you.

## Network and privacy

The app makes exactly one kind of outbound network call: downloading
model files from `huggingface.co` on first run (the faster-whisper
model and the ECAPA-TDNN speaker encoder). After that, no network. All
other paths (clipboard hand-off, file I/O, transcription, speaker
identification) are on-device.

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
on a Python ssl build with weird defaults), pre-stage the models
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

The ECAPA-TDNN speaker encoder follows the same pattern; pre-stage its
`<app_data>/models/ecapa/` directory if you cannot reach Hugging Face
on the work machine.

## License

MIT. See [`LICENSE`](LICENSE).
