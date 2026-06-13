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


# ---- installed_extension_version (#102 bug 7) --------------------------


def test_installed_extension_version_empty_when_not_extracted(isolated_data_dir):
    from meeting_notetaker.automation.installer import installed_extension_version
    # Fresh data dir; extension hasn't been extracted yet.
    assert installed_extension_version() == ""


def test_installed_extension_version_reads_manifest(isolated_data_dir, tmp_path):
    """After extract_extension lands the manifest in extension_dir,
    installed_extension_version returns the version field."""
    import json
    from meeting_notetaker.automation.installer import (
        extract_extension, installed_extension_version,
    )
    src = tmp_path / "bundled"
    src.mkdir()
    (src / "manifest.json").write_text(
        json.dumps({
            "manifest_version": 3,
            "name": "x",
            "version": "1.2.3",
            "key": "irrelevant",
        }),
        encoding="utf-8",
    )
    # extract_extension validates the key/id pair; bypass by calling
    # the read directly against the destination after a manual copy.
    from meeting_notetaker.utils.paths import extension_dir
    dest = extension_dir()
    import shutil
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    assert installed_extension_version() == "1.2.3"


def test_installed_extension_version_empty_on_malformed_json(
    isolated_data_dir,
):
    """A corrupt manifest is treated as 'not installed' so the
    skew check is a no-op rather than crashing the alert path."""
    from meeting_notetaker.automation.installer import installed_extension_version
    from meeting_notetaker.utils.paths import extension_dir
    dest = extension_dir()
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "manifest.json").write_text(
        "{not valid json", encoding="utf-8",
    )
    assert installed_extension_version() == ""


def test_installed_extension_version_empty_when_field_missing(
    isolated_data_dir,
):
    import json
    from meeting_notetaker.automation.installer import installed_extension_version
    from meeting_notetaker.utils.paths import extension_dir
    dest = extension_dir()
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "name": "x"}),
        encoding="utf-8",
    )
    assert installed_extension_version() == ""


# ---- bundled_extension_version (#102 bug 7 follow-up) ------------------


def test_bundled_extension_version_reads_resources_manifest(isolated_data_dir):
    """The shipped bundle's manifest is at resources/extension/manifest.json
    inside the package. The helper must read that path regardless of
    whether the on-disk extension_dir copy has been extracted."""
    from meeting_notetaker.automation.installer import bundled_extension_version
    # The repo's bundled manifest must exist + carry a non-empty version
    # string (currently 0.7.11). The exact value drifts with each
    # extension change; just check shape.
    version = bundled_extension_version()
    assert version  # non-empty
    parts = version.split(".")
    assert len(parts) >= 2
    assert all(p.isdigit() for p in parts)


def test_bundled_extension_version_independent_of_extension_dir(
    isolated_data_dir,
):
    """bundled_extension_version reads from package resources, NOT
    from the per-user extension_dir copy. Verify by deliberately
    staging a different version in extension_dir and confirming
    the bundled lookup is unaffected."""
    import json
    from meeting_notetaker.automation.installer import (
        bundled_extension_version, installed_extension_version,
    )
    from meeting_notetaker.utils.paths import extension_dir
    dest = extension_dir()
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "manifest.json").write_text(
        json.dumps({
            "manifest_version": 3, "name": "x", "version": "0.0.1",
        }),
        encoding="utf-8",
    )
    assert installed_extension_version() == "0.0.1"
    # Bundled is whatever ships in resources/; deliberately NOT 0.0.1.
    bundled = bundled_extension_version()
    assert bundled != "0.0.1"
    assert bundled  # non-empty


# ---- find_chrome_executable + open_chrome_extensions_page (#102 bug 7) -


def test_find_chrome_executable_returns_none_on_non_windows():
    """The lookup is documented Windows-only; on Linux / macOS the
    helper bails out cleanly so the version-skew alert can fall
    through to its text-only instructions."""
    import sys as _sys
    from meeting_notetaker.automation.installer import find_chrome_executable
    if _sys.platform.startswith("win"):
        # Skip cleanly on Windows test runs -- the function may
        # actually return a path there.
        return
    assert find_chrome_executable() is None


def test_open_chrome_extensions_page_returns_false_when_no_chrome(
    monkeypatch,
):
    """When find_chrome_executable returns None, the launch helper
    is a no-op that returns False. The skew-check caller treats
    False as 'fall back to status-bar text instructions'."""
    from meeting_notetaker.automation import installer as automation_installer
    monkeypatch.setattr(
        automation_installer, "find_chrome_executable", lambda: None,
    )
    assert automation_installer.open_chrome_extensions_page() is False


def test_open_chrome_extensions_page_launches_with_extension_id(
    monkeypatch, tmp_path,
):
    """When Chrome IS found, Popen is called with chrome.exe + the
    deep-link URL that scrolls to our extension's tile."""
    from meeting_notetaker.automation import installer as automation_installer
    fake_chrome = tmp_path / "chrome.exe"
    fake_chrome.write_bytes(b"fake")
    monkeypatch.setattr(
        automation_installer, "find_chrome_executable",
        lambda: fake_chrome,
    )
    captured: dict = {}

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

        class _Stub:
            pass
        return _Stub()

    monkeypatch.setattr(
        automation_installer.subprocess, "Popen", fake_popen,
    )
    ok = automation_installer.open_chrome_extensions_page()
    assert ok is True
    assert captured["args"][0] == str(fake_chrome)
    url = captured["args"][1]
    assert url.startswith("chrome://extensions/?id=")
    assert automation_installer.EXTENSION_ID in url


def test_open_chrome_extensions_page_returns_false_on_popen_failure(
    monkeypatch, tmp_path,
):
    from meeting_notetaker.automation import installer as automation_installer
    fake_chrome = tmp_path / "chrome.exe"
    fake_chrome.write_bytes(b"x")
    monkeypatch.setattr(
        automation_installer, "find_chrome_executable",
        lambda: fake_chrome,
    )

    def boom(*_args, **_kwargs):
        raise OSError("Access denied")

    monkeypatch.setattr(
        automation_installer.subprocess, "Popen", boom,
    )
    assert automation_installer.open_chrome_extensions_page() is False
