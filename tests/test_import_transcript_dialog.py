"""ImportTranscriptDialog smoke tests (#80).

Offscreen Qt; no clipboard, no file dialogs (we drive set_initial_body
+ the strip toggle + the speakers table directly so the test doesn't
have to mock QFileDialog/QGuiApplication.clipboard).
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.ui.import_transcript_dialog import ImportTranscriptDialog  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


def test_initial_state_disables_import_button(qt_app):
    dlg = ImportTranscriptDialog()
    try:
        assert dlg._ok_btn.isEnabled() is False  # noqa: SLF001
        assert dlg._speakers_frame.isHidden() is True  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_set_initial_body_normalizes_and_enables_import(qt_app):
    dlg = ImportTranscriptDialog()
    try:
        dlg.set_initial_body(
            "Started transcription\n\nJane: hello\nAaron: hi back\n"
        )
        preview = dlg._preview.toPlainText()  # noqa: SLF001
        assert "Started transcription" not in preview
        assert "Jane: hello" in preview
        assert "Aaron: hi back" in preview
        assert dlg._ok_btn.isEnabled() is True  # noqa: SLF001
        # Two speakers detected -> table is unhidden with 2 rows.
        # Use isHidden (semantic: was setVisible(False) called?) rather
        # than isVisible (semantic: am I + my ancestors all currently
        # shown?), since the dialog itself was never .show()n.
        assert dlg._speakers_frame.isHidden() is False  # noqa: SLF001
        assert dlg._speakers_table.rowCount() == 2  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_strip_toggle_off_preserves_chrome(qt_app):
    dlg = ImportTranscriptDialog()
    try:
        dlg.set_initial_body("Started transcription\nJane: hi\n")
        assert "Started transcription" not in dlg._preview.toPlainText()  # noqa: SLF001
        dlg._strip_chrome.setChecked(False)  # noqa: SLF001
        # Toggling re-renders from the cached source body.
        assert "Started transcription" in dlg._preview.toPlainText()  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_apply_remap_button_rewrites_preview_and_table(qt_app):
    dlg = ImportTranscriptDialog()
    try:
        dlg.set_initial_body("Jane: hi\nAaron: hello\n")
        # Type a new label into Jane's row.
        item = dlg._speakers_table.item(0, 2)  # noqa: SLF001
        assert item is not None
        item.setText("Jane Smith")
        dlg._on_apply_remap()  # noqa: SLF001
        preview = dlg._preview.toPlainText()  # noqa: SLF001
        assert "Jane Smith: hi" in preview
        assert "Aaron: hello" in preview
        # Table re-detected; original label column now shows the new name.
        label_item = dlg._speakers_table.item(0, 0)  # noqa: SLF001
        assert label_item is not None
        assert label_item.text() == "Jane Smith"
    finally:
        dlg.deleteLater()


def test_accept_returns_normalized_body_and_mapping(qt_app):
    dlg = ImportTranscriptDialog()
    try:
        dlg.set_initial_body("Jane: hi\nAaron: hello\n")
        # Type a remap value but don't click Apply -- _on_accept must
        # still pick it up.
        dlg._speakers_table.item(1, 2).setText("Aaron Dodd")  # noqa: SLF001
        dlg._on_accept()  # noqa: SLF001
        assert "Aaron Dodd: hello" in dlg.result_body
        assert dlg.result_mapping == {"Aaron": "Aaron Dodd"}
    finally:
        dlg.deleteLater()


def test_speakers_table_hidden_when_no_speakers_detected(qt_app):
    """A caption-only paste (no Name: prefixes) lights up the preview
    but leaves the speakers table hidden."""
    dlg = ImportTranscriptDialog()
    try:
        dlg.set_initial_body("just a body line\nand another\n")
        assert dlg._ok_btn.isEnabled() is True  # noqa: SLF001
        assert dlg._speakers_frame.isHidden() is True  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_session_title_appears_in_header(qt_app):
    dlg = ImportTranscriptDialog(session_title="Weekly sync")
    try:
        # The header is the first label child; we just check the text
        # contains the title verbatim.
        children = dlg.findChildren(type(dlg._status_label))  # noqa: SLF001
        # _status_label is QLabel too; the header is the other QLabel
        # with "Weekly sync" in it.
        header_texts = [c.text() for c in children if "Weekly sync" in c.text()]
        assert header_texts, [c.text() for c in children]
    finally:
        dlg.deleteLater()
