# meeting-notetaker Claude skill

A Claude Agent Skill that teaches the Chrome Claude extension to drive
Meeting Notetaker's local web UI: list meetings, read the rendered
prompt, synthesize, save the result back through the Save form, and
publish the finished note to Obsidian, Notion, or Confluence through
the per-meeting Export dropdown.

Users who import this skill can say "synthesize the last meeting" or
"summarize all meetings in the Platform series from last week" and the
extension knows the pages, the prompt location, the Save form, and the
Export dropdown without being manually prompted each time.

## What is here

- `SKILL.md`: the skill itself. The YAML frontmatter carries the
  `name` and `description`; the body is the instructions Claude reads
  when the skill activates.

## Install into the Chrome Claude extension

1. Zip the `claude-skill/` directory so the archive's root is
   `SKILL.md` (not a nested directory). On Linux or macOS:
   `cd claude-skill && zip ../meeting-notetaker-skill.zip SKILL.md`.
   On Windows PowerShell:
   `Compress-Archive -Path .\claude-skill\SKILL.md -DestinationPath .\meeting-notetaker-skill.zip`.
2. Open the Chrome Claude extension.
3. Open the extension's settings and find the Skills section.
4. Import the zip you built in step 1.
5. In the extension's Site Access settings, grant access to
   `http://127.0.0.1:*/*` (or the specific port the server is on).
   Chrome treats loopback like any other host; without this the
   extension cannot read the local pages.

## Prerequisites in the app

Before the skill can do anything useful, the Meeting Notetaker web UI
has to be running. Either:

- Open the app, Settings > Synthesis via MCP, tick **Enable synthesis
  via MCP**, click **Start web UI and open in browser...**, OR
- Open the app, Settings > Synthesis via MCP, tick **Enable synthesis
  via MCP**, tick **Start the web UI automatically when Meeting
  Notetaker launches**, and relaunch. That one-time setup makes every
  future launch start the server automatically.

To expose the Export dropdown inside the skill's workflow, tick each
destination you want available under **Settings > Synthesis via MCP >
Web UI export destinations** (Obsidian, Notion, Confluence). A
destination stays disabled until its own integration is configured
and verified under Settings > Integrations.

See `docs/mcp-setup.md` in the repository root for the full setup
reference (including the Claude Desktop / Claude Code stdio path).

## When to update the skill

Keep the skill in sync with the HTML surface it drives. Any change to
the route table, the Save form name, the rendered-prompt pane id, the
filter query parameter names, or the Export dropdown's destination
names needs a matching edit to `SKILL.md`. The HTML tests under
`tests/test_web_views.py` pin the structural pieces the skill relies
on, and `tests/test_claude_skill.py` pins the skill's own anchors; a
change on either side surfaces as a test diff.
