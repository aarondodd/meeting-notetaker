"""Phase-2 MCP tool pins: classification filters, list_classification, Obsidian export.

Pure-Python; no sockets or Qt. Isolates data dir via the autouse fixture.
"""
from __future__ import annotations

import pytest

from meeting_notetaker.mcp import tools
from meeting_notetaker.models.classification import ClassificationStore
from meeting_notetaker.models.session import STATE_COMPLETE, SessionStore
from meeting_notetaker.models.transcript import TranscriptStore
from meeting_notetaker.utils.config import Config


@pytest.fixture
def seeded(isolated_data_dir):
    with SessionStore() as store:
        a = store.create_session("Design review")
        b = store.create_session("1:1 with Alex")
        for sess in (a, b):
            store.update_session(
                sess.id, state=STATE_COMPLETE, has_transcript=True
            )
    TranscriptStore(a.id).save_notes("# Design review\n\n- ok\n", archive_existing=False)
    with SessionStore() as store:
        store.update_session(a.id, has_notes=True)
    with ClassificationStore() as cstore:
        series = cstore.get_or_create_series("Platform")
        topic = cstore.get_or_create_topic("auth")
        cstore.assign_series(a.id, series.id)
        cstore.add_session_topic(a.id, topic.id)
    return a.id, b.id, series.id, topic.id


# ---- list_meetings classification filters -------------------------------


def test_list_meetings_filters_by_series_id(seeded):
    a_id, b_id, series_id, _topic_id = seeded
    result = tools.list_meetings({"series_id": series_id})
    ids = [m["id"] for m in result["meetings"]]
    assert a_id in ids and b_id not in ids


def test_list_meetings_filters_by_topic_id(seeded):
    a_id, b_id, _series_id, topic_id = seeded
    result = tools.list_meetings({"topic_id": topic_id})
    ids = [m["id"] for m in result["meetings"]]
    assert a_id in ids and b_id not in ids


def test_list_meetings_series_and_topic_intersect(seeded):
    a_id, _b_id, series_id, topic_id = seeded
    result = tools.list_meetings(
        {"series_id": series_id, "topic_id": topic_id}
    )
    assert [m["id"] for m in result["meetings"]] == [a_id]


def test_list_meetings_bad_series_id_errors(seeded):
    result = tools.list_meetings({"series_id": "not-a-number"})
    assert "error" in result
    assert "integer" in result["error"]


def test_list_meetings_empty_series_id_is_any(seeded):
    """An empty string must behave like omitting the filter."""
    expect = tools.list_meetings({})
    actual = tools.list_meetings({"series_id": ""})
    assert actual == expect


# ---- list_classification ------------------------------------------------


def test_list_classification_returns_in_use_only(seeded):
    result = tools.list_classification({})
    assert any(s["name"] == "Platform" for s in result["series"])
    assert any(t["name"] == "auth" for t in result["topics"])
    # No contacts tagged yet.
    assert result["contacts"] == []


def test_list_classification_empty_store(isolated_data_dir):
    result = tools.list_classification({})
    assert result == {"series": [], "contacts": [], "topics": []}


# ---- export_meeting_to_obsidian -----------------------------------------


def test_export_without_vault_configured(seeded):
    a_id, _b_id, _s, _t = seeded
    result = tools.export_meeting_to_obsidian({"meeting_id": a_id})
    assert "error" in result
    assert "Obsidian vault is not configured" in result["error"]


def test_export_unknown_meeting(isolated_data_dir, tmp_path):
    cfg = Config.load()
    cfg.obsidian.vault_root = str(tmp_path)
    cfg.save()
    result = tools.export_meeting_to_obsidian({"meeting_id": "does-not-exist"})
    assert "error" in result
    assert "no meeting with id" in result["error"]


def test_export_without_synthesis_errors(seeded, tmp_path):
    _a_id, b_id, _s, _t = seeded
    cfg = Config.load()
    cfg.obsidian.vault_root = str(tmp_path)
    cfg.save()
    result = tools.export_meeting_to_obsidian({"meeting_id": b_id})
    assert "error" in result
    assert "No synthesis saved" in result["error"]


def test_export_happy_path_writes_markdown_into_vault(seeded, tmp_path):
    a_id, _b_id, _s, _t = seeded
    cfg = Config.load()
    cfg.obsidian.vault_root = str(tmp_path)
    cfg.obsidian.vault_name = "test-vault"
    cfg.save()
    result = tools.export_meeting_to_obsidian({"meeting_id": a_id})
    assert "error" not in result, result
    # export_to_obsidian returns an ExportResult carrying a message +
    # an obsidian:// page_url; the tool surfaces both.
    assert result["message"].startswith("Saved to Obsidian vault as "), result
    assert result["page_url"].startswith("obsidian://")
    # Something ending in .md must have landed inside the vault root.
    produced = list(tmp_path.rglob("*.md"))
    assert produced, "no markdown written into the vault"
    body = produced[0].read_text(encoding="utf-8")
    assert "Design review" in body


# ---- tool catalog registration ------------------------------------------


def test_new_tools_are_registered():
    names = {t.name for t in tools.TOOLS}
    assert "list_classification" in names
    assert "export_meeting_to_obsidian" in names
    assert "series_id" in tools.tool_by_name("list_meetings").input_schema["properties"]
