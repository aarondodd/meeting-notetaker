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
    """Launch the GUI with the local web UI enabled this run.

    `<path-to-meeting-notetaker.exe> --web-ui` used to spawn a bare
    HTTP server with no GUI, which left the single-instance lock
    uninvolved (so the user could end up with the GUI plus a
    standalone --web-ui exe at once) and had no on-screen surface for
    the actual URL when something went wrong. The flag now routes
    through the GUI with FORCE_WEB_UI_THIS_RUN set, so there is one
    process, the lock prevents a second launch, and the resolved URL
    is visible in Settings > Synthesis.

    Pure headless operation (no Qt) is still available by calling
    ``meeting_notetaker.web.server:main`` directly from a Python
    entry point; the flag is wired to the GUI path because that is
    the shape every user hits.
    """
    import meeting_notetaker.app as _app
    _app.FORCE_WEB_UI_THIS_RUN = True
    return _app.main()


if __name__ == "__main__":
    if "--check-deps" in sys.argv[1:]:
        sys.exit(_run_check_deps())
    if "--mcp-server" in sys.argv[1:]:
        sys.exit(_run_mcp_server())
    if "--web-ui" in sys.argv[1:]:
        # Qt doesn't recognize --web-ui and would log a warning for
        # every unknown dash flag. Pull it out of argv before the
        # GUI entry point passes argv to QApplication.
        sys.argv = [a for a in sys.argv if a != "--web-ui"]
        sys.exit(_run_web_ui())
    from meeting_notetaker.app import main   # noqa: E402  -- import after truststore inject
    sys.exit(main())
