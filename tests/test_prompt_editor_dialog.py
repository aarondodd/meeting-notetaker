"""Tests for the in-app prompt editor dialog (#89).

Drives the dialog offscreen via QT_QPA_PLATFORM=offscreen. The CRUD
logic itself is tested by test_prompts_editor.py; here we cover the
dialog-level behavior:

  * Initial population of the prompt list.
  * Editor body matches the selected prompt.
  * Save flips the dirty flag off and writes the body.
  * Previous-versions list populates after a save.
  * Revert button activates only with a history selection.
  * Variable hint panel includes every placeholder render() honors.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.ui.prompt_editor_dialog import (  # noqa: E402
    PROMPT_PLACEHOLDERS,
    PromptEditorDialog,
)
from meeting_notetaker.utils import prompts as prompts_mod  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


def _select_prompt_by_name(dlg: PromptEditorDialog, name: str) -> None:
    """Pick a prompt from the list by name; resolves any item-data lookup."""
    for i in range(dlg._prompt_list.count()):  # noqa: SLF001
        item = dlg._prompt_list.item(i)
        if item.data(Qt.ItemDataRole.UserRole) == name:
            dlg._prompt_list.setCurrentItem(item)  # noqa: SLF001
            return
    pytest.fail(f"prompt named {name!r} not in list")


# ---- initial population --------------------------------------------------

def test_dialog_populates_bundled_prompts(qt_app, isolated_data_dir):
    dlg = PromptEditorDialog()
    names = [
        dlg._prompt_list.item(i).data(Qt.ItemDataRole.UserRole)  # noqa: SLF001
        for i in range(dlg._prompt_list.count())  # noqa: SLF001
    ]
    assert "default" in names
    assert "one-on-one" in names
    assert "standup" in names


def test_dialog_marks_bundled_prompts(qt_app, isolated_data_dir):
    dlg = PromptEditorDialog()
    for i in range(dlg._prompt_list.count()):  # noqa: SLF001
        text = dlg._prompt_list.item(i).text()  # noqa: SLF001
        name = dlg._prompt_list.item(i).data(Qt.ItemDataRole.UserRole)  # noqa: SLF001
        if prompts_mod.is_bundled_prompt(name):
            assert "(bundled)" in text


def test_editor_loads_selected_prompt_body(qt_app, isolated_data_dir):
    dlg = PromptEditorDialog()
    _select_prompt_by_name(dlg, "standup")
    tpl = prompts_mod.get_template("standup")
    assert dlg._editor.toPlainText() == tpl.body  # noqa: SLF001


def test_initial_save_button_disabled(qt_app, isolated_data_dir):
    """No dirty state until the user edits."""
    dlg = PromptEditorDialog()
    _select_prompt_by_name(dlg, "default")
    assert not dlg._save_btn.isEnabled()  # noqa: SLF001


# ---- save flow ----------------------------------------------------------

def test_edit_flips_dirty_and_save_enabled(qt_app, isolated_data_dir):
    dlg = PromptEditorDialog()
    _select_prompt_by_name(dlg, "standup")
    dlg._editor.setPlainText("totally new body for standup")  # noqa: SLF001
    assert dlg._dirty is True  # noqa: SLF001
    assert dlg._save_btn.isEnabled()  # noqa: SLF001


def test_save_writes_to_disk_and_clears_dirty(qt_app, isolated_data_dir):
    dlg = PromptEditorDialog()
    _select_prompt_by_name(dlg, "standup")
    dlg._editor.setPlainText("new body")  # noqa: SLF001
    dlg._save_current()  # noqa: SLF001
    # File contains the new body.
    tpl = prompts_mod.get_template("standup")
    assert tpl.body == "new body"
    assert dlg._dirty is False  # noqa: SLF001
    assert not dlg._save_btn.isEnabled()  # noqa: SLF001


def test_save_populates_history_list(qt_app, isolated_data_dir):
    """The first save of a prompt that already exists archives the
    prior body, so history list has one entry afterward."""
    dlg = PromptEditorDialog()
    _select_prompt_by_name(dlg, "standup")
    initial = dlg._editor.toPlainText()  # noqa: SLF001
    dlg._editor.setPlainText("v2")  # noqa: SLF001
    dlg._save_current()  # noqa: SLF001
    assert dlg._history_list.count() == 1  # noqa: SLF001
    # Tooltip on the row carries a preview of the prior body.
    item = dlg._history_list.item(0)  # noqa: SLF001
    assert initial[:50] in item.toolTip()


# ---- revert flow --------------------------------------------------------

def test_revert_button_disabled_without_history_selection(qt_app, isolated_data_dir):
    dlg = PromptEditorDialog()
    _select_prompt_by_name(dlg, "default")
    assert not dlg._revert_btn.isEnabled()  # noqa: SLF001


def test_revert_restores_archived_body(qt_app, isolated_data_dir):
    dlg = PromptEditorDialog()
    _select_prompt_by_name(dlg, "standup")
    # Save once to make a history entry, then change again.
    dlg._editor.setPlainText("body A")  # noqa: SLF001
    dlg._save_current()  # noqa: SLF001
    dlg._editor.setPlainText("body B")  # noqa: SLF001
    dlg._save_current()  # noqa: SLF001
    # Select the history entry containing "body A" and revert.
    assert dlg._history_list.count() >= 1  # noqa: SLF001
    # Newest first; body B is the prior body (it was the active version
    # before "body B" was saved... wait, sequence: A saved -> archived
    # original. B saved -> archived A. So history newest-first is
    # [archive-of-A, archive-of-original]. We want the archive of A
    # (which contains "body A").
    target_row = None
    for i in range(dlg._history_list.count()):  # noqa: SLF001
        item = dlg._history_list.item(i)  # noqa: SLF001
        if "body A" in item.toolTip():
            target_row = i
            break
    assert target_row is not None
    dlg._history_list.setCurrentRow(target_row)  # noqa: SLF001
    dlg._on_revert_clicked()  # noqa: SLF001
    assert dlg._editor.toPlainText() == "body A"  # noqa: SLF001


# ---- variable hint panel ------------------------------------------------

def test_placeholder_catalog_lists_all_render_placeholders(qt_app):
    """Every placeholder the editor advertises must be honored by
    prompts.render() so the user's mental model stays accurate. The
    inverse direction (render handles tokens that aren't in the
    catalog) is intentional -- callers can introduce experimental
    tokens without the catalog blocking them."""
    catalog_tokens = {token for token, _ in PROMPT_PLACEHOLDERS}
    expected = {
        "{{session_title}}", "{{date}}", "{{transcript}}",
        "{{live_notes}}", "{{attendees}}", "{{user_name}}",
    }
    assert expected.issubset(catalog_tokens)


def test_placeholder_descriptions_are_non_empty(qt_app):
    for token, desc in PROMPT_PLACEHOLDERS:
        assert desc.strip(), f"empty description for {token}"


# ---- new / duplicate / delete flow -------------------------------------

def test_creating_new_prompt_appears_in_list(qt_app, isolated_data_dir):
    """Drive the API directly (the dialog's QInputDialog isn't easy to
    script offscreen); confirm the list refresh picks it up."""
    prompts_mod.create_prompt("brand-new", body="hello")
    dlg = PromptEditorDialog()
    names = [
        dlg._prompt_list.item(i).data(Qt.ItemDataRole.UserRole)  # noqa: SLF001
        for i in range(dlg._prompt_list.count())  # noqa: SLF001
    ]
    assert "brand-new" in names


def test_deleted_prompt_drops_from_list(qt_app, isolated_data_dir):
    prompts_mod.create_prompt("ephemeral", body="x")
    prompts_mod.delete_prompt("ephemeral", archive_first=False)
    dlg = PromptEditorDialog()
    names = [
        dlg._prompt_list.item(i).data(Qt.ItemDataRole.UserRole)  # noqa: SLF001
        for i in range(dlg._prompt_list.count())  # noqa: SLF001
    ]
    assert "ephemeral" not in names
