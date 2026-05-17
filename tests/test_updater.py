"""Unit tests for the GitHub-releases updater.

Covers everything that can be exercised without an actual network round
trip: version parsing/comparison, the weekly-interval gate, and the
last-check timestamp persistence. The HTTP call itself is exercised
indirectly via monkeypatching urlopen on get_latest_release.
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
    # Three days later -- still within the weekly window.
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
    # Within the week -- no network call should happen because the gate
    # returns None first.
    result = updater.check_for_updates(
        now=base + timedelta(days=1), last_check_path=path
    )
    assert result is None


def test_check_for_updates_stamps_when_interval_elapsed(tmp_path: Path, monkeypatch):
    path = tmp_path / "last.txt"
    base = datetime(2026, 5, 1, 12, 0)
    updater.record_version_check(when=base, now_path=path)
    later = base + timedelta(days=10)
    # Force get_latest_release to return None so the function exits cleanly
    # without HTTP I/O but still records the new timestamp.
    monkeypatch.setattr(updater, "get_latest_release", lambda **kwargs: None)
    updater.check_for_updates(now=later, last_check_path=path)
    assert updater.get_last_version_check(path) == later


# ---- get_latest_release ----------------------------------------------------


def test_get_latest_release_success(monkeypatch):
    payload = {
        "tag_name": "v0.5.0",
        "zipball_url": "https://api.github.com/zip",
        "html_url": "https://github.com/aarondodd/meeting-notetaker/releases/tag/v0.5.0",
        "body": "Notes...",
    }
    fake_resp = MagicMock()
    fake_resp.read.return_value = json.dumps(payload).encode("utf-8")
    fake_resp.__enter__ = lambda self: self
    fake_resp.__exit__ = lambda self, *a: None
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda req, timeout=30: fake_resp)
    result = updater.get_latest_release(owner="aarondodd", repo="meeting-notetaker")
    assert result is not None
    assert result["tag_name"] == "0.5.0"
    assert result["zipball_url"] == "https://api.github.com/zip"


def test_get_latest_release_404(monkeypatch):
    from urllib.error import HTTPError

    def fake_urlopen(req, timeout=30):
        raise HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b""))

    monkeypatch.setattr(updater.urllib.request, "urlopen", fake_urlopen)
    assert updater.get_latest_release() is None


def test_get_latest_release_network_error(monkeypatch):
    from urllib.error import URLError

    def fake_urlopen(req, timeout=30):
        raise URLError("connection refused")

    monkeypatch.setattr(updater.urllib.request, "urlopen", fake_urlopen)
    assert updater.get_latest_release() is None


def test_get_latest_release_missing_fields(monkeypatch):
    fake_resp = MagicMock()
    fake_resp.read.return_value = json.dumps({"tag_name": "0.5.0"}).encode("utf-8")
    fake_resp.__enter__ = lambda self: self
    fake_resp.__exit__ = lambda self, *a: None
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda req, timeout=30: fake_resp)
    # Missing zipball_url -- should treat as no-release-available.
    assert updater.get_latest_release() is None


def test_get_latest_release_invalid_json(monkeypatch):
    fake_resp = MagicMock()
    fake_resp.read.return_value = b"not json"
    fake_resp.__enter__ = lambda self: self
    fake_resp.__exit__ = lambda self, *a: None
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda req, timeout=30: fake_resp)
    assert updater.get_latest_release() is None


# ---- check_for_updates end-to-end ------------------------------------------


def test_check_for_updates_returns_tuple_when_newer(tmp_path: Path, monkeypatch):
    path = tmp_path / "last.txt"
    monkeypatch.setattr(
        updater,
        "get_latest_release",
        lambda **kwargs: {
            "tag_name": "99.0.0",
            "zipball_url": "x",
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
            "zipball_url": "x",
            "html_url": "",
            "body": "",
        },
    )
    assert updater.check_for_updates(last_check_path=path) is None
