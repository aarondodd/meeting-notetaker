# Synthesis via MCP

Meeting Notetaker v0.8 retires the bundled Chrome extension in favor
of a local Model Context Protocol (MCP) server. Any Claude surface
that speaks MCP (Chrome Claude extension, Claude Desktop, Claude Code)
can read your meeting transcripts, render a synthesis prompt against
your current template, and save the result back to Meeting Notetaker.
Your meetings never leave the machine.

## Why MCP replaced the extension

The old bridge drove claude.ai through a Chrome extension and a native
messaging host. Every claude.ai markup change broke selectors; the
service worker's wake-up quirks and the clipboard permission flow
made first-run flaky. The MCP path removes all of that: tools are
Python functions on your box, the Claude surface calls them with a
real schema, and the synthesis happens in the Claude surface itself.

## What you'll need

- Meeting Notetaker v0.8 or newer, installed via the Inno Setup
  installer or built from source
- One of
  - Chrome, with the Claude extension installed
  - Claude Desktop
  - Claude Code

If you don't have the Chrome Claude extension yet, click the "Open
Chrome Web Store..." button in Meeting Notetaker's Settings page
titled "Synthesis via MCP", or go to the Claude extension listing in
your browser.

## Setup, two steps

1. In Meeting Notetaker, open Settings > Synthesis via MCP.
2. Tick "Enable synthesis via MCP". Copy the config snippet shown
   below the tick-box, then paste it into your Claude client's MCP
   config. The three common clients each have their own location.

### Chrome Claude extension

Open the extension's options page, scroll to "MCP servers", and paste
the snippet into the server list. Save; reload the page you want
synthesis on.

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`
on macOS or `%APPDATA%\Claude\claude_desktop_config.json` on Windows.
Merge the snippet into the top-level object (if the file already has
an `mcpServers` key, add `meeting-notetaker` under it; otherwise paste
the whole thing). Restart Claude Desktop.

### Claude Code

```bash
claude mcp add meeting-notetaker '<launcher-command-from-settings>'
```

Or hand-edit `~/.config/claude/claude.json` the same way as Claude
Desktop.

## The config snippet

Meeting Notetaker's Settings page shows the exact snippet for your
install. On a frozen Windows build it looks like:

```json
{
  "mcpServers": {
    "meeting-notetaker": {
      "command": "C:\\Program Files\\MeetingNotetaker\\meeting-notetaker.exe",
      "args": ["--mcp-server"]
    }
  }
}
```

A source checkout looks like:

```json
{
  "mcpServers": {
    "meeting-notetaker": {
      "command": "/usr/bin/python3",
      "args": ["/path/to/meeting-notetaker/main.py", "--mcp-server"]
    }
  }
}
```

## Verifying

Once the client is pointed at the server, ask your Claude surface
"list my meetings" or "what's the latest meeting I recorded". It
should call `list_meetings` and return the sessions it finds.

## Available tools

The server exposes these tools; each one is schema-advertised so the
Claude surface sees descriptions and types directly:

- `list_meetings(limit?, state?, folder_id?)`: recent sessions
- `get_meeting(meeting_id)`: metadata plus parsed attendees
- `get_transcript(meeting_id)`: raw transcript body
- `get_live_notes(meeting_id)`: user-authored live-notes buffer
- `get_notes(meeting_id)`: current synthesized notes, if any
- `list_templates()`: synthesis prompt templates
- `get_template(name)`: a template's raw body
- `render_synthesis_prompt(meeting_id, template_name?)`: fully
  substituted prompt ready to execute
- `save_notes(meeting_id, notes)`: write notes.md, archive prior
- `set_prompt_template(meeting_id, template_name)`: per-meeting
  template override

The canonical flow is: `list_meetings` to pick an id,
`render_synthesis_prompt` to get a ready-to-execute prompt, let your
model synthesize, then `save_notes` to write the result. Meeting
Notetaker reloads its Synthesis tab the next time the session is
opened.

## Troubleshooting

- The MCP server logs to stderr. Claude Desktop surfaces stderr in
  its MCP server status pane; Claude Code puts it in its debug log.
- If the Claude surface reports "server failed to start", confirm the
  launcher command runs by hand: open a terminal and run the exact
  `command` + `args` from the snippet. It should sit waiting for
  stdin.
- If `list_meetings` returns zero sessions, check that
  `%APPDATA%\MeetingNotetaker\` has your real meeting store. The MCP
  server honors `MEETING_NOTETAKER_DATA_DIR` the same way the GUI
  does, so a test override works too.
