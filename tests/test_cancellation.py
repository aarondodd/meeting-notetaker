"""Cancellation primitives for export workers (#60).

The encoders accept a `should_cancel: Callable[[], bool]` and call
`raise_if_cancelled` at checkpoint boundaries; when it returns True
an `ExportCancelled` exception unwinds the export pipeline. The
worker catches it, the orchestrator deletes partial output, the
dialog reports cancellation distinctly from real errors.
"""
from __future__ import annotations

import pytest

from meeting_notetaker.utils.cancellation import (
    ExportCancelled,
    raise_if_cancelled,
)


def test_raise_if_cancelled_noop_when_callable_none():
    """should_cancel=None is the no-cancellation path used by unit
    tests and one-shot CLI exports; must not raise."""
    raise_if_cancelled(None)


def test_raise_if_cancelled_noop_when_callable_returns_false():
    raise_if_cancelled(lambda: False)


def test_raise_if_cancelled_raises_when_true():
    with pytest.raises(ExportCancelled) as exc:
        raise_if_cancelled(lambda: True, "video encode")
    assert "video encode" in str(exc.value)


def test_export_cancelled_is_exception_subclass():
    """ExportCancelled inherits from Exception so an existing
    `except Exception` cleanup block catches it; but it is a
    distinct class so callers that care can match on it
    specifically."""
    assert issubclass(ExportCancelled, Exception)


def test_export_cancelled_default_description():
    """Without an explicit description the message reads cleanly."""
    with pytest.raises(ExportCancelled) as exc:
        raise_if_cancelled(lambda: True)
    assert "cancelled" in str(exc.value).lower()
