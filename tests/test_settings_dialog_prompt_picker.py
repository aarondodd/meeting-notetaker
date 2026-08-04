"""Regression for #129: Settings > Edit Prompts crashed on close because
_open_prompt_editor referenced self._default_template (nonexistent) instead
of self._default_template_picker. The picker-refresh has since been extracted
into _refresh_default_template_picker(); this test calls that helper directly
to catch a future rename or typo that would reintroduce the AttributeError.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.ui.settings_dialog import SettingsDialog  # noqa: E402
from meeting_notetaker.utils.config import Config  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


def test_refresh_default_template_picker_does_not_raise(qt_app):
    """The helper called after the prompt editor closes must not
    reference a stale attribute name. Prior to #129 this raised
    AttributeError: 'SettingsDialog' object has no attribute
    '_default_template'."""
    cfg = Config()
    dlg = SettingsDialog(cfg)
    try:
        # Should complete without raising.
        dlg._refresh_default_template_picker()  # noqa: SLF001
        # And the picker should still be populated with at least the
        # bundled default templates.
        assert dlg._default_template_picker.count() > 0  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_refresh_default_template_picker_preserves_selection(qt_app):
    """When the current selection still exists in the refreshed list,
    it stays selected. Regression for the intended UX of the
    Settings > Edit Prompts flow."""
    cfg = Config()
    dlg = SettingsDialog(cfg)
    try:
        picker = dlg._default_template_picker  # noqa: SLF001
        if picker.count() < 2:
            pytest.skip("need at least two templates to exercise preservation")
        picker.setCurrentIndex(1)
        selected = picker.currentData()
        dlg._refresh_default_template_picker()  # noqa: SLF001
        assert picker.currentData() == selected
    finally:
        dlg.deleteLater()
