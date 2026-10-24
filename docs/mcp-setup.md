# Synthesis via MCP

Meeting Notetaker v0.8 retires the bundled Chrome extension in favor
of an MCP service layer. The ten MCP tools (listed at the end of this
document) are the API; two transports expose them. Pick whichever
your Claude surface supports.

| Claude surface | Transport |
| :--- | :--- |
| Chrome Claude extension (claude.ai in-browser) | Local web UI at `http://127.0.0.1:PORT/` |
| Claude Desktop | stdio MCP server (`--mcp-server`) |
| Claude Code | stdio MCP server (`--mcp-server`) |

Your meetings never leave the machine either way. The web UI binds
loopback only. The stdio server reads JSON-RPC on stdin and writes to
stdout; nothing crosses the network at all.

## Why MCP replaced the extension

The old bridge drove claude.ai through a Chrome extension and a native
messaging host. Every claude.ai markup change broke selectors; the
service worker's wake-up quirks and the clipboard permission flow
made first-run flaky. The MCP path removes all of that: tools are
Python functions on your box, the Claude surface either calls them
with a real schema (Desktop and Code) or drives a page we own and can
keep stable (Chrome Claude).

## Chrome Claude extension (local web UI)

The Chrome Claude extension does not accept a stdio MCP config. Its
Custom Connectors list only takes remote MCP server URLs with OAuth
or header auth, and it will refuse a private IP, so a localhost stdio
server has nowhere to be pasted. The web UI gives the extension a
page to drive instead.

1. Open Meeting Notetaker's Settings page, tab "Synthesis via MCP".
2. Tick "Enable synthesis via MCP".
3. Click "Start web UI and open in browser...". A localhost page
   opens in your default browser; Meeting Notetaker keeps running
   alongside.
4. In Chrome, open the Claude extension sidebar on that tab. The
   extension can now read the page, follow links, and submit the
   Save form.

A typical workflow in the extension:

- "Open the latest meeting and synthesize it." The extension clicks
  through the meeting list, reads the rendered prompt pane, drafts
  notes in its own chat, types them into the textarea, and clicks
  Save.
- "Summarize all meetings in the EDA folder from last week." The
  extension filters the list, follows each detail link, reads
  transcripts and existing notes, and writes a cross-meeting summary.

You can also run the web UI from the command line:

```
<path-to-meeting-notetaker.exe> --web-ui
<path-to-meeting-notetaker.exe> --web-ui --port 8800 --no-open
```

Flags: `--port N` picks a preferred port (default 8765; falls back by
up to 20 if busy), `--no-open` skips the automatic browser launch,
`--host H` overrides the bind host (default 127.0.0.1).

### What the page offers

- `/meetings`: list view with filters (state, folder, has transcript,
  has synthesis, limit). Each row links to a detail page.
- `/meetings/<id>`: metadata, attendees, live notes, transcript,
  current synthesis, template picker, rendered prompt, textarea, Save
  button.
- `/templates`: list of configured prompt templates.
- `/api/...`: JSON mirrors of every HTML route plus every mutating
  action, for scripts and future automation. See the Automation
  section below.

### Chrome Claude needs to be allowed on localhost

Chrome extensions treat 127.0.0.1 as a host like any other. In the
Claude extension's Site Access, grant access to
`http://127.0.0.1:*/*` or to the specific port you pick.

### Importing the Claude skill (one-off setup)

The repository ships a Claude Agent Skill under `claude-skill/` that
teaches the extension exactly which pages to read, where the rendered
prompt lives, and how to post synthesis back. Zip `claude-skill/`
with `SKILL.md` at the root and import it into the Chrome Claude
extension's Skills section. Full steps and the install prerequisites
are in `claude-skill/README.md`. After the skill is imported you can
ask the extension to "synthesize the latest meeting" or "summarize
all meetings in folder X last week" without re-explaining the page
structure each time.

## Claude Desktop or Claude Code (stdio MCP)

The stdio server is the right choice when a Claude app can be
installed on the host. It gives the model the ten typed tools
directly, with schemas, so there is no page scraping involved.

1. Open Meeting Notetaker's Settings page, tab "Synthesis via MCP".
2. Tick "Enable synthesis via MCP".
3. Click "Copy config to clipboard".
4. Paste into your client's config, as below.

### The config snippet

For a frozen Windows build the snippet looks like:

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

For a source checkout it looks like:

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

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`
on macOS or `%APPDATA%\Claude\claude_desktop_config.json` on Windows.
Merge the snippet into the top-level object (if the file already has
an `mcpServers` key, add `meeting-notetaker` under it; otherwise paste
the whole thing). Fully quit Claude Desktop from the tray and
relaunch.

### Claude Code

```bash
claude mcp add meeting-notetaker '<launcher-command-from-settings>'
```

Or hand-edit `~/.claude.json` (or a per-project `.mcp.json`) the same
way as Claude Desktop.

## Verifying

Once the client is pointed at the server, ask your Claude surface
"list my meetings" or "what's the latest meeting I recorded". It
should call `list_meetings` and return the sessions it finds.

For the web UI path, open `http://127.0.0.1:PORT/healthz` in your
browser; it returns `ok` when the server is up.

## Automation (JSON API)

Any script that reads JSON over HTTP can drive the same operations
without a Claude surface in the loop. Everything under `/api/` is
JSON in and JSON out; the response shape matches the MCP tools.

| Method and path | Tool it mirrors |
| :--- | :--- |
| GET `/api/meetings?limit=&state=&folder_id=&series_id=&contact_id=&topic_id=&has_transcript=&has_notes=` | `list_meetings` |
| GET `/api/meetings/<id>` | `get_meeting` |
| GET `/api/meetings/<id>/transcript` | `get_transcript` |
| GET `/api/meetings/<id>/live_notes` | `get_live_notes` |
| GET `/api/meetings/<id>/notes` | `get_notes` |
| GET `/api/meetings/<id>/prompt?template_name=` | `render_synthesis_prompt` |
| POST `/api/meetings/<id>/save` (`{"notes": "..."}`) | `save_notes` |
| POST `/api/meetings/<id>/template` (`{"template_name": "..."}`) | `set_prompt_template` |
| POST `/api/meetings/<id>/export/obsidian` | `export_meeting_to_obsidian` |
| GET `/api/templates` | `list_templates` |
| GET `/api/templates/<name>` | `get_template` |
| GET `/api/classification` | `list_classification` |

Both JSON POST bodies and form-encoded bodies are accepted on the
mutating routes, so the HTML form on the detail page and a scripted
`curl -d '{"notes": "..."}'` share one handler.

## Available tools

The MCP server exposes these ten tools; each one is schema-advertised
so the Claude surface sees descriptions and types directly. The web
UI and JSON API are built on top of the same functions.

- `list_meetings(limit?, state?, folder_id?, series_id?, contact_id?, topic_id?)`:
  recent sessions, now also filterable by classification
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
- `list_classification()`: series / contacts / topics in use, for
  mapping a user's text onto the id filters above
- `export_meeting_to_obsidian(meeting_id)`: publish the current
  synthesis into the configured Obsidian vault

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
  server and the web UI both honor `MEETING_NOTETAKER_DATA_DIR` the
  same way the GUI does, so a test override works too.
- If the web UI port is in use, the launcher tries the next 20 ports
  before giving up. Watch the stderr line "web UI on
  http://127.0.0.1:PORT/" for the actual port.
- If the Chrome Claude extension cannot reach the page, check Site
  Access on the extension and make sure `http://127.0.0.1:*/*` (or
  the specific port) is allowed.
