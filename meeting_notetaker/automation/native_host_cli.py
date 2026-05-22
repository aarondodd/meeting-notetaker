"""Argparse wrapper for tests + manual invocation.

In production, ``main.py --native-host`` is the entry point Chrome's
native-messaging manifest points at; tests use this module instead so
they don't have to load every meeting_notetaker dependency just to
spawn a host process.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .native_host import run


def main() -> int:
    parser = argparse.ArgumentParser(prog="meeting_notetaker.native_host_cli")
    parser.add_argument(
        "--handshake-file",
        required=True,
        type=Path,
        help="path to the bridge.json the running app wrote",
    )
    parser.add_argument(
        "--extension-id",
        default="",
        help="Chrome extension ID that invoked the host (informational)",
    )
    args = parser.parse_args()
    return run(args.handshake_file, extension_id=args.extension_id)


if __name__ == "__main__":  # pragma: no cover -- entry point
    sys.exit(main())
