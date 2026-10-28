# Meeting Notetaker User Guide

Day-to-day use of Meeting Notetaker. For project background and the
rationale ("why this exists"), see the top-level
[README](../../README.md). For architecture and internals (recording
pipeline, diarization, service layer), see the
[Technical Details](../../README.md#technical-details) section of the
README.

## Pages

- [Getting started](getting-started.md): requirements and the
  installation paths (Windows installer, PowerShell bootstrap, local
  build, source checkout).
- [Recording and notes](recording.md): running a session, the My
  Notes tab, screen capture, playback with transcript sync, exporting
  the recording, transcribing a failed recording, and importing a
  transcript when there is no recording to run.
- [Synthesis](synthesis.md): the two synthesis paths. The manual
  clipboard hand-off that works with any chatbot, and the MCP service
  layer (stdio for Claude Desktop and Claude Code, loopback web UI for
  the Chrome Claude extension).
- [Publishing notes](publishing.md): saving the finished synthesis to
  Notion, Confluence, or Obsidian.
- [Speaker identification](speakers.md): the on-device speaker
  recognition pipeline, labeling, correcting, and tuning the
  thresholds.
- [Settings and backups](settings.md): the full Settings reference
  table, plus the backup and restore flow.

For MCP setup specifically (one-page config snippets for Claude
Desktop, Claude Code, and the Chrome Claude extension), see
[../mcp-setup.md](../mcp-setup.md).
