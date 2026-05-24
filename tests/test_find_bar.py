"""FindBar widget -- attach + show + find round-trip.

The find bar is intentionally minimal but its core promise is "Ctrl+F
on the active text widget finds the typed word and shows 'No matches'
when there isn't one". Tests exercise that promise with a real
QPlainTextEdit so the Qt-side `find()` method is on the hook -- if
the bar's flag construction or wrap-around drift, these fail.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QTextCursor  # noqa: E402
from PyQt6.QtWidgets import QApplication, QPlainTextEdit  # noqa: E402

from meeting_notetaker.ui.find_bar import FindBar  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture
def host(qt_app):
    """Real text widget so .find() actually runs against a document."""
    editor = QPlainTextEdit()
    editor.setPlainText(
        "alice and bob discussed MDM rollout.\n"
        "later we agreed on the Informatica migration timeline.\n"
        "see also the prior notes about MDM strategy."
    )
    return editor


@pytest.fixture
def bar(qt_app):
    return FindBar()


def test_show_for_attaches_and_reveals(bar, host):
    bar.show_for(host)
    assert bar.isVisible()
    assert bar._host is host  # noqa: SLF001


def test_show_for_prefills_from_single_word_selection(bar, host):
    cursor = host.textCursor()
    cursor.setPosition(0)
    # Select the literal word "alice" at position 0.
    cursor.movePosition(
        QTextCursor.MoveOperation.NextWord,
        QTextCursor.MoveMode.KeepAnchor,
    )
    host.setTextCursor(cursor)
    bar.show_for(host)
    # The selection includes the trailing space, but the pre-fill only
    # takes single-token selections without spaces. With "alice " the
    # token contains no internal space; we trim trailing whitespace.
    assert "alice" in bar._input.text()  # noqa: SLF001


def test_show_for_skips_multi_line_selection(bar, host):
    """Selecting two lines (which Qt represents with U+2029 paragraph
    separators) must NOT pre-fill -- the find bar handles single-line
    queries only."""
    cursor = host.textCursor()
    cursor.setPosition(0)
    cursor.movePosition(
        QTextCursor.MoveOperation.Down,
        QTextCursor.MoveMode.KeepAnchor,
    )
    host.setTextCursor(cursor)
    bar.show_for(host)
    assert bar._input.text() == ""  # noqa: SLF001


def test_find_navigates_forward_and_loops(bar, host):
    bar.show_for(host)
    bar._input.setText("MDM")
    # Initial search lands on the first MDM.
    bar._search_current_query()  # noqa: SLF001
    first_pos = host.textCursor().position()
    # Next match lands on the second MDM, further into the doc.
    assert bar._find_next()  # noqa: SLF001
    second_pos = host.textCursor().position()
    assert second_pos > first_pos
    # Next again wraps back to the first match (FindBar moves the
    # cursor to the start on miss and retries).
    assert bar._find_next()  # noqa: SLF001
    third_pos = host.textCursor().position()
    assert third_pos == first_pos


def test_find_previous_walks_backward(bar, host):
    bar.show_for(host)
    bar._input.setText("MDM")
    bar._search_current_query()  # noqa: SLF001
    # Land on the first match, then advance to the second.
    bar._find_next()  # noqa: SLF001
    second_pos = host.textCursor().position()
    assert bar._find_previous()  # noqa: SLF001
    first_pos = host.textCursor().position()
    assert first_pos < second_pos


def test_no_matches_status_when_nothing_found(bar, host):
    bar.show_for(host)
    bar._input.setText("doesnotappearanywhereinthecorpus")
    bar._search_current_query()  # noqa: SLF001
    assert bar._status.text() == "No matches"  # noqa: SLF001


def test_case_sensitive_toggle_changes_results(bar, host):
    bar.show_for(host)
    bar._input.setText("mdm")  # lowercase
    # Case insensitive: 2 matches (the document has "MDM" twice).
    bar._case_check.setChecked(False)
    assert bar._find_next()  # noqa: SLF001
    # Flip case-sensitive on -- now lowercase "mdm" finds nothing.
    bar._case_check.setChecked(True)
    bar._search_current_query()  # noqa: SLF001
    assert bar._status.text() == "No matches"  # noqa: SLF001


def test_close_button_emits_closed_signal(bar, host):
    fired = []
    bar.closed.connect(lambda: fired.append(True))
    bar.show_for(host)
    bar.hide_bar()
    assert fired == [True]
    assert not bar.isVisible()


def test_empty_query_clears_status(bar, host):
    bar.show_for(host)
    bar._input.setText("something")  # noqa: SLF001
    bar._search_current_query()  # noqa: SLF001
    bar._input.setText("")  # noqa: SLF001
    bar._search_current_query()  # noqa: SLF001
    assert bar._status.text() == ""  # noqa: SLF001
