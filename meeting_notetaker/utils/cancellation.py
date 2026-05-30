"""Cancellation primitives for long-running export work (#60).

Pattern:

    from meeting_notetaker.utils.cancellation import (
        ExportCancelled, raise_if_cancelled,
    )

    def export_something(*, should_cancel=None):
        for frame_idx in range(n):
            raise_if_cancelled(should_cancel)
            encode_one(frame_idx)

The caller wires a ``threading.Event.is_set`` or equivalent zero-arg
callable as ``should_cancel``. Encoders raise ``ExportCancelled`` at
the next checkpoint when the flag is set; the worker thread catches
it, cleans up partial output, and reports the cancellation.

Separate from ``Exception`` (no generic-error catch swallows it) so
the cancel path is unambiguous through every layer of the export
pipeline.
"""
from __future__ import annotations

from typing import Callable, Optional


class ExportCancelled(Exception):
    """Raised when a should_cancel callable signals cancellation.

    Carries a short description of what was running so the worker's
    log line is informative; the user-facing message is constant
    ("Export cancelled.").
    """


def raise_if_cancelled(
    should_cancel: Optional[Callable[[], bool]],
    description: str = "export",
) -> None:
    """Raise ExportCancelled when the caller's flag is set.

    No-op when ``should_cancel`` is None (the in-app smoke / unit
    paths don't need cancellation plumbing). Safe to call inside
    tight loops -- the only work is one function call + one bool
    check when the callable is supplied.
    """
    if should_cancel is None:
        return
    if should_cancel():
        raise ExportCancelled(f"{description} cancelled")
