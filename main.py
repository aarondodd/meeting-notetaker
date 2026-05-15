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

from meeting_notetaker.app import main   # noqa: E402  -- import after truststore inject


if __name__ == "__main__":
    sys.exit(main())
