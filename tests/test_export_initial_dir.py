"""Default-folder resolution for Export save dialogs (v0.7.5).

The resolve_export_initial_dir helper sits between Settings >
Export > Default folder and every QFileDialog.getSaveFileName /
getExistingDirectory call in the export paths. It must:
  - honor the configured folder when set + present on disk;
  - fall back to the per-call default when the configured value is
    blank;
  - also fall back when the configured path no longer exists (e.g.
    the user yanked a USB drive between sessions).
"""
from __future__ import annotations

from pathlib import Path

from meeting_notetaker.utils.export import (
    export_initial_save_path,
    resolve_export_initial_dir,
)


def test_resolve_honors_configured_folder(tmp_path):
    configured = tmp_path / "user-exports"
    configured.mkdir()
    fallback = tmp_path / "session"
    fallback.mkdir()
    assert resolve_export_initial_dir(str(configured), fallback) == configured


def test_resolve_falls_back_when_configured_blank(tmp_path):
    fallback = tmp_path / "session"
    fallback.mkdir()
    assert resolve_export_initial_dir("", fallback) == fallback


def test_resolve_falls_back_when_configured_missing(tmp_path):
    """Stale config pointing at a removed drive shouldn't strand the
    user. Treat missing == unset."""
    fallback = tmp_path / "session"
    fallback.mkdir()
    ghost = tmp_path / "removed-drive"  # never created
    assert resolve_export_initial_dir(str(ghost), fallback) == fallback


def test_resolve_falls_back_when_configured_is_a_file(tmp_path):
    """Defensive: someone hand-edited config.toml to a file path.
    File != dir -> fallback."""
    fallback = tmp_path / "session"
    fallback.mkdir()
    not_a_dir = tmp_path / "actually-a-file.txt"
    not_a_dir.write_text("oops", encoding="utf-8")
    assert resolve_export_initial_dir(str(not_a_dir), fallback) == fallback


def test_resolve_expands_user_in_configured_path(tmp_path, monkeypatch):
    """A configured path starting with ``~`` resolves against HOME so
    a config.toml authored on another machine still finds the user's
    home dir."""
    home = tmp_path / "home"
    home.mkdir()
    sub = home / "exports"
    sub.mkdir()
    monkeypatch.setenv("HOME", str(home))
    fallback = tmp_path / "session"
    fallback.mkdir()
    assert resolve_export_initial_dir("~/exports", fallback) == sub


def test_export_initial_save_path_joins_filename(tmp_path):
    configured = tmp_path / "exports"
    configured.mkdir()
    result = export_initial_save_path(
        str(configured), tmp_path / "fallback", "Meeting -- Audio -- 2026-06-01.mp3",
    )
    assert result == str(
        configured / "Meeting -- Audio -- 2026-06-01.mp3"
    )


def test_export_initial_save_path_falls_back_when_blank(tmp_path):
    fallback = tmp_path / "session"
    fallback.mkdir()
    result = export_initial_save_path("", fallback, "Out.pdf")
    assert result == str(fallback / "Out.pdf")
