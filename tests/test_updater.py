"""Unit tests for the GitHub-releases updater.

Covers everything that can be exercised without an actual network round
trip or actually running an installer: version parsing, the weekly-
interval gate, the last-check timestamp persistence, asset selection
from a release payload, and the upgrade() flow (with the network and
subprocess calls mocked out). The end-to-end installer launch is
inherently Windows-only and is not exercised in CI; the unit boundaries
verify everything up to subprocess.Popen.
"""
from __future__ import annotations

import io
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from meeting_notetaker.utils import updater


# ---- version parsing -------------------------------------------------------


def test_parse_version_simple():
    assert updater.parse_version("1.2.3") == (1, 2, 3)


def test_parse_version_strips_v_prefix():
    assert updater.parse_version("v0.4.0") == (0, 4, 0)
    assert updater.parse_version("V0.4.0") == (0, 4, 0)


def test_parse_version_invalid_returns_none():
    assert updater.parse_version("") is None
    assert updater.parse_version("abc") is None
    assert updater.parse_version("1.x.0") is None


def test_parse_version_two_component():
    assert updater.parse_version("1.2") == (1, 2)


def test_parse_version_strips_prerelease_suffix():
    assert updater.parse_version("0.5.0-dev") == (0, 5, 0)
    assert updater.parse_version("v1.2.3-rc1") == (1, 2, 3)
    assert updater.parse_version("1.0.0+build42") == (1, 0, 0)


def test_is_newer_version_prerelease_compares_equal_to_final():
    # A dev build of x.y.z should regard the released x.y.z as not newer
    # (parses to same tuple). Upgrade from 0.5.0-dev to 0.6.0 still works.
    assert updater.is_newer_version("0.5.0", "0.5.0-dev") is False
    assert updater.is_newer_version("0.6.0", "0.5.0-dev") is True


def test_is_newer_version_basic():
    assert updater.is_newer_version("0.5.0", "0.4.0") is True
    assert updater.is_newer_version("0.4.0", "0.4.0") is False
    assert updater.is_newer_version("0.4.0", "0.5.0") is False


def test_is_newer_version_handles_v_prefix():
    assert updater.is_newer_version("v0.5.0", "0.4.0") is True


def test_is_newer_version_invalid_returns_false():
    assert updater.is_newer_version("garbage", "0.4.0") is False
    assert updater.is_newer_version("0.5.0", "garbage") is False


def test_is_newer_version_patch_bump():
    assert updater.is_newer_version("0.4.1", "0.4.0") is True


def test_is_newer_version_multi_digit():
    assert updater.is_newer_version("0.10.0", "0.9.0") is True


# ---- last-check persistence -----------------------------------------------


def test_get_last_check_missing_returns_none(tmp_path: Path):
    assert updater.get_last_version_check(tmp_path / "absent.txt") is None


def test_record_and_read_roundtrip(tmp_path: Path):
    path = tmp_path / "last.txt"
    when = datetime(2026, 5, 17, 14, 30, 45)
    updater.record_version_check(when=when, now_path=path)
    assert updater.get_last_version_check(path) == when


def test_record_overwrites(tmp_path: Path):
    path = tmp_path / "last.txt"
    updater.record_version_check(when=datetime(2026, 5, 1), now_path=path)
    updater.record_version_check(when=datetime(2026, 5, 17), now_path=path)
    assert updater.get_last_version_check(path) == datetime(2026, 5, 17)


def test_get_last_check_handles_corruption(tmp_path: Path):
    path = tmp_path / "last.txt"
    path.write_text("not a date", encoding="utf-8")
    assert updater.get_last_version_check(path) is None


# ---- weekly interval gate -------------------------------------------------


def test_should_check_no_prior_record(tmp_path: Path):
    assert updater.should_check_for_updates(
        last_check_path=tmp_path / "absent.txt"
    ) is True


def test_should_check_within_week_returns_false(tmp_path: Path):
    path = tmp_path / "last.txt"
    base = datetime(2026, 5, 17, 12, 0)
    updater.record_version_check(when=base, now_path=path)
    later = base + timedelta(days=3)
    assert updater.should_check_for_updates(now=later, last_check_path=path) is False


