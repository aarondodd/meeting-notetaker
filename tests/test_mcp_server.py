"""MCP server unit tests.

Covers the tool catalog + dispatch path end to end without the stdio
transport. Uses the ``isolated_data_dir`` autouse fixture so the real
SessionStore + TranscriptStore are exercised against a tmp %APPDATA%.
"""
from __future__ import annotations

import io
import json

import pytest

from meeting_notetaker.mcp import server, tools
from meeting_notetaker.models.session import STATE_COMPLETE, SessionStore
from meeting_notetaker.models.transcript import TranscriptStore


# ---- fixtures -----------------------------------------------------------

@pytest.fixture
def seeded_session(isolated_data_dir):
    """Create one session with a transcript + live notes + a prior
    synthesis so the mutation tests have something to overwrite."""
    with SessionStore() as store:
        session = store.create_session("Weekly sync")
        store.update_session(
            session.id,
            state=STATE_COMPLETE,
            has_transcript=True,
        )
    tstore = TranscriptStore(session.id)
    tstore.set_imported_transcript(
        "[00:00:00] Alex: Welcome everyone.\n"
        "[00:00:03] Me: Thanks for joining.\n"
    )
    tstore.save_live_notes(
        "# Attendees\n- Alex\n- Me\n\n"
        "# Agenda\n- status\n\n# Notes\n\n# Action Items\n"
    )
    tstore.save_notes("# Prior synthesis\nnothing yet", archive_existing=False)
    return session


# ---- catalog ------------------------------------------------------------

def test_tool_names_are_unique_and_handlers_bound():
    names = [t.name for t in tools.TOOLS]
    assert len(names) == len(set(names))
    for t in tools.TOOLS:
        assert callable(t.handler)
        assert t.input_schema["type"] == "object"


def test_tool_by_name_unknown_returns_none():
    assert tools.tool_by_name("not-a-tool") is None


# ---- JSON-RPC envelope --------------------------------------------------

def test_initialize_returns_server_info():
    resp = server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    result = resp["result"]
    assert result["serverInfo"]["name"] == "meeting-notetaker"
    assert "tools" in result["capabilities"]
    assert result["protocolVersion"] == server.PROTOCOL_VERSION


def test_initialized_notification_has_no_reply():
    assert server.dispatch(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}
    ) is None


def test_tools_list_matches_catalog():
    resp = server.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    listed = {t["name"] for t in resp["result"]["tools"]}
    expected = {t.name for t in tools.TOOLS}
    assert listed == expected


def test_unknown_method_returns_method_not_found():
    resp = server.dispatch({"jsonrpc": "2.0", "id": 3, "method": "nope"})
    assert resp["error"]["code"] == server.METHOD_NOT_FOUND


def test_unknown_tool_returns_invalid_params():
    resp = server.dispatch({
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {"name": "no-such-tool", "arguments": {}},
    })
    assert resp["error"]["code"] == server.INVALID_PARAMS


# ---- tool handlers ------------------------------------------------------

def test_list_meetings_returns_seeded(seeded_session):
    out = tools.list_meetings({"limit": 10})
    ids = [m["id"] for m in out["meetings"]]
    assert seeded_session.id in ids


def test_list_meetings_state_filter_rejects_bogus(seeded_session):
    out = tools.list_meetings({"state": "bogus"})
    assert "error" in out


def test_get_transcript_round_trip(seeded_session):
    out = tools.get_transcript({"meeting_id": seeded_session.id})
    assert "Welcome everyone" in out["transcript"]


def test_get_meeting_includes_attendees(seeded_session):
    out = tools.get_meeting({"meeting_id": seeded_session.id})
    assert out["meeting"]["title"] == "Weekly sync"
    assert "Alex" in out["meeting"]["attendees"]


def test_get_live_notes_returns_body(seeded_session):
    out = tools.get_live_notes({"meeting_id": seeded_session.id})
    assert "# Attendees" in out["live_notes"]


def test_render_synthesis_prompt_substitutes_placeholders(seeded_session):
    out = tools.render_synthesis_prompt({"meeting_id": seeded_session.id})
    assert "Weekly sync" in out["prompt"]
    assert "Welcome everyone" in out["prompt"]


def test_save_notes_persists_and_flags_has_notes(seeded_session):
    body = "# Final notes\n- shipped\n"
    out = tools.save_notes({"meeting_id": seeded_session.id, "notes": body})
    assert "error" not in out
    assert out["bytes_written"] == len(body.encode("utf-8"))
    # Archive was made because a prior notes.md existed in the fixture.
    assert out["archived_previous"] is not None
    with SessionStore() as store:
        refreshed = store.get_session(seeded_session.id)
    assert refreshed.has_notes is True
    assert TranscriptStore(seeded_session.id).read_notes() == body


def test_save_notes_rejects_missing_id():
    out = tools.save_notes({"notes": "x"})
    assert "error" in out


def test_save_notes_rejects_missing_body(seeded_session):
    out = tools.save_notes({"meeting_id": seeded_session.id})
    assert "error" in out


def test_set_prompt_template_records_override(seeded_session):
    out = tools.set_prompt_template({
        "meeting_id": seeded_session.id,
        "template_name": "default",
    })
    assert "error" not in out
    stored = TranscriptStore(seeded_session.id).read_prompt_template_name()
    assert stored == "default"


def test_set_prompt_template_rejects_unknown(seeded_session):
    out = tools.set_prompt_template({
        "meeting_id": seeded_session.id,
        "template_name": "does-not-exist",
    })
    assert "error" in out


# ---- stdio loop ---------------------------------------------------------

def test_serve_loop_reads_and_writes_newline_delimited(seeded_session):
    """One full session: initialize, notifications/initialized,
    tools/list, tools/call."""
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "list_meetings", "arguments": {"limit": 5}},
        },
    ]
    inp = io.StringIO("\n".join(json.dumps(m) for m in msgs) + "\n")
    out = io.StringIO()
    assert server.serve(inp, out) == 0
    lines = [json.loads(line) for line in out.getvalue().strip().splitlines()]
    assert [r.get("id") for r in lines] == [1, 2, 3]
    assert "serverInfo" in lines[0]["result"]
    tools_payload = lines[1]["result"]["tools"]
    assert any(t["name"] == "list_meetings" for t in tools_payload)
    call_result = lines[2]["result"]
    assert call_result["isError"] is False
    text = call_result["content"][0]["text"]
    assert seeded_session.id in text


def test_serve_loop_handles_bad_json():
    inp = io.StringIO("not json\n")
    out = io.StringIO()
    server.serve(inp, out)
    resp = json.loads(out.getvalue().strip())
    assert resp["error"]["code"] == server.PARSE_ERROR
