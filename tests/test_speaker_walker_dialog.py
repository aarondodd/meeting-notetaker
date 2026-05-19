"""SpeakerWalkerDialog Qt offscreen tests."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.ui.speaker_walker_dialog import (
    SpeakerWalkerDialog,
    SpeakerWalkerEntry,
)


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _entry(cluster_id: int, *, name=None, suggestions=None) -> SpeakerWalkerEntry:
    return SpeakerWalkerEntry(
        cluster_id=cluster_id,
        current_name=name,
        example_lines=[f"[00:00:01] sample line for cluster {cluster_id + 1}"],
        centroid=np.full(8, 0.1 * cluster_id, dtype=np.float32),
        match_similarity=0.81 if name else None,
        suggestions=list(suggestions or []),
    )


def test_label_mode_dialog_lists_entries(qt_app):
    entries = [_entry(0), _entry(1)]
    dialog = SpeakerWalkerDialog(entries, mode="label", session_title="Test")
    assert "Label Unknown Speakers" in dialog.windowTitle()
    assert "Test" in dialog.windowTitle()
    assert len(dialog._cards) == 2


def test_review_mode_dialog_uses_review_title(qt_app):
    entries = [_entry(0, name="Alice")]
    dialog = SpeakerWalkerDialog(entries, mode="review")
    assert "Review Speakers" in dialog.windowTitle()
    assert len(dialog._cards) == 1


def test_decisions_returns_user_choices(qt_app):
    entries = [_entry(0), _entry(1)]
    dialog = SpeakerWalkerDialog(entries, mode="label")
    # Simulate the user typing a name into the first card and leaving second blank.
    dialog._cards[0]._name_combo.setCurrentText("Bob")
    dialog._cards[1]._name_combo.setCurrentText("")
    decisions = dialog.decisions()
    assert decisions[0].name == "Bob"
    assert decisions[0].cluster_id == 0
    assert decisions[1].name is None
    assert decisions[1].cluster_id == 1


def test_skip_clears_name_and_keeps_should_forget_false(qt_app):
    entries = [_entry(0, name="Alice")]
    dialog = SpeakerWalkerDialog(entries, mode="review")
    dialog._cards[0]._name_combo.setCurrentText("Some new name")
    dialog._cards[0]._on_skip_clicked()
    decision = dialog._cards[0].decision()
    assert decision.name is None
    assert decision.should_forget is False


def test_forget_in_review_mode_sets_should_forget(qt_app):
    entries = [_entry(0, name="Alice")]
    dialog = SpeakerWalkerDialog(entries, mode="review")
    # The card exposes _forget_btn only in review mode for already-named clusters.
    assert dialog._cards[0]._forget_btn is not None
    dialog._cards[0]._on_forget_clicked()
    decision = dialog._cards[0].decision()
    assert decision.name is None
    assert decision.should_forget is True


def test_forget_button_absent_in_label_mode(qt_app):
    entries = [_entry(0)]
    dialog = SpeakerWalkerDialog(entries, mode="label")
    assert dialog._cards[0]._forget_btn is None


def test_empty_entries_still_renders(qt_app):
    """Pop-with-no-entries shouldn't crash and offers just an OK button."""
    dialog = SpeakerWalkerDialog([], mode="label")
    assert dialog._cards == []
    # Should not raise on close.
    dialog.accept()


def test_suggestions_populate_combo(qt_app):
    entries = [_entry(0, suggestions=["Alice", "Bob", "Carol"])]
    dialog = SpeakerWalkerDialog(entries, mode="label")
    combo = dialog._cards[0]._name_combo
    items = [combo.itemText(i) for i in range(combo.count())]
    assert "Alice" in items
    assert "Bob" in items
    assert "Carol" in items


def test_review_mode_preselects_current_name(qt_app):
    entries = [_entry(0, name="Alice", suggestions=["Alice", "Bob"])]
    dialog = SpeakerWalkerDialog(entries, mode="review")
    assert dialog._cards[0]._name_combo.currentText() == "Alice"


def test_invalid_mode_raises():
    with pytest.raises(ValueError):
        SpeakerWalkerDialog([_entry(0)], mode="bogus")