def test_should_check_after_week_returns_true(tmp_path: Path):
    path = tmp_path / "last.txt"
    base = datetime(2026, 5, 17, 12, 0)
    updater.record_version_check(when=base, now_path=path)
    later = base + timedelta(days=8)
    assert updater.should_check_for_updates(now=later, last_check_path=path) is True


def test_check_for_updates_respects_interval_returns_none(tmp_path: Path):
    path = tmp_path / "last.txt"
    base = datetime(2026, 5, 17, 12, 0)
    updater.record_version_check(when=base, now_path=path)
    result = updater.check_for_updates(
        now=base + timedelta(days=1), last_check_path=path
    )
    assert result is None


def test_check_for_updates_stamps_when_interval_elapsed(tmp_path: Path, monkeypatch):
    path = tmp_path / "last.txt"
    base = datetime(2026, 5, 1, 12, 0)
    updater.record_version_check(when=base, now_path=path)
    later = base + timedelta(days=10)
    monkeypatch.setattr(updater, "get_latest_release", lambda **kwargs: None)
    updater.check_for_updates(now=later, last_check_path=path)
    assert updater.get_last_version_check(path) == later


# ---- get_latest_release ----------------------------------------------------


def _fake_release_payload(version: str = "0.6.6", with_installer: bool = True) -> dict:
    """Mimic the post-strip dict shape that get_latest_release returns to upgrade().

    get_latest_release() does `tag_name = data["tag_name"].lstrip("vV")` before
    returning, so callers downstream see "0.6.6" not "v0.6.6". Reflect that
    here so tests assert on the same shape upgrade() actually sees.
    """
    assets = []
    if with_installer:
        assets.append({
            "name": f"meeting-notetaker-setup-{version}.exe",
            "browser_download_url": (
                f"https://github.com/aarondodd/meeting-notetaker/releases/download/"
                f"v{version}/meeting-notetaker-setup-{version}.exe"
            ),
        })
        assets.append({
            "name": "meeting-notetaker.exe",
            "browser_download_url": (
                f"https://github.com/aarondodd/meeting-notetaker/releases/download/"
                f"v{version}/meeting-notetaker.exe"
            ),
        })
    return {
        "tag_name": version,
        "html_url": f"https://github.com/aarondodd/meeting-notetaker/releases/tag/v{version}",
        "body": "Release notes...",
        "assets": assets,
    }


def _stub_urlopen(monkeypatch, payload_or_exc):
    fake_resp = MagicMock()
    if isinstance(payload_or_exc, Exception):
        def raiser(req, timeout=30):
            raise payload_or_exc
        monkeypatch.setattr(updater.urllib.request, "urlopen", raiser)
        return
    fake_resp.read.return_value = json.dumps(payload_or_exc).encode("utf-8")
    fake_resp.__enter__ = lambda self: self
    fake_resp.__exit__ = lambda self, *a: None
    monkeypatch.setattr(
        updater.urllib.request, "urlopen", lambda req, timeout=30: fake_resp
    )


def test_get_latest_release_success(monkeypatch):
    _stub_urlopen(monkeypatch, _fake_release_payload("0.6.6"))
    result = updater.get_latest_release(owner="aarondodd", repo="meeting-notetaker")
    assert result is not None
    assert result["tag_name"] == "0.6.6"
    assert isinstance(result["assets"], list)
    assert len(result["assets"]) == 2


def test_get_latest_release_404(monkeypatch):
    from urllib.error import HTTPError
    _stub_urlopen(
        monkeypatch,
        HTTPError("https://x", 404, "Not Found", {}, io.BytesIO(b"")),
    )
    assert updater.get_latest_release() is None


def test_get_latest_release_network_error(monkeypatch):
    from urllib.error import URLError
    _stub_urlopen(monkeypatch, URLError("connection refused"))
    assert updater.get_latest_release() is None


def test_get_latest_release_missing_tag(monkeypatch):
    """Without tag_name we can't tell if there's an update -- treat as no release."""
    _stub_urlopen(monkeypatch, {"assets": [], "html_url": "", "body": ""})
    assert updater.get_latest_release() is None


def test_get_latest_release_invalid_json(monkeypatch):
    fake_resp = MagicMock()
    fake_resp.read.return_value = b"not json"
    fake_resp.__enter__ = lambda self: self
    fake_resp.__exit__ = lambda self, *a: None
    monkeypatch.setattr(
        updater.urllib.request, "urlopen", lambda req, timeout=30: fake_resp
    )
    assert updater.get_latest_release() is None


