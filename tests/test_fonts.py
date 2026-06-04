"""Font resolution + Settings Fonts section tests (#80 followup).

Covers:
  - utils.fonts.resolve_editor_font / resolve_preview_font defaults
    and monospace style hint contract
  - Settings Fonts section is registered, alphabetized, and round-
    trips through config on accept
  - The editor font picker only offers fixed-pitch families
  - LiveNotesWidget.apply_fonts pushes the resolved font onto both
    the editor and the inner preview pane
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QFont, QFontDatabase  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.ui.live_notes_widget import LiveNotesWidget  # noqa: E402
from meeting_notetaker.ui.settings_dialog import SettingsDialog  # noqa: E402
from meeting_notetaker.utils.config import Config  # noqa: E402
from meeting_notetaker.utils.fonts import (  # noqa: E402
    resolve_editor_font,
    resolve_preview_font,
)


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


# ---- utils.fonts ---------------------------------------------------------


def test_resolve_editor_font_defaults_set_monospace_style_hint(qt_app):
    font = resolve_editor_font("", 0)
    # Monospace style hint guarantees Qt picks a fixed-pitch substitute
    # even when the family lookup misses on the host -- this is the
    # backstop for the regression where _MarkdownEditor inherited
    # Qt's proportional default.
    assert font.styleHint() == QFont.StyleHint.Monospace
    assert font.fixedPitch() is True


def test_resolve_editor_font_honors_size_when_nonzero(qt_app):
    font = resolve_editor_font("", 13)
    assert font.pointSize() == 13


def test_resolve_editor_font_skips_size_when_zero(qt_app):
    """Zero is the sentinel for 'use the platform / family default'.
    pointSize() returns the QFont's natural value; we just check we
    didn't explicitly override it to a small number."""
    font = resolve_editor_font("Consolas", 0)
    # Don't pin a specific point size -- different Qt builds default
    # differently. The contract is that the result isn't suddenly
    # 0 / negative (which would render invisibly).
    assert font.pointSize() != 0


def test_resolve_editor_font_uses_requested_family_when_provided(qt_app):
    font = resolve_editor_font("Courier New", 11)
    assert font.family() == "Courier New"
    # Even with an explicit family, the monospace style hint stays so
    # the OS substitution path picks a fixed-pitch fallback.
    assert font.styleHint() == QFont.StyleHint.Monospace


def test_resolve_preview_font_no_style_hint_imposed(qt_app):
    font = resolve_preview_font("", 0)
    # Preview gets the application default; we don't impose monospace
    # because the preview body is mixed content (headings, prose,
    # tables, code blocks each carry their own intrinsic styling).
    assert font.styleHint() != QFont.StyleHint.Monospace


def test_resolve_preview_font_honors_explicit_family(qt_app):
    font = resolve_preview_font("Verdana", 13)
    assert font.family() == "Verdana"
    assert font.pointSize() == 13


# ---- Settings Fonts section ---------------------------------------------


def test_fonts_section_registered_in_alphabetical_order(qt_app):
    cfg = Config()
    dlg = SettingsDialog(cfg)
    try:
        labels = [
            dlg._nav.item(i).text()  # noqa: SLF001
            for i in range(dlg._nav.count())  # noqa: SLF001
        ]
        assert "Fonts" in labels
        # Sits alphabetically: Calendar < Export < Fonts < Integrations
        f_idx = labels.index("Fonts")
        assert labels[f_idx - 1] == "Export"
    finally:
        dlg.deleteLater()


def test_editor_font_picker_lists_only_monospace_families(qt_app):
    """The editor's family picker filters via QFontDatabase
    isFixedPitch. The auto sentinel comes first with empty data."""
    cfg = Config()
    dlg = SettingsDialog(cfg)
    try:
        picker = dlg._editor_font_picker  # noqa: SLF001
        # First entry is the auto sentinel.
        assert picker.itemData(0) == ""
        assert "auto" in picker.itemText(0).lower()
        # Every subsequent entry must be a monospace family. Sample
        # by reading the picker's data values and asking QFontDatabase.
        for i in range(1, picker.count()):
            fam = picker.itemData(i)
            assert isinstance(fam, str) and fam
            assert QFontDatabase.isFixedPitch(fam), (
                f"editor picker offered non-monospace family {fam!r}"
            )
    finally:
        dlg.deleteLater()


