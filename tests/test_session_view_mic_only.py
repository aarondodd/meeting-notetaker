"""Mic-only / notes-only synthesis gating (#87).

Pins the v0.7.9 fix: a voice-note / walkthrough session whose audio
came out silent (Whisper produced no segments) should still let the
user synthesize from notes alone. Pre-fix, Generate Synthesis Prompt
was gated on has_transcript -- batch returning empty left the session
permanently inert.
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
    STATE_NEW,
    STATE_PROCESSING,
    STATE_RECORDING,
    Session,
)
from meeting_notetaker.ui.session_view import SessionView  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


def _make_session(
    *,
    state: str = STATE_COMPLETE,
    has_transcript: bool = False,
    has_notes: bool = False,
) -> Session:
    return Session(
        id="sess-mic-only",
        title="Voice notes",
        state=state,
        created_at="2026-06-07T12:00:00+00:00",
        has_transcript=has_transcript,
        has_notes=has_notes,
    )


def _state_view(qt_app, *, has_transcript: bool, has_notes: bool, state: str = STATE_COMPLETE):
    """Drive _set_buttons_for_state directly with a real session
    attached. has_session is part of the gate so the session must
    not be None."""
    view = SessionView()
    view._session = _make_session(  # noqa: SLF001
        state=state, has_transcript=has_transcript, has_notes=has_notes,
    )
    view._set_buttons_for_state(  # noqa: SLF001
        state,
        has_transcript=has_transcript,
        has_notes=has_notes,
    )
    return view


# ---- Generate Synthesis Prompt gate -------------------------------------

def test_generate_enabled_with_transcript_only(qt_app):
    """Historical case: transcript present, no notes. Generate is
    enabled."""
    view = _state_view(qt_app, has_transcript=True, has_notes=False)
    assert view._generate_btn.isEnabled()  # noqa: SLF001


def test_generate_enabled_with_notes_only(qt_app):
    """#87: notes present, no transcript (mic-only voice-note session
    where batch returned empty). Generate must be enabled so the user
    can drive synthesis from notes alone."""
    view = _state_view(qt_app, has_transcript=False, has_notes=True)
    assert view._generate_btn.isEnabled()  # noqa: SLF001


def test_generate_enabled_with_both(qt_app):
    """Standard case -- transcript + notes."""
    view = _state_view(qt_app, has_transcript=True, has_notes=True)
    assert view._generate_btn.isEnabled()  # noqa: SLF001


def test_generate_disabled_with_neither(qt_app):
    """No content at all -- Generate stays disabled. There's nothing
    for the LLM to chew on."""
    view = _state_view(qt_app, has_transcript=False, has_notes=False)
    assert not view._generate_btn.isEnabled()  # noqa: SLF001


# ---- Paste Response gate (unchanged behavior; pin for regression) ------

def test_paste_matches_generate_gate(qt_app):
    """Paste and Generate share the same enable gate now; pin the
    parity so a future drift surfaces in tests."""
    for ht, hn in ((True, False), (False, True), (True, True), (False, False)):
        view = _state_view(qt_app, has_transcript=ht, has_notes=hn)
        assert (
            view._generate_btn.isEnabled()  # noqa: SLF001
            == view._paste_btn.isEnabled()  # noqa: SLF001
        ), f"Generate/Paste parity broke for has_transcript={ht}, has_notes={hn}"


# ---- recording lock -----------------------------------------------------

def test_generate_disabled_during_recording(qt_app):
    """Recording in progress -- Generate must stay disabled even with
    notes or transcript already present."""
    view = _state_view(
        qt_app,
        has_transcript=True,
        has_notes=True,
        state=STATE_RECORDING,
    )
    assert not view._generate_btn.isEnabled()  # noqa: SLF001


# ---- transcript placeholder copy ---------------------------------------

def test_placeholder_announces_no_speech_when_batch_empty(qt_app):
    """Recording finished, has_transcript=True (batch ran) but text
    is empty -> placeholder must guide the user to the notes path."""
    view = SessionView()
    session = _make_session(state=STATE_COMPLETE, has_transcript=True)
    view._refresh_transcript_placeholder(session)  # noqa: SLF001
    text = view._transcript_view.placeholderText()  # noqa: SLF001
    assert "No speech detected" in text
    assert "My Notes" in text


def test_placeholder_for_fresh_session(qt_app):
    """Brand-new session -> guidance toward Start / Import."""
    view = SessionView()
    session = _make_session(state=STATE_NEW, has_transcript=False)
    view._refresh_transcript_placeholder(session)  # noqa: SLF001
    text = view._transcript_view.placeholderText()  # noqa: SLF001
    assert "Start a recording" in text
    assert "Import Transcript" in text


def test_placeholder_during_processing(qt_app):
    """Recording finished, batch still running -> generic wait copy."""
    view = SessionView()
    session = _make_session(state=STATE_PROCESSING, has_transcript=False)
    view._refresh_transcript_placeholder(session)  # noqa: SLF001
    text = view._transcript_view.placeholderText()  # noqa: SLF001
    assert "recording finishes" in text