# ---- get_installer_asset ---------------------------------------------------


def test_get_installer_asset_picks_setup_exe():
    release = _fake_release_payload("0.6.6")
    asset = updater.get_installer_asset(release)
    assert asset is not None
    version, url = asset
    assert version == "0.6.6"
    assert url.endswith("meeting-notetaker-setup-0.6.6.exe")


def test_get_installer_asset_ignores_portable_exe():
    """A release attaching only meeting-notetaker.exe (no setup-) returns None."""
    release = {
        "tag_name": "0.6.6",
        "assets": [{
            "name": "meeting-notetaker.exe",
            "browser_download_url": "https://x/meeting-notetaker.exe",
        }],
    }
    assert updater.get_installer_asset(release) is None


def test_get_installer_asset_missing_when_no_assets():
    release = _fake_release_payload("0.6.6", with_installer=False)
    # The fake_payload helper still adds the portable .exe; strip it.
    release["assets"] = []
    assert updater.get_installer_asset(release) is None


def test_get_installer_asset_handles_version_suffixes():
    """Pre-release tags (e.g. 0.7.0-rc1) still match the asset pattern."""
    release = {
        "tag_name": "0.7.0-rc1",
        "assets": [{
            "name": "meeting-notetaker-setup-0.7.0-rc1.exe",
            "browser_download_url": "https://x/setup-0.7.0-rc1.exe",
        }],
    }
    asset = updater.get_installer_asset(release)
    assert asset is not None
    assert asset[0] == "0.7.0-rc1"


# ---- check_for_updates end-to-end ------------------------------------------


def test_check_for_updates_returns_tuple_when_newer(tmp_path: Path, monkeypatch):
    path = tmp_path / "last.txt"
    monkeypatch.setattr(
        updater,
        "get_latest_release",
        lambda **kwargs: {
            "tag_name": "99.0.0",
            "assets": [],
            "html_url": "",
            "body": "",
        },
    )
    result = updater.check_for_updates(last_check_path=path)
    assert result is not None
    local, remote = result
    assert remote == "99.0.0"
    assert local == updater.__version__


def test_check_for_updates_returns_none_when_same(tmp_path: Path, monkeypatch):
    path = tmp_path / "last.txt"
    monkeypatch.setattr(
        updater,
        "get_latest_release",
        lambda **kwargs: {
            "tag_name": updater.__version__,
            "assets": [],
            "html_url": "",
            "body": "",
        },
    )
    assert updater.check_for_updates(last_check_path=path) is None


# ---- is_frozen / current_exe_path -----------------------------------------


def test_is_frozen_false_normally(monkeypatch):
    # The pytest interpreter is never pyinstaller-frozen.
    monkeypatch.delattr(updater.sys, "frozen", raising=False)
    assert updater.is_frozen() is False


def test_is_frozen_true_when_attr_set(monkeypatch):
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    assert updater.is_frozen() is True


def test_current_exe_path_none_when_not_frozen(monkeypatch):
    monkeypatch.delattr(updater.sys, "frozen", raising=False)
    assert updater.current_exe_path() is None


# ---- launch_installer ------------------------------------------------------


def test_launch_installer_missing_file_returns_failure(tmp_path: Path):
    ok, msg = updater.launch_installer(tmp_path / "does-not-exist.exe")
    assert not ok
    assert "not found" in msg.lower()


def test_launch_installer_passes_silent_flags(tmp_path: Path, monkeypatch):
    installer = tmp_path / "fake-installer.exe"
    installer.write_bytes(b"MZ stub")  # exists; we won't actually run it
    captured: dict = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return MagicMock()

    monkeypatch.setattr(updater.subprocess, "Popen", fake_popen)
    ok, _msg = updater.launch_installer(installer)
    assert ok
    # The .exe path is the first arg, then the silent flags.
    assert captured["cmd"][0] == str(installer)
    assert "/SILENT" in captured["cmd"]
    assert "/SUPPRESSMSGBOXES" in captured["cmd"]
    assert "/NORESTART" in captured["cmd"]


