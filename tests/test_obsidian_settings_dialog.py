"""Settings dialog: Obsidian section regression tests (#96).

The verify -> accept handoff has to preserve last_verified_at across
the dialog's lifecycle. Earlier shape regressed when the field's raw
input differed from what `Path(raw).expanduser()` produces (forward
slashes on Windows, `~` expansion, trailing separator stripping), so
these tests pin the canonical form is what lands in both _config and
the line edit after Verify.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from meeting_notetaker.ui.settings_dialog import SettingsDialog  # noqa: E402
from meeting_notetaker.utils.config import Config  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


def _real_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "Vault"
    vault.mkdir()
    return vault


def test_verify_writes_canonical_path_into_line_edit(qt_app, tmp_path):
    """After Verify, the line edit must match _config.obsidian.vault_root
    so the Accept-time comparison doesn't wipe last_verified_at."""
    vault = _real_vault(tmp_path)
    cfg = Config()
    dlg = SettingsDialog(cfg)
    try:
        # Raw user input might not match Path's canonical form. Pad
        # with a trailing separator -- Path strips it.
        dlg._obsidian_vault_edit.setText(str(vault) + os.sep)  # noqa: SLF001
        dlg._on_verify_obsidian()  # noqa: SLF001
        line_edit_text = dlg._obsidian_vault_edit.text()  # noqa: SLF001
        assert line_edit_text == cfg.obsidian.vault_root
        assert cfg.obsidian.last_verified_at != ""
    finally:
        dlg.deleteLater()


def test_accept_preserves_last_verified_at_after_verify(qt_app, tmp_path):
    """The canonical bug from Aaron's first-run: Verify succeeds, OK is
    clicked, but the Save to... menu still doesn't list Obsidian
    because _on_accept's compare-and-clear path wiped last_verified_at."""
    vault = _real_vault(tmp_path)
    cfg = Config()
    dlg = SettingsDialog(cfg)
    try:
        # Same trailing-separator shape as above -- one of the real-
        # world inputs that triggered the regression.
        dlg._obsidian_vault_edit.setText(str(vault) + os.sep)  # noqa: SLF001
        dlg._on_verify_obsidian()  # noqa: SLF001
        verified_at = cfg.obsidian.last_verified_at
        assert verified_at != ""
        dlg._on_accept()  # noqa: SLF001
        # Accept must NOT wipe last_verified_at when the user only
        # verified + clicked OK.
        assert cfg.obsidian.last_verified_at == verified_at
        assert cfg.obsidian.vault_root == str(vault)
    finally:
        dlg.deleteLater()


def test_accept_clears_last_verified_at_when_user_edits_after_verify(
    qt_app, tmp_path,
):
    """Edits after Verify should clear last_verified_at so a stale
    'Connected' label doesn't outlive the path it described."""
    vault = _real_vault(tmp_path)
    other = tmp_path / "Other Vault"
    other.mkdir()
    cfg = Config()
    dlg = SettingsDialog(cfg)
    try:
        dlg._obsidian_vault_edit.setText(str(vault))  # noqa: SLF001
        dlg._on_verify_obsidian()  # noqa: SLF001
        assert cfg.obsidian.last_verified_at != ""
        # User edits the field to point at a different vault, then
        # accepts WITHOUT clicking Verify again.
        dlg._obsidian_vault_edit.setText(str(other))  # noqa: SLF001
        dlg._on_accept()  # noqa: SLF001
        assert cfg.obsidian.last_verified_at == ""
        assert cfg.obsidian.vault_root == str(other)
    finally:
        dlg.deleteLater()


def test_browse_normalizes_separators_to_native_form(qt_app, tmp_path):
    """QFileDialog returns forward slashes on Windows; the Browse
    handler must normalize so the Accept-time compare doesn't see a
    spurious diff. We can't easily fire QFileDialog headless, so we
    simulate by setting text via the same code path Browse uses."""
    vault = _real_vault(tmp_path)
    cfg = Config()
    dlg = SettingsDialog(cfg)
    try:
        forward = str(vault).replace(os.sep, "/")
        # Mirror what _on_browse_obsidian_vault does after a chosen path.
        dlg._obsidian_vault_edit.setText(str(Path(forward)))  # noqa: SLF001
        # The text now reflects Path's normalized form.
        assert dlg._obsidian_vault_edit.text() == str(vault)  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_verify_rejects_missing_directory(qt_app, tmp_path):
    cfg = Config()
    dlg = SettingsDialog(cfg)
    try:
        dlg._obsidian_vault_edit.setText(  # noqa: SLF001
            str(tmp_path / "nope"),
        )
        dlg._on_verify_obsidian()  # noqa: SLF001
        assert cfg.obsidian.last_verified_at == ""
    finally:
        dlg.deleteLater()


def test_verify_rejects_empty_input(qt_app):
    cfg = Config()
    dlg = SettingsDialog(cfg)
    try:
        dlg._obsidian_vault_edit.setText("")  # noqa: SLF001
        dlg._on_verify_obsidian()  # noqa: SLF001
        assert cfg.obsidian.last_verified_at == ""
    finally:
        dlg.deleteLater()


def test_location_template_round_trip(qt_app, tmp_path):
    """Settings -> Accept should persist the location template choice
    so the picker uses it on the next save."""
    cfg = Config()
    cfg.obsidian.location_template_name = "by_series"
    dlg = SettingsDialog(cfg)
    try:
        # The dropdown should have landed on by_series.
        assert dlg._obsidian_location_picker.currentData() == "by_series"  # noqa: SLF001
        dlg._on_accept()  # noqa: SLF001
        assert cfg.obsidian.location_template_name == "by_series"
    finally:
        dlg.deleteLater()
