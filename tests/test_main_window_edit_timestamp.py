"""Edit Session Timestamp -- context-menu dialog round-trips UTC ISO.

These tests exercise the pure UI layer: the timestamp-edit dialog parses
the stored UTC ISO seed, displays a local-time editor, and produces a
fresh UTC ISO string back to the controller.

The full right-click -> menu -> dialog -> signal-emit path needs a real
MainWindow + populated session list, which pulls in the entire app
graph. The dialog itself is the load-bearing piece; verifying it in
isolation here is enough for CI.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QDateTime  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.ui.main_window import (  # noqa: E402
    _EditTimestampDialog,
    _parse_iso_to_local,
)


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def test_parse_iso_to_local_handles_zulu_and_offset():
    """Both 'Z' and '+00:00' shapes round-trip to a local naive datetime."""
    a = _parse_iso_to_local("2026-05-19T12:00:00Z")
    b = _parse_iso_to_local("2026-05-19T12:00:00+00:00")
    assert a is not None and b is not None
    assert a.tzinfo is None and b.tzinfo is None
    assert a == b


def test_parse_iso_to_local_returns_none_on_garbage():
    assert _parse_iso_to_local("") is None
    assert _parse_iso_to_local("not-a-date") is None


def test_edit_timestamp_dialog_seeds_from_local(qt_app):
    """The QDateTimeEdit must surface the supplied local datetime so the
    user sees what was stored, not a wallclock reset."""
    seed = datetime(2026, 1, 15, 14, 30, 45)
    dialog = _EditTimestampDialog(initial=seed)
    qdt: QDateTime = dialog._editor.dateTime()
    assert qdt.date().year() == 2026
    assert qdt.date().month() == 1
    assert qdt.date().day() == 15
    assert qdt.time().hour() == 14
    assert qdt.time().minute() == 30
    assert qdt.time().second() == 45


def test_edit_timestamp_dialog_result_round_trips_through_utc(qt_app):
    """result_utc_iso() reads the editor as local time and converts to UTC.
    Round-tripping the seed must land on the same string we would have
    written for that same moment via the calendar-alignment path."""
    seed = datetime(2026, 7, 4, 9, 15, 0)
    dialog = _EditTimestampDialog(initial=seed)
    iso = dialog.result_utc_iso()

    # The result must parse as a UTC ISO string ending in 'Z' and, when
    # converted back to local, return the seed exactly.
    assert iso.endswith("Z")
    parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    assert parsed.tzinfo == timezone.utc
    back_to_local = parsed.astimezone().replace(tzinfo=None)
    assert back_to_local == seed


def test_session_list_column_renders_local_time(monkeypatch):
    """The list's date column converts the stored UTC timestamp into the
    user's local timezone. Aaron flagged this after seeing 21:00 in the
    list for a session he had edited to 11:00 (HST = UTC-10)."""
    import time
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset not available on this platform")
    monkeypatch.setenv("TZ", "Pacific/Honolulu")
    time.tzset()

    from meeting_notetaker.models.session import Session
    from meeting_notetaker.ui.main_window import _session_date_and_title

    s = Session(id="x", title="Test", created_at="2026-05-19T21:00:00Z")
    when, _title = _session_date_and_title(s)
    # 21:00 UTC -> 11:00 HST (UTC-10).
    assert when == "2026-05-19 11:00"


def test_session_list_column_round_trip_with_edit_dialog(qt_app, monkeypatch):
    """End-to-end TZ consistency: what the edit dialog writes for a local
    'YYYY-MM-DD HH:MM' moment is exactly what the list column then renders
    for that same session."""
    import time
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset not available on this platform")
    monkeypatch.setenv("TZ", "Pacific/Honolulu")
    time.tzset()

    from meeting_notetaker.models.session import Session
    from meeting_notetaker.ui.main_window import _session_date_and_title

    seed = datetime(2026, 5, 19, 11, 0, 0)
    dialog = _EditTimestampDialog(initial=seed)
    stored_iso = dialog.result_utc_iso()
    s = Session(id="x", title="t", created_at=stored_iso)
    when, _title = _session_date_and_title(s)
    assert when == "2026-05-19 11:00"
