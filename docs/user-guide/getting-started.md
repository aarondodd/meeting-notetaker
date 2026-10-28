# Getting started

## Requirements

- Windows 10 / 11 (the WASAPI loopback capture is Windows-only).
- A working microphone and a default speaker / output device.
- About 500 MB of disk for the default `small.en` Whisper model. The
  model downloads on first run.

Synthesis automation is optional. If you want it, pick one of:

- Claude Desktop or Claude Code on the same machine, consuming the
  stdio MCP server directly.
- The Chrome Claude extension in any Chromium browser, driving the
  loopback web UI.

The setup matrix is in [../mcp-setup.md](../mcp-setup.md). The web UI
is a stdlib HTTP server with no extra dependency on the machine's
side.

> **Microsoft Store Python is unsupported** (dev install only).
> Microsoft Store Python runs in a UWP AppContainer sandbox that
> blocks microphone access at the OS level. The app detects this at
> startup and shows a warning dialog. Use
> [python.org](https://www.python.org/downloads/) instead.

## Installation

### Windows installer (typical user path, v0.6.6+)

Download `meeting-notetaker-setup-X.Y.Z.exe` from the [latest
release](https://github.com/aarondodd/meeting-notetaker/releases/latest)
and run it. The installer:

- Installs per-user by default (no admin required); the UAC dialog
  lets you elevate for a Program Files install if you prefer
- Creates a Start Menu shortcut plus an optional desktop shortcut
- Registers with Add/Remove Programs for clean uninstall
- Subsequent versions self-upgrade silently (Help > Upgrade or the
  weekly background check) by downloading the new installer and
  re-running it in place

### One-line bootstrap (PowerShell)

Two PowerShell scripts in the repo root cover the headless paths:

```powershell
# Fresh install: fetches the latest release asset and launches it.
iwr -useb https://raw.githubusercontent.com/aarondodd/meeting-notetaker/main/install.ps1 | iex

# Upgrade an existing install: detects the installed version via
# the Inno Setup registry entry, compares against the latest
# release, prompts to confirm, then runs the installer in upgrade
# mode (Restart Manager closes the running app + relaunches it).
iwr -useb https://raw.githubusercontent.com/aarondodd/meeting-notetaker/main/upgrade.ps1 | iex
```

Both scripts print the relevant version and asset details and ask
Y/N before downloading. Pass `-Yes` to skip the prompt for unattended
installs; `upgrade.ps1` also accepts `-Force` to reinstall even when
the installed version matches the latest release.

`install.ps1` launches the full Inno Setup wizard (EULA, install dir,
desktop shortcut). `upgrade.ps1` launches the installer with the same
`/SILENT /SUPPRESSMSGBOXES /NORESTART` flags the in-app updater uses,
so the existing install is replaced in place without re-prompting;
Windows Restart Manager closes the running app via the installer's
`CloseApplications` hook and relaunches it after. If you want the
full wizard during an upgrade for some reason (e.g. moving to a
different install dir), use `install.ps1`; the stable AppId still
produces an in-place upgrade rather than stacking two Add/Remove
Programs entries.

### Build locally (Windows)

```powershell
.\build.ps1                         # produces dist\meeting-notetaker\meeting-notetaker.exe
.\dist\meeting-notetaker\meeting-notetaker.exe
```

The executable bundles the Python runtime, PyQt6, faster-whisper, and
PyAV. The Whisper model itself is downloaded on first run into
`%APPDATA%\MeetingNotetaker\models\`.

### From source (dev)

```powershell
# Windows PowerShell
.\install-deps.ps1                  # creates .venv, installs everything
.\.venv\Scripts\Activate.ps1
python main.py
```

```bash
# Linux / macOS
./install-deps.sh
source .venv/bin/activate
python main.py
```
