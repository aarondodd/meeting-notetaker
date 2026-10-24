"""Local HTTP transport over the MCP tools layer.

The web UI is a second transport (alongside the stdio MCP server) that
calls the same functions in ``meeting_notetaker.mcp.tools``. Chrome
Claude drives the pages here while Claude Desktop and Claude Code
drive the stdio server; both see the identical ten operations.
"""
