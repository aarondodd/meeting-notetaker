"""Tests for the SessionPromptEditDialog (#90).

The dialog itself is pure-UI; the rendering + dispatch happens in
MainApp's _on_edit_and_send handler (which is exercised end-to-end on
Windows). Here we cover:

  * Body initialization from rendered_prompt.
  * Header content shows session title + template name.
  * Primary button label tracks automation mode.
  * Send/Copy populates result_payload with the edited body.
  * Empty body refuses to accept.
  * Save as new template... persists via prompts module.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QDialog  # noqa: E402

from meeting_notetaker.ui.session_prompt_dialog import (  # noqa: E402
    SessionPromptEditDialog,
    SessionPromptEditResult,
)
from meeting_notetaker.utils import prompts as prompts_mod  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


# ---- initialization -----------------------------------------------------

def test_dialog_loads_rendered_body(qt_app):
    body = "synthesize the following meeting transcript:\n[ ... ]"
    dlg = SessionPromptEditDialog(
        rendered_prompt=body,
        session_title="Test Session",
        template_name="standup",
        automation_enabled=True,
    )
    assert dlg._editor.toPlainText() == body  # noqa: SLF001


def test_dialog_header_shows_session_and_template(qt_app):
    dlg = SessionPromptEditDialog(
        rendered_prompt="x",
        session_title="Q3 Roadmap Review",
        template_name="standup",
        automation_enabled=True,
    )
    # Header is a QLabel sitting in the outer layout; grab via children.
    from PyQt6.QtWidgets import QLabel  # noqa: PLC0415
    labels = dlg.findChildren(QLabel)
    header_text = " ".join(lbl.text() for lbl in labels)
    assert "Q3 Roadmap Review" in header_text
    assert "standup" in header_text


def test_primary_button_says_send_in_automation_mode(qt_app):
    dlg = SessionPromptEditDialog(
        rendered_prompt="x", session_title="t",
        template_name="default", automation_enabled=True,
    )
    assert "Send" in dlg._primary_btn.text()  # noqa: SLF001


def test_primary_button_says_copy_when_automation_off(qt_app):
    dlg = SessionPromptEditDialog(
        rendered_prompt="x", session_title="t",
        template_name="default", automation_enabled=False,
    )
    assert "Copy" in dlg._primary_btn.text()  # noqa: SLF001


# ---- accept / cancel ---------------------------------------------------

def test_send_populates_result_payload(qt_app):
    dlg = SessionPromptEditDialog(
        rendered_prompt="original", session_title="t",
        template_name="default", automation_enabled=True,
    )
    dlg._editor.setPlainText("edited body")  # noqa: SLF001
    dlg._on_send_clicked()  # noqa: SLF001
    assert dlg.result() == QDialog.DialogCode.Accepted
    assert isinstance(dlg.result_payload, SessionPromptEditResult)
    assert dlg.result_payload.action == "send"
    assert dlg.result_payload.edited_body == "edited body"


def test_copy_populates_result_payload(qt_app):
    dlg = SessionPromptEditDialog(
        rendered_prompt="original", session_title="t",
        template_name="default", automation_enabled=False,
    )
    dlg._editor.setPlainText("edited body")  # noqa: SLF001
    dlg._on_copy_clicked()  # noqa: SLF001
    assert dlg.result_payload.action == "copy"
    assert dlg.result_payload.edited_body == "edited body"


def test_send_refuses_empty_body(qt_app):
    """A user clearing the editor by mistake should get a warning,
    not a no-op send that dispatches nothing."""
    dlg = SessionPromptEditDialog(
        rendered_prompt="x", session_title="t",
        template_name="default", automation_enabled=True,
    )
    dlg._editor.setPlainText("")  # noqa: SLF001
    # Patch the QMessageBox.warning so the test isn't blocked on a modal.
    from PyQt6.QtWidgets import QMessageBox  # noqa: PLC0415
    seen = []
    original = QMessageBox.warning
    QMessageBox.warning = lambda *args, **kwargs: seen.append(args)
    try:
        dlg._on_send_clicked()  # noqa: SLF001
    finally:
        QMessageBox.warning = original
    assert seen, "expected a warning dialog for empty body"
    assert dlg.result_payload is None


def test_copy_refuses_empty_body(qt_app):
    dlg = SessionPromptEditDialog(
        rendered_prompt="x", session_title="t",
        template_name="default", automation_enabled=False,
    )
    dlg._editor.setPlainText("")  # noqa: SLF001
    from PyQt6.QtWidgets import QMessageBox  # noqa: PLC0415
    seen = []
    original = QMessageBox.warning
    QMessageBox.warning = lambda *args, **kwargs: seen.append(args)
    try:
        dlg._on_copy_clicked()  # noqa: SLF001
    finally:
        QMessageBox.warning = original
    assert seen
    assert dlg.result_payload is None


def test_cancel_leaves_result_payload_none(qt_app):
    dlg = SessionPromptEditDialog(
        rendered_prompt="x", session_title="t",
        template_name="default", automation_enabled=True,
    )
    dlg.reject()
    assert dlg.result_payload is None


# ---- stats footer -------------------------------------------------------

def test_stats_footer_updates_on_edit(qt_app):
    dlg = SessionPromptEditDialog(
        rendered_prompt="initial", session_title="t",
        template_name="default", automation_enabled=True,
    )
    dlg._editor.setPlainText("new\nbody")  # noqa: SLF001
    stats = dlg._stats_label.text()  # noqa: SLF001
    assert "2" in stats  # 2 lines
    assert "8" in stats  # 8 chars (new\nbody is 8)


# ---- save as new template -----------------------------------------------

def test_save_as_template_persists_via_prompts_module(
    qt_app, isolated_data_dir, monkeypatch,
):
    """The save-as-template button drives prompts.create_prompt with
    the validated name and current body. Patch QInputDialog so the
    test isn't blocked on a modal."""
    from PyQt6.QtWidgets import QInputDialog, QMessageBox  # noqa: PLC0415
    monkeypatch.setattr(
        QInputDialog, "getText",
        lambda *args, **kwargs: ("custom-edit", True),
    )
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: None)
    dlg = SessionPromptEditDialog(
        rendered_prompt="body to save", session_title="t",
        template_name="default", automation_enabled=True,
    )
    dlg._on_save_as_template()  # noqa: SLF001
    tpl = prompts_mod.get_template("custom-edit")
    assert tpl is not None
    assert tpl.body == "body to save"


def test_save_as_template_refuses_invalid_name(
    qt_app, isolated_data_dir, monkeypatch,
):
    """An unsafe name (slash, leading underscore, etc.) should surface
    the validation error rather than silently writing somewhere
    unexpected."""
    from PyQt6.QtWidgets import QInputDialog, QMessageBox  # noqa: PLC0415
    monkeypatch.setattr(
        QInputDialog, "getText",
        lambda *args, **kwargs: ("../escape", True),
    )
    seen_warnings = []
    monkeypatch.setattr(
        QMessageBox, "warning",
        lambda *args, **kwargs: seen_warnings.append(args),
    )
    dlg = SessionPromptEditDialog(
        rendered_prompt="body", session_title="t",
        template_name="default", automation_enabled=True,
    )
    dlg._on_save_as_template()  # noqa: SLF001
    assert seen_warnings, "expected validation warning"
    assert prompts_mod.get_template("../escape") is None
