"""Pin the SessionView processing-state label phrasing.

Regression for the misleading "you can synthesize now" claim during
batch refinement when live transcription is off (v0.6.5+ default).
Under that config the transcript file is empty until batch
completes, so the claim that synthesis is available is wrong.

The label picks between two phrasings based on whether a live
transcript actually exists:

  - has_live_transcript=True  -> "Refining transcript -- you can
    synthesize now" (the historical message, correct when live
    captions populated the pane during recording)
  - has_live_transcript=False -> "Refining transcript..." (the
    quieter form; no claim about synthesis availability)
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.models.session import (  # noqa: E402
    STATE_COMPLETE,
    STATE_PROCESSING,
    STATE_RECORDING,
    Session,
)
from meeting_notetaker.ui.session_view import SessionView, _pretty_state  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


def _make_session(*, has_transcript: bool, state: str = STATE_PROCESSING) -> Session:
    return Session(
        id="sess-label",
        title="Sample",
        state=state,
        created_at="2026-06-04T12:00:00+00:00",
        has_transcript=has_transcript,
    )


# ---- pure-function level pins -------------------------------------------


def test_pretty_state_processing_with_live_transcript():
    out = _pretty_state(STATE_PROCESSING, has_live_transcript=True)
    assert out == "Refining transcript -- you can synthesize now"


def test_pretty_state_processing_without_live_transcript():
    out = _pretty_state(STATE_PROCESSING, has_live_transcript=False)
    assert out == "Refining transcript..."
    # Stronger contract: the "synthesize now" claim must NOT appear
    # when there's no live transcript to synthesize from. Pins the
    # regression directly.
    assert "synthesize" not in out


def test_pretty_state_processing_default_no_synthesize_claim():
    """A bare call without the kwarg must not falsely advertise
    synthesis -- the default is the safe phrasing."""
    out = _pretty_state(STATE_PROCESSING)
    assert "synthesize" not in out


def test_pretty_state_recording_and_complete_unchanged():
    """Other states keep their original phrasing -- the fix is
    scoped to PROCESSING."""
    assert _pretty_state(STATE_RECORDING) == "Recording"
    assert _pretty_state(STATE_COMPLETE) == ""


# ---- SessionView-level pins ---------------------------------------------


def test_session_view_label_shows_synthesize_now_when_live_transcript_present(qt_app):
    """set_session was called with a non-empty live transcript: the
    historical 'you can synthesize now' message is correct."""
    sv = SessionView()
    try:
        sv.set_session(
            _make_session(has_transcript=True),
            transcript="[00:00:00] Me: hello\n",
            notes="", previous_notes_paths=[], live_notes="",
        )
        assert (
            sv._state_label.text()  # noqa: SLF001
            == "Refining transcript -- you can synthesize now"
        )
    finally:
        sv.deleteLater()


def test_session_view_label_quiet_when_no_live_transcript(qt_app):
    """v0.6.5+ default scenario: live transcription off, no live
    captions written at Stop. The label must NOT claim synthesis is
    available -- the transcript file is empty until batch finishes."""
    sv = SessionView()
    try:
        sv.set_session(
            _make_session(has_transcript=False),
            transcript="",
            notes="", previous_notes_paths=[], live_notes="",
        )
        text = sv._state_label.text()  # noqa: SLF001
        assert text == "Refining transcript..."
        assert "synthesize" not in text
    finally:
        sv.deleteLater()


def test_session_view_update_state_quiet_when_no_live_transcript(qt_app):
    """The update_state path (state transitions during a loaded
    session) follows the same rule as set_session. Regression for
    Aaron's reported scenario: clicking back into the session during
    refinement showed the misleading message."""
    sv = SessionView()
    try:
        # Load a fresh session in RECORDING state with no transcript;
        # then flip to PROCESSING the way controller.state_changed does.
        sv.set_session(
            _make_session(has_transcript=False, state=STATE_RECORDING),
            transcript="",
            notes="", previous_notes_paths=[], live_notes="",
        )
        sv.update_state(STATE_PROCESSING)
        assert sv._state_label.text() == "Refining transcript..."  # noqa: SLF001
    finally:
        sv.deleteLater()


def test_session_view_update_state_synthesize_now_when_live_transcript(qt_app):
    """Same path with has_transcript=True keeps the historical
    message -- the live captions branch is unchanged."""
    sv = SessionView()
    try:
        sv.set_session(
            _make_session(has_transcript=True, state=STATE_RECORDING),
            transcript="[00:00:00] Me: live caption arrived\n",
            notes="", previous_notes_paths=[], live_notes="",
        )
        sv.update_state(STATE_PROCESSING)
        assert (
            sv._state_label.text()  # noqa: SLF001
            == "Refining transcript -- you can synthesize now"
        )
    finally:
        sv.deleteLater()
