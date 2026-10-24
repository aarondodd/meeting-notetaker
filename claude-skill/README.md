# meeting-notetaker Claude skill

A Claude Agent Skill that teaches the Chrome Claude extension to drive
Meeting Notetaker's local web UI: list meetings, read the rendered
prompt, synthesize, and save the result back through the Save form.

Users who import this skill can say "synthesize the last meeting" or
"summarize all meetings in the Platform folder from last week" and the
extension knows the pages, the prompt location, and the Save form
without being manually prompted each time.

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

- Open the app, Settings > Synthesis, click **Start web UI and open in
  browser...**, OR
- Open the app, Settings > Synthesis, tick **Start the web UI
  automatically when Meeting Notetaker launches**, and relaunch. That
  one-time setup makes every future launch start the server
  automatically.

See `docs/mcp-setup.md` in the repository root for the full setup
reference (including the Claude Desktop / Claude Code stdio path).

## When to update the skill

Keep the skill in sync with the HTML surface it drives. Any change to
the route table, the Save form name, the rendered-prompt pane id, or
the filter query parameter names needs a matching edit to `SKILL.md`.
The HTML tests under `tests/test_web_views.py` pin the structural
pieces the skill relies on; a change there is a reliable signal.