def test_launch_installer_handles_oserror(tmp_path: Path, monkeypatch):
    installer = tmp_path / "fake-installer.exe"
    installer.write_bytes(b"MZ stub")

    def fake_popen(cmd, **kwargs):
        raise OSError("EACCES")

    monkeypatch.setattr(updater.subprocess, "Popen", fake_popen)
    ok, msg = updater.launch_installer(installer)
    assert not ok
    assert "Could not launch installer" in msg


# ---- upgrade() end-to-end --------------------------------------------------


def test_upgrade_source_build_returns_guidance_without_network(monkeypatch):
    """Running from source (not frozen) short-circuits before any network call."""
    monkeypatch.delattr(updater.sys, "frozen", raising=False)

    # If get_latest_release got called we'd notice -- replace it with a
    # raiser so the test fails loudly if the short-circuit is missing.
    def must_not_call(**kwargs):
        raise AssertionError("get_latest_release should not be called from a source build")

    monkeypatch.setattr(updater, "get_latest_release", must_not_call)
    ok, msg = updater.upgrade()
    assert not ok
    assert "running from source" in msg
    assert "build.ps1" in msg or "rebuild" in msg


def test_upgrade_no_network_returns_failure(monkeypatch):
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater, "get_latest_release", lambda **kwargs: None)
    ok, msg = updater.upgrade()
    assert not ok
    assert "Could not fetch" in msg


def test_upgrade_already_latest_returns_success(monkeypatch):
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        updater,
        "get_latest_release",
        lambda **kwargs: {
            "tag_name": updater.__version__,
            "assets": [],
            "html_url": "",
            "body": "",
        },
    )
    ok, msg = updater.upgrade()
    assert ok
    assert "Already on the latest" in msg


def test_upgrade_no_installer_asset_returns_failure(monkeypatch):
    """A new release with no installer asset surfaces a clear error."""
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        updater,
        "get_latest_release",
        lambda **kwargs: {
            "tag_name": "99.0.0",
            "assets": [],
            "html_url": "",
            "body": "",
        },
    )
    ok, msg = updater.upgrade()
    assert not ok
    assert "no installer asset" in msg
    assert "99.0.0" in msg


def test_upgrade_download_failure_surfaces(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        updater, "get_latest_release", lambda **kwargs: _fake_release_payload("99.0.0")
    )
    monkeypatch.setattr(updater, "updates_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, "download_release", lambda url, dest: False)
    ok, msg = updater.upgrade()
    assert not ok
    assert "Failed to download" in msg


def test_upgrade_happy_path_launches_installer(monkeypatch, tmp_path: Path):
    """Fetch -> download -> launch returns success and calls the launcher."""
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        updater, "get_latest_release", lambda **kwargs: _fake_release_payload("99.0.0")
    )
    monkeypatch.setattr(updater, "updates_dir", lambda: tmp_path)

    def fake_download(url, dest):
        Path(dest).write_bytes(b"MZ stub")
        return True

    monkeypatch.setattr(updater, "download_release", fake_download)

    launched: list = []

    def fake_launch(path):
        launched.append(path)
        return True, f"Launched {Path(path).name}"

    monkeypatch.setattr(updater, "launch_installer", fake_launch)

    stages: list = []
    ok, msg = updater.upgrade(progress_callback=lambda stage, m: stages.append(stage))
    assert ok, msg
    assert "Installer for 99.0.0 has been launched" in msg
    # Stages walked through fetch -> download -> launch -> done.
    assert stages == ["fetch", "download", "launch", "done"]
    # Installer file was written + handed to the launcher.
    assert len(launched) == 1
    assert launched[0].name == "meeting-notetaker-setup-99.0.0.exe"


def test_upgrade_progress_callback_optional(monkeypatch, tmp_path: Path):
    """Callers omitting progress_callback should still work end-to-end."""
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        updater, "get_latest_release", lambda **kwargs: _fake_release_payload("99.0.0")
    )
    monkeypatch.setattr(updater, "updates_dir", lambda: tmp_path)
    monkeypatch.setattr(
        updater, "download_release", lambda url, dest: Path(dest).write_bytes(b"x") or True
    )
    monkeypatch.setattr(updater, "launch_installer", lambda p: (True, ""))
    ok, _msg = updater.upgrade()
    assert ok
