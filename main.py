"""Meeting Notetaker -- thin entry point.

Injects truststore (when available) BEFORE any other imports so corporate
MITM proxies that re-sign TLS to huggingface.co (Netskope, Zscaler, etc.)
work via the OS cert store rather than failing with CERTIFICATE_VERIFY_FAILED.
"""
from __future__ import annotations

import sys


def _inject_truststore() -> None:
    """Make Python's ssl module use the OS certificate store.

    Silent no-op if truststore is not installed, or on platforms where it
    is not packaged in requirements.txt. The only downside on a clean
    network is no extra trust, which is what we would have anyway.
    """
    try:
        import truststore
    except ImportError:
        return
    truststore.inject_into_ssl()


_inject_truststore()


def _run_check_deps() -> int:
    """Build-gate self-test. Runs every dependency check, prints a
    plain-text report, exits 0 if all OK and 1 if any MISSING.

    Imports the check module locally so this path doesn't drag the UI
    layer in -- the whole point is for a frozen .exe to surface a bundling
    miss before the UI ever tries to load.
    """
    from meeting_notetaker.utils.dependency_check import (
        Status,
        format_report,
        run_checks,
        summary,
    )
    grouped = run_checks()
    print(format_report(grouped))
    counts = summary(grouped)
    return 1 if counts[Status.MISSING] > 0 else 0


def _run_mcp_server() -> int:
    """Launch the stdio MCP server.

    Invoked from a Claude Desktop or Claude Code MCP client config as:

        <path-to-meeting-notetaker.exe> --mcp-server

    No GUI starts. The server reads JSON-RPC messages from stdin and
    writes responses to stdout; logs go to stderr. The Chrome Claude
    extension does not consume stdio MCP configs; it drives the web UI
    at --web-ui instead.
    """
    from meeting_notetaker.mcp.server import main
    return main()


def _run_web_ui() -> int:
    """Launch the local web UI.

    Invoked as ``<path-to-meeting-notetaker.exe> --web-ui`` on the
    user's work machine. Binds 127.0.0.1 only and opens the default
    browser so the Chrome Claude extension (running in that browser)
    can read the page, drive the synthesis workflow, and POST the
    result back. The underlying operations are the same ten MCP tools
    that --mcp-server serves over stdio.
    """
    from meeting_notetaker.web.server import main
    return main()


if __name__ == "__main__":
    if "--check-deps" in sys.argv[1:]:
        sys.exit(_run_check_deps())
    if "--mcp-server" in sys.argv[1:]:
        sys.exit(_run_mcp_server())
    if "--web-ui" in sys.argv[1:]:
        sys.exit(_run_web_ui())
    from meeting_notetaker.app import main   # noqa: E402  -- import after truststore inject
    sys.exit(main())
