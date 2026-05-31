"""Markdown formatting-toolbar tests for LiveNotesWidget (#73 #7).

Pins _set_line_prefix (heading / bullet / quote / task / numbered)
behavior so future edits to the prefix logic break loudly. Pre-#73
the non-heading branch was a bare ``pass``: applying List to a
line like ``    foo`` produced ``-     foo`` (prefix before the
indent) which silently broke nested-list editing.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtGui import QTextCursor  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.ui.live_notes_widget import LiveNotesWidget  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


def _select_all(w: LiveNotesWidget) -> None:
    """Select the entire editor body so the toolbar prefix path
    operates on all lines, not just the current one."""
    cur = w._editor.textCursor()  # noqa: SLF001
    cur.movePosition(QTextCursor.MoveOperation.Start)
    cur.movePosition(
        QTextCursor.MoveOperation.End,
        QTextCursor.MoveMode.KeepAnchor,
    )
    w._editor.setTextCursor(cur)  # noqa: SLF001


# ---- heading branch (H1 / H2 / H3) ---------------------------------


def test_heading_h1_replaces_existing_heading(qt_app):
    w = LiveNotesWidget()
    try:
        w.setPlainText("### Old heading")
        _select_all(w)
        w._set_line_prefix("# ", heading=True)  # noqa: SLF001
        assert w.toPlainText() == "# Old heading"
    finally:
        w.deleteLater()


def test_heading_promotes_plain_line(qt_app):
    w = LiveNotesWidget()
    try:
        w.setPlainText("some text")
        _select_all(w)
        w._set_line_prefix("## ", heading=True)  # noqa: SLF001
        assert w.toPlainText() == "## some text"
    finally:
        w.deleteLater()


def test_heading_strips_leading_whitespace(qt_app):
    """Heading lines don't carry indent; lstrip is intentional so
    the user can't end up with '    # foo'."""
    w = LiveNotesWidget()
    try:
        w.setPlainText("    indented heading-candidate")
        _select_all(w)
        w._set_line_prefix("# ", heading=True)  # noqa: SLF001
        assert w.toPlainText() == "# indented heading-candidate"
    finally:
        w.deleteLater()


# ---- non-heading branch: List / Quote / Task ----------------------


def test_bullet_prefix_added_to_plain_line(qt_app):
    w = LiveNotesWidget()
    try:
        w.setPlainText("an item")
        _select_all(w)
        w._set_line_prefix("- ")  # noqa: SLF001
        assert w.toPlainText() == "- an item"
    finally:
        w.deleteLater()


def test_bullet_prefix_preserves_leading_indentation(qt_app):
    """The post-#73 fix: indent stays at the start of the line +
    the prefix goes between the indent and the content. Pre-fix
    behavior was '-     nested' (prefix before indent)."""
    w = LiveNotesWidget()
    try:
        w.setPlainText("    nested content")
        _select_all(w)
        w._set_line_prefix("- ")  # noqa: SLF001
        assert w.toPlainText() == "    - nested content"
    finally:
        w.deleteLater()


def test_quote_prefix_preserves_tab_indent(qt_app):
    w = LiveNotesWidget()
    try:
        w.setPlainText("\t\tdeeply indented")
        _select_all(w)
        w._set_line_prefix("> ")  # noqa: SLF001
        assert w.toPlainText() == "\t\t> deeply indented"
    finally:
        w.deleteLater()


def test_task_prefix_applies_to_multiple_lines(qt_app):
    w = LiveNotesWidget()
    try:
        w.setPlainText("first\nsecond\nthird")
        _select_all(w)
        w._set_line_prefix("- [ ] ")  # noqa: SLF001
        assert w.toPlainText() == "- [ ] first\n- [ ] second\n- [ ] third"
    finally:
        w.deleteLater()


def test_bullet_prefix_each_indented_line_preserved(qt_app):
    """Mixed indentation across the selection -- every line keeps
    its own leading whitespace after the prefix lands."""
    w = LiveNotesWidget()
    try:
        w.setPlainText("a\n  b\n    c")
        _select_all(w)
        w._set_line_prefix("- ")  # noqa: SLF001
        assert w.toPlainText() == "- a\n  - b\n    - c"
    finally:
        w.deleteLater()


# ---- _numbered_list ----------------------------------------------


def test_numbered_list_enumerates_selected_lines(qt_app):
    w = LiveNotesWidget()
    try:
        w.setPlainText("alpha\nbeta\ngamma")
        _select_all(w)
        w._numbered_list()  # noqa: SLF001
        assert w.toPlainText() == "1. alpha\n2. beta\n3. gamma"
    finally:
        w.deleteLater()


def test_numbered_list_single_line(qt_app):
    w = LiveNotesWidget()
    try:
        w.setPlainText("solo")
        _select_all(w)
        w._numbered_list()  # noqa: SLF001
        assert w.toPlainText() == "1. solo"
    finally:
        w.deleteLater()
