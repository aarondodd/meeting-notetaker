"""Tests for the Obsidian vault discovery helpers (#96).

Pure-Python; tmp-vault fixtures only. No PyQt, no real Obsidian
install required.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from meeting_notetaker.integrations import obsidian_vault as ov


def _write_registry(obsidian_dir: Path, vaults: dict) -> None:
    obsidian_dir.mkdir(parents=True, exist_ok=True)
    (obsidian_dir / "obsidian.json").write_text(
        json.dumps({"vaults": vaults}), encoding="utf-8",
    )


def test_list_registered_vaults_returns_empty_when_registry_missing(tmp_path):
    out = ov.list_registered_vaults(tmp_path / "nope")
    assert out == []


def test_list_registered_vaults_sorted_by_recency(tmp_path):
    a = tmp_path / "vault-a"
    b = tmp_path / "vault-b"
    a.mkdir()
    b.mkdir()
    _write_registry(tmp_path / "obsidian", {
        "v1": {"path": str(a), "ts": 100},
        "v2": {"path": str(b), "ts": 999},
    })
    out = ov.list_registered_vaults(tmp_path / "obsidian")
    assert [v.name for v in out] == ["vault-b", "vault-a"]


def test_list_registered_vaults_skips_entries_without_path(tmp_path):
    _write_registry(tmp_path / "obsidian", {
        "v1": {"ts": 100},
        "v2": {"path": str(tmp_path / "real"), "ts": 200},
    })
    (tmp_path / "real").mkdir()
    out = ov.list_registered_vaults(tmp_path / "obsidian")
    assert len(out) == 1
    assert out[0].name == "real"


def test_vault_name_for_path_uses_registry(tmp_path):
    vault = tmp_path / "Vault One"
    vault.mkdir()
    _write_registry(tmp_path / "obsidian", {
        "v1": {"path": str(vault), "ts": 100},
    })
    assert ov.vault_name_for_path(
        vault, tmp_path / "obsidian",
    ) == "Vault One"


def test_vault_name_for_path_falls_back_to_basename(tmp_path):
    vault = tmp_path / "Unregistered"
    vault.mkdir()
    _write_registry(tmp_path / "obsidian", {})
    assert ov.vault_name_for_path(
        vault, tmp_path / "obsidian",
    ) == "Unregistered"


def test_is_vault_registered(tmp_path):
    vault = tmp_path / "MyVault"
    vault.mkdir()
    obs_dir = tmp_path / "obsidian"
    _write_registry(obs_dir, {})
    assert not ov.is_vault_registered(vault, obs_dir)
    _write_registry(obs_dir, {"v1": {"path": str(vault), "ts": 1}})
    assert ov.is_vault_registered(vault, obs_dir)


def test_vault_is_valid(tmp_path):
    vault = tmp_path / "MyVault"
    assert not ov.vault_is_valid(vault)
    vault.mkdir()
    assert ov.vault_is_valid(vault)


def test_read_attachment_folder_path_none_when_missing(tmp_path):
    vault = tmp_path / "MyVault"
    vault.mkdir()
    assert ov.read_attachment_folder_path(vault) is None


def test_read_attachment_folder_path_set(tmp_path):
    vault = tmp_path / "MyVault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / ".obsidian" / "app.json").write_text(
        json.dumps({"attachmentFolderPath": "Attachments/Inbox"}),
        encoding="utf-8",
    )
    assert ov.read_attachment_folder_path(vault) == "Attachments/Inbox"


def test_read_attachment_folder_path_default_returns_none(tmp_path):
    vault = tmp_path / "MyVault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / ".obsidian" / "app.json").write_text(
        json.dumps({"attachmentFolderPath": "./"}),
        encoding="utf-8",
    )
    assert ov.read_attachment_folder_path(vault) is None


def test_read_attachment_folder_path_malformed_returns_none(tmp_path):
    vault = tmp_path / "MyVault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / ".obsidian" / "app.json").write_text("not json", encoding="utf-8")
    assert ov.read_attachment_folder_path(vault) is None


def test_read_daily_notes_config_core_plugin(tmp_path):
    vault = tmp_path / "v"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / ".obsidian" / "daily-notes.json").write_text(
        json.dumps({"folder": "Journal", "format": "YYYY-MM-DD"}),
        encoding="utf-8",
    )
    daily = ov.read_daily_notes_config(vault)
    assert daily is not None
    assert daily.folder == "Journal"
    assert daily.filename_format == "YYYY-MM-DD"


def test_read_daily_notes_config_periodic_notes_plugin(tmp_path):
    vault = tmp_path / "v"
    plugin_dir = vault / ".obsidian" / "plugins" / "periodic-notes"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "data.json").write_text(json.dumps({
        "daily": {"enabled": True, "folder": "Daily", "format": "YYYY/MM/DD"},
    }), encoding="utf-8")
    daily = ov.read_daily_notes_config(vault)
    assert daily is not None
    assert daily.folder == "Daily"
    assert daily.filename_format == "YYYY/MM/DD"


def test_read_daily_notes_config_returns_none_when_no_files(tmp_path):
    vault = tmp_path / "v"
    vault.mkdir()
    assert ov.read_daily_notes_config(vault) is None


@pytest.mark.parametrize("fmt,when,expected", [
    ("YYYY-MM-DD", date(2026, 6, 9), "2026-06-09"),
    ("YY-M-D", date(2026, 6, 9), "26-6-9"),
    ("MMMM D, YYYY", date(2026, 1, 5), "January 5, 2026"),
    ("MMM DD", date(2026, 12, 1), "Dec 01"),
    ("dddd YYYY-MM-DD", date(2026, 6, 9), "Tuesday 2026-06-09"),
    ("ddd", date(2026, 6, 9), "Tue"),
    ("plain text only", date(2026, 6, 9), "plain text only"),
])
def test_render_moment_format(fmt, when, expected):
    assert ov.render_moment_format(fmt, when) == expected


def test_daily_note_path_for_uses_folder_and_format(tmp_path):
    vault = tmp_path / "v"
    vault.mkdir()
    daily = ov.DailyNotesConfig(folder="Journal", filename_format="YYYY-MM-DD")
    out = ov.daily_note_path_for(vault, daily, date(2026, 6, 9))
    assert out == vault / "Journal" / "2026-06-09.md"


def test_daily_note_path_for_no_folder(tmp_path):
    vault = tmp_path / "v"
    vault.mkdir()
    daily = ov.DailyNotesConfig(folder="", filename_format="YYYY-MM-DD")
    out = ov.daily_note_path_for(vault, daily, date(2026, 6, 9))
    assert out == vault / "2026-06-09.md"
