"""Model Context Protocol (MCP) server for Meeting Notetaker.

Exposes the user's local meeting data to any MCP-capable Claude surface
(Chrome extension, Claude Desktop, Claude Code) over stdio. See
``mcp/server.py`` for the protocol loop and ``mcp/tools.py`` for the
tool catalog + handlers.
"""
