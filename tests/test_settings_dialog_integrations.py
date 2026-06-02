"""Settings -> Integrations (Experimental) tab tests (#79).

Covers the new Notion + Confluence credential rows. Verify-click
flows mock the network so the test runs offline + deterministic.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

pytest.importorskip("PyQt6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLineEdit  # noqa: E402

from meeting_notetaker.ui.settings_dialog import SettingsDialog  # noqa: E402
from meeting_notetaker.utils.config import Config  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


def test_integrations_section_registered_with_experimental_tag(qt_app):
    cfg = Config()
    dlg = SettingsDialog(cfg)
    try:
        labels = [
            dlg._nav.item(i).text()  # noqa: SLF001
            for i in range(dlg._nav.count())  # noqa: SLF001
        ]
        assert "Integrations (Experimental)" in labels
    finally:
        dlg.deleteLater()


def test_token_fields_default_to_password_echo(qt_app):
    cfg = Config()
    dlg = SettingsDialog(cfg)
    try:
        assert dlg._notion_token_edit.echoMode() == QLineEdit.EchoMode.Password  # noqa: SLF001
        assert dlg._confluence_token_edit.echoMode() == QLineEdit.EchoMode.Password  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_show_button_toggles_token_visibility(qt_app):
    cfg = Config()
    dlg = SettingsDialog(cfg)
    try:
        dlg._notion_show_token.setChecked(True)  # noqa: SLF001
        assert dlg._notion_token_edit.echoMode() == QLineEdit.EchoMode.Normal  # noqa: SLF001
        dlg._notion_show_token.setChecked(False)  # noqa: SLF001
        assert dlg._notion_token_edit.echoMode() == QLineEdit.EchoMode.Password  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_status_label_says_not_configured_when_empty(qt_app):
    cfg = Config()
    dlg = SettingsDialog(cfg)
    try:
        assert "Not configured" in dlg._notion_status_label.text()  # noqa: SLF001
        assert "Not configured" in dlg._confluence_status_label.text()  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_status_label_shows_verified_when_token_and_timestamp_present(qt_app):
    cfg = Config()
    cfg.notion.api_token = "secret_abc"
    cfg.notion.last_verified_at = "2026-06-02T14:30:00"
    cfg.confluence.base_url = "https://x/wiki"
    cfg.confluence.email = "u@x.com"
    cfg.confluence.api_token = "t"
    cfg.confluence.last_verified_at = "2026-06-02T14:31:00"
    dlg = SettingsDialog(cfg)
    try:
        assert "Connected" in dlg._notion_status_label.text()  # noqa: SLF001
        assert "2026-06-02T14:30:00" in dlg._notion_status_label.text()  # noqa: SLF001
        assert "Connected" in dlg._confluence_status_label.text()  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_notion_verify_button_calls_client_and_stamps_timestamp(qt_app):
    cfg = Config()
    dlg = SettingsDialog(cfg)
    try:
        dlg._notion_token_edit.setText("secret_abc")  # noqa: SLF001
        with patch(
            "meeting_notetaker.integrations.notion_api.NotionClient.verify",
            return_value={"id": "bot-1", "name": "Test Bot"},
        ):
            dlg._on_verify_notion()  # noqa: SLF001
        # Token + timestamp recorded straight to the config instance.
        assert dlg._config.notion.api_token == "secret_abc"  # noqa: SLF001
        assert dlg._config.notion.last_verified_at  # noqa: SLF001
        # Status label reflects the bot name.
        assert "Test Bot" in dlg._notion_status_label.text()  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_notion_verify_failure_surfaces_status_code(qt_app):
    from meeting_notetaker.integrations.notion_api import NotionAPIError

    cfg = Config()
    dlg = SettingsDialog(cfg)
    try:
        dlg._notion_token_edit.setText("bad_token")  # noqa: SLF001
        with patch(
            "meeting_notetaker.integrations.notion_api.NotionClient.verify",
            side_effect=NotionAPIError(401, "bad token", ""),
        ):
            dlg._on_verify_notion()  # noqa: SLF001
        assert "Failed (401)" in dlg._notion_status_label.text()  # noqa: SLF001
        # last_verified_at remains untouched on failure.
        assert dlg._config.notion.last_verified_at == ""  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_confluence_verify_requires_all_three_fields(qt_app):
    cfg = Config()
    dlg = SettingsDialog(cfg)
    try:
        # No fields filled in.
        dlg._on_verify_confluence()  # noqa: SLF001
        assert "Provide" in dlg._confluence_status_label.text()  # noqa: SLF001
        # Only base URL.
        dlg._confluence_base_url_edit.setText("https://x.atlassian.net/wiki")  # noqa: SLF001
        dlg._on_verify_confluence()  # noqa: SLF001
        assert "Provide" in dlg._confluence_status_label.text()  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_confluence_verify_records_credentials_on_success(qt_app):
    cfg = Config()
    dlg = SettingsDialog(cfg)
    try:
        dlg._confluence_base_url_edit.setText("https://x.atlassian.net/wiki")  # noqa: SLF001
        dlg._confluence_email_edit.setText("u@x.com")  # noqa: SLF001
        dlg._confluence_token_edit.setText("t")  # noqa: SLF001
        with patch(
            "meeting_notetaker.integrations.confluence_api.ConfluenceClient.verify",
            return_value={"displayName": "Test User", "accountId": "u-1"},
        ):
            dlg._on_verify_confluence()  # noqa: SLF001
        assert dlg._config.confluence.base_url == "https://x.atlassian.net/wiki"  # noqa: SLF001
        assert dlg._config.confluence.email == "u@x.com"  # noqa: SLF001
        assert dlg._config.confluence.api_token == "t"  # noqa: SLF001
        assert dlg._config.confluence.last_verified_at  # noqa: SLF001
        assert "Test User" in dlg._confluence_status_label.text()  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_changing_token_on_accept_clears_last_verified(qt_app, tmp_path):
    """If the user edits the token after a successful Verify, the
    'Connected' state shouldn't outlive the credential change. On
    accept the timestamp clears so the export menu's verified-only
    gate re-prompts a verify."""
    cfg = Config()
    cfg.notion.api_token = "secret_old"
    cfg.notion.last_verified_at = "2026-06-02T14:30:00"
    dlg = SettingsDialog(cfg)
    try:
        # Edit the token without re-verifying.
        dlg._notion_token_edit.setText("secret_new")  # noqa: SLF001
        dlg._on_accept()  # noqa: SLF001
        assert dlg._config.notion.api_token == "secret_new"  # noqa: SLF001
        assert dlg._config.notion.last_verified_at == ""  # noqa: SLF001
    finally:
        dlg.deleteLater()


def test_unchanged_credentials_keep_last_verified_on_accept(qt_app):
    cfg = Config()
    cfg.confluence.base_url = "https://x/wiki"
    cfg.confluence.email = "u@x.com"
    cfg.confluence.api_token = "t"
    cfg.confluence.last_verified_at = "2026-06-02T14:30:00"
    dlg = SettingsDialog(cfg)
    try:
        dlg._on_accept()  # noqa: SLF001
        assert dlg._config.confluence.last_verified_at == "2026-06-02T14:30:00"  # noqa: SLF001
    finally:
        dlg.deleteLater()
