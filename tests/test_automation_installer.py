"""Installer plumbing: extension extraction, native-messaging-host
manifest generation, deterministic extension ID validation.

Registry writes are Windows-only and aren't exercised here (the tests
run on Linux and macOS dev hosts); ``installation_state()`` on those
platforms is expected to return False for the registry fields.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from meeting_notetaker.automation.installer import (
    EXTENSION_ID,
    NATIVE_HOST_NAME,
    _derive_extension_id_from_key,
    extract_extension,
    install,
    installation_state,
    is_fully_installed,
    uninstall,
    write_native_host_manifest,
)


def test_extension_id_matches_manifest_key(isolated_data_dir):
    """Pin the contract: the EXTENSION_ID constant must be derivable
    from the manifest's `key` field. If someone regenerates the key
    without updating EXTENSION_ID, this catches it."""
    from meeting_notetaker.utils.paths import resource_path
    manifest = json.loads(
        (resource_path("extension") / "manifest.json").read_text(encoding="utf-8")
    )
    derived = _derive_extension_id_from_key(manifest["key"])
    assert derived == EXTENSION_ID


def test_derive_extension_id_format():
    """Format invariant: 32 chars, each in [a-p]. Chrome's key->id
    algorithm has this exact shape -- a regression here likely means
    we got the byte-to-nibble mapping wrong."""
    derived = _derive_extension_id_from_key(
        "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA"  # short truncated input
        "iweEMkjwAUd00+UAMffryJJY8g3vrx7wrXciDO7ad0g="
    )
    assert len(derived) == 32
    for ch in derived:
        assert "a" <= ch <= "p", f"bad char {ch!r} in derived id {derived}"


def test_extract_extension_copies_manifest(isolated_data_dir, tmp_path):
    """Extraction must land manifest.json + content/ + icons/ at the
    target. Anything missing == build packaging issue."""
    dest = tmp_path / "ext"
    result = extract_extension(dest=dest)
    assert result == dest
    assert (dest / "manifest.json").exists()
    assert (dest / "background.js").exists()
    assert (dest / "content" / "claude.js").exists()
    assert (dest / "icons" / "icon128.png").exists()


def test_extract_extension_replaces_existing(isolated_data_dir, tmp_path):
    """Re-extracting overwrites the prior install. Stale files from
    older versions must not survive."""
    dest = tmp_path / "ext"
    extract_extension(dest=dest)
    stale = dest / "stale.txt"
    stale.write_text("from a prior version")
    extract_extension(dest=dest)
    assert not stale.exists()


def test_extract_extension_missing_source_raises(tmp_path):
    """If the bundled source is missing (build packaging bug), surface
    that loudly rather than installing an empty folder."""
    fake_source = tmp_path / "no-extension-here"
    fake_source.mkdir()
    with pytest.raises(FileNotFoundError, match="no manifest"):
        extract_extension(source=fake_source, dest=tmp_path / "ext")


def test_native_host_manifest_shape(isolated_data_dir, tmp_path):
    """The Chrome native-messaging spec requires name, path, type,
    and allowed_origins. Wrong shape -> Chrome silently ignores the
    manifest and connectNative fails with a confusing error."""
    host_exe = tmp_path / "fake-mn.exe"
    host_exe.touch()
    manifest_path = tmp_path / "host.json"
    write_native_host_manifest(host_executable=host_exe, manifest_path=manifest_path)
    data = json.loads(manifest_path.read_text())
    assert data["name"] == NATIVE_HOST_NAME
    assert data["type"] == "stdio"
    assert data["allowed_origins"] == [f"chrome-extension://{EXTENSION_ID}/"]
    # Path points at the generated wrapper, not the bare exe -- Chrome
    # can't pass CLI args directly so the wrapper invokes the exe with
    # --native-host on our behalf.
    wrapper = Path(data["path"])
    assert wrapper.exists()
    assert "--native-host" in wrapper.read_text()


def test_install_state_off_by_default(isolated_data_dir):
    state = installation_state()
    assert state["extension_extracted"] is False
    assert state["native_manifest_written"] is False
    assert is_fully_installed() is False


def test_install_orchestration_lands_artifacts(isolated_data_dir, tmp_path):
    """End-to-end install on Linux: extension folder + manifest land
    (registry write is no-op off Windows). is_fully_installed should
    flip True on non-Windows once those two artifacts exist."""
    host_exe = tmp_path / "fake-mn.exe"
    host_exe.touch()
    install(host_executable=host_exe)
    state = installation_state()
    assert state["extension_extracted"] is True
    assert state["native_manifest_written"] is True
    if sys.platform.startswith("win"):
        # On Windows the registry write should have landed; HKCU is
        # writable without admin.
        assert state["registry_chrome"] is True
    else:
        assert is_fully_installed() is True


def test_uninstall_removes_manifest_keeps_extension(isolated_data_dir, tmp_path):
    """Path 3 contract: the user owns the chrome://extensions side.
    Uninstall should pull the native-host manifest + registry but
    leave the extracted extension files in place so the user can
    decide when to remove from Chrome."""
    host_exe = tmp_path / "fake-mn.exe"
    host_exe.touch()
    install(host_executable=host_exe)
    assert installation_state()["native_manifest_written"] is True
    uninstall()
    state = installation_state()
    assert state["native_manifest_written"] is False
    assert state["extension_extracted"] is True


def test_uninstall_with_keep_false_removes_extension(isolated_data_dir, tmp_path):
    host_exe = tmp_path / "fake-mn.exe"
    host_exe.touch()
    install(host_executable=host_exe)
    uninstall(keep_extension_files=False)
    state = installation_state()
    assert state["extension_extracted"] is False
    assert state["native_manifest_written"] is False