def test_preview_font_picker_offers_unfiltered_family_list(qt_app):
    """Preview face is unrestricted; the picker must include at least
    one proportional family that the editor picker excludes (otherwise
    the filter on the editor side would be meaningless)."""
    cfg = Config()
    dlg = SettingsDialog(cfg)
    try:
        editor_families = {
            dlg._editor_font_picker.itemData(i)  # noqa: SLF001
            for i in range(dlg._editor_font_picker.count())  # noqa: SLF001
        }
        preview_families = {
            dlg._preview_font_picker.itemData(i)  # noqa: SLF001
            for i in range(dlg._preview_font_picker.count())  # noqa: SLF001
        }
        # Preview is strict superset of editor (proportional families
        # show up only in preview).
        assert preview_families >= editor_families
        only_preview = preview_families - editor_families
        # At least one proportional family must exist on the host;
        # the offscreen test platform ships several.
        assert len(only_preview) >= 1
    finally:
        dlg.deleteLater()


def test_fonts_round_trip_through_accept(qt_app):
    """Editing the pickers + spin boxes and clicking Save writes the
    values back to the Config instance."""
    cfg = Config()
    dlg = SettingsDialog(cfg)
    try:
        # Pick a known monospace family if present; otherwise stay on
        # the auto sentinel (the picker's filtering means we can't
        # hard-code a face that exists on every host).
        picker = dlg._editor_font_picker  # noqa: SLF001
        if picker.count() > 1:
            picker.setCurrentIndex(1)
            expected_family = picker.itemData(1)
        else:
            expected_family = ""
        dlg._editor_font_size.setValue(14)  # noqa: SLF001
        dlg._preview_font_size.setValue(13)  # noqa: SLF001
        dlg._on_accept()  # noqa: SLF001
        assert cfg.ui.editor_font_family == expected_family
        assert cfg.ui.editor_font_size == 14
        assert cfg.ui.preview_font_size == 13
    finally:
        dlg.deleteLater()


def test_fonts_section_seeds_from_saved_config(qt_app):
    """A Config that already has values surfaces them as the picker's
    initial selection -- so reopening Settings doesn't reset the
    user's choice."""
    cfg = Config()
    cfg.ui.editor_font_size = 16
    cfg.ui.preview_font_size = 12
    dlg = SettingsDialog(cfg)
    try:
        assert dlg._editor_font_size.value() == 16  # noqa: SLF001
        assert dlg._preview_font_size.value() == 12  # noqa: SLF001
    finally:
        dlg.deleteLater()


# ---- LiveNotesWidget.apply_fonts ----------------------------------------


def test_live_notes_widget_apply_fonts_pushes_to_editor_and_preview(qt_app):
    """Both the editable side (the _MarkdownEditor) and the preview
    side (the inner MarkdownPreview wrapped by PreviewWithToc) must
    pick up the new font when apply_fonts fires."""
    w = LiveNotesWidget()
    try:
        editor_font = QFont("Consolas")
        editor_font.setStyleHint(QFont.StyleHint.Monospace)
        editor_font.setPointSize(13)
        preview_font = QFont("Arial")
        preview_font.setPointSize(11)
        w.apply_fonts(editor_font, preview_font)
        # Editor (the inner _MarkdownEditor).
        assert w._editor.font().family() == "Consolas"  # noqa: SLF001
        assert w._editor.font().pointSize() == 13  # noqa: SLF001
        # Preview: PreviewWithToc.preview() returns the inner
        # MarkdownPreview; the font lives on that surface.
        inner = w._preview.preview()  # noqa: SLF001
        assert inner.font().family() == "Arial"
        assert inner.font().pointSize() == 11
    finally:
        w.deleteLater()


def test_live_notes_widget_editor_has_fixedPitch_after_apply(qt_app):
    """Regression for the headline bug: _MarkdownEditor was inheriting
    the platform proportional default. After apply_fonts the editor
    must report fixedPitch=True."""
    w = LiveNotesWidget()
    try:
        font = resolve_editor_font("", 0)
        w.apply_fonts(font, resolve_preview_font("", 0))
        assert w._editor.font().fixedPitch() is True  # noqa: SLF001
    finally:
        w.deleteLater()
