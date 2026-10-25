"""Handler-level tests for the local web UI.

Each test calls ``handlers.dispatch`` directly against a Request
built in the test, so we never bind a socket. Integration via urllib
is covered in test_web_server.py.
"""
from __future__ import annotations

import json

import pytest

from meeting_notetaker.mcp import tools
from meeting_notetaker.models.session import (
    STATE_COMPLETE,
    STATE_NEW,
    SessionStore,
)
from meeting_notetaker.models.transcript import TranscriptStore
from meeting_notetaker.web import handlers


# ---- fixtures -----------------------------------------------------------


@pytest.fixture
def seeded_meetings(isolated_data_dir):
    """One finished meeting with full data, one new one with nothing.

    Returns the SessionStore IDs as (completed_id, new_id) for the
    handler tests to reference by id.
    """
    with SessionStore() as store:
        done = store.create_session("Weekly sync")
        store.update_session(
            done.id,
            state=STATE_COMPLETE,
            has_transcript=True,
            has_notes=True,
        )
        empty = store.create_session("Draft meeting")
        store.update_session(empty.id, state=STATE_NEW)
    tstore = TranscriptStore(done.id)
    tstore.set_imported_transcript(
        "[00:00:00] Alex: Welcome.\n[00:00:03] Me: Thanks.\n"
    )
    tstore.save_live_notes(
        "# Attendees\n- Alex\n- Me\n\n# Agenda\n- status\n\n"
        "# Notes\n\n# Action Items\n"
    )
    tstore.save_notes("# Prior synthesis\nnothing yet", archive_existing=False)
    return done.id, empty.id


def _req(
    method: str,
    path: str,
    *,
    query: dict | None = None,
    form: dict | None = None,
    json_body: object | None = None,
    headers: dict | None = None,
) -> handlers.Request:
    q: dict[str, list[str]] = {k: [str(v)] for k, v in (query or {}).items()}
    f: dict[str, list[str]] = {k: [str(v)] for k, v in (form or {}).items()}
    return handlers.Request(
        method=method,
        path=path,
        query=q,
        form=f,
        json_body=json_body,
        headers={(k or "").lower(): v for k, v in (headers or {}).items()},
        path_params={},
    )


# ---- routing + smoke ----------------------------------------------------


def test_home_redirects_to_meetings():
    status, hdrs, body = handlers.dispatch(_req("GET", "/"))
    assert status == 302
    assert hdrs["Location"] == "/meetings"
    assert body == b""


def test_unknown_route_returns_404_html():
    status, hdrs, body = handlers.dispatch(_req("GET", "/nope"))
    assert status == 404
    assert hdrs["Content-Type"].startswith("text/html")


def test_unknown_route_returns_404_json_for_api():
    status, hdrs, body = handlers.dispatch(_req("GET", "/api/nope"))
    assert status == 404
    assert hdrs["Content-Type"].startswith("application/json")
    assert json.loads(body)["error"]


def test_healthz_ok():
    status, _hdrs, body = handlers.dispatch(_req("GET", "/healthz"))
    assert status == 200
    assert body == b"ok\n"


def test_static_style_served():
    status, hdrs, body = handlers.dispatch(_req("GET", "/static/style.css"))
    assert status == 200
    assert hdrs["Content-Type"].startswith("text/css")
    assert b"body" in body


# ---- /meetings ----------------------------------------------------------


def test_meetings_list_shows_both_seeded(seeded_meetings):
    done_id, empty_id = seeded_meetings
    status, _hdrs, body = handlers.dispatch(_req("GET", "/meetings"))
    assert status == 200
    html = body.decode("utf-8")
    assert "Weekly sync" in html
    assert "Draft meeting" in html
    assert "2 meeting(s)" in html


def test_meetings_list_filters_by_state(seeded_meetings):
    _done_id, empty_id = seeded_meetings
    status, _hdrs, body = handlers.dispatch(
        _req("GET", "/meetings", query={"state": STATE_NEW})
    )
    assert status == 200
    html = body.decode("utf-8")
    assert "Draft meeting" in html
    assert "Weekly sync" not in html


def test_meetings_list_filters_by_has_notes(seeded_meetings):
    done_id, empty_id = seeded_meetings
    status, _hdrs, body = handlers.dispatch(
        _req("GET", "/meetings", query={"has_notes": "1"})
    )
    html = body.decode("utf-8")
    assert "Weekly sync" in html
    assert "Draft meeting" not in html


def test_meetings_list_filters_by_has_transcript(seeded_meetings):
    done_id, empty_id = seeded_meetings
    status, _hdrs, body = handlers.dispatch(
        _req("GET", "/meetings", query={"has_transcript": "1"})
    )
    html = body.decode("utf-8")
    assert "Weekly sync" in html
    assert "Draft meeting" not in html


def test_meetings_list_empty_filter_set_renders(seeded_meetings):
    status, _hdrs, body = handlers.dispatch(
        _req("GET", "/meetings", query={"state": STATE_NEW, "has_notes": "1"})
    )
    assert status == 200
    assert b"No meetings match" in body


# ---- /meetings/<id> -----------------------------------------------------


def test_meeting_detail_renders_everything(seeded_meetings):
    done_id, _empty_id = seeded_meetings
    status, _hdrs, body = handlers.dispatch(
        _req("GET", f"/meetings/{done_id}")
    )
    assert status == 200
    html = body.decode("utf-8")
    assert "Weekly sync" in html
    assert "Alex" in html
    assert "Prior synthesis" in html
    assert 'name="notes"' in html
    assert 'action="/meetings/' in html
    assert "Save synthesis" in html
    # The rendered prompt pane is in a <pre> so read_page can grab it.
    assert '<pre id="rendered-prompt">' in html


def test_meeting_detail_unknown_id_is_404(isolated_data_dir):
    status, hdrs, _body = handlers.dispatch(
        _req("GET", "/meetings/no-such-id")
    )
    assert status == 404
    assert hdrs["Content-Type"].startswith("text/html")


# ---- /meetings/<id>/save (HTML form) ------------------------------------


def test_meeting_save_form_writes_notes_and_redirects(seeded_meetings):
    done_id, _empty_id = seeded_meetings
    status, hdrs, _body = handlers.dispatch(
        _req(
            "POST",
            f"/meetings/{done_id}/save",
            form={"notes": "# From the browser\nhello"},
        )
    )
    assert status == 303
    assert hdrs["Location"] == f"/meetings/{done_id}?saved=1"
    # The write went through the tool layer.
    reread = tools.get_notes({"meeting_id": done_id})
    assert "From the browser" in reread["notes"]


def test_meeting_save_form_empty_notes_is_accepted(seeded_meetings):
    done_id, _empty_id = seeded_meetings
    status, _hdrs, _body = handlers.dispatch(
        _req("POST", f"/meetings/{done_id}/save", form={"notes": ""})
    )
    # An empty string is valid; the tool accepts it.
    assert status == 303


# ---- /meetings/<id>/template (HTML form) --------------------------------


def test_meeting_set_template_form_persists(seeded_meetings):
    done_id, _empty_id = seeded_meetings
    status, hdrs, _body = handlers.dispatch(
        _req(
            "POST",
            f"/meetings/{done_id}/template",
            form={"template_name": ""},
        )
    )
    assert status == 303
    assert hdrs["Location"].startswith(f"/meetings/{done_id}?template_set=")


def test_meeting_set_template_rejects_unknown(seeded_meetings):
    done_id, _empty_id = seeded_meetings
    status, _hdrs, body = handlers.dispatch(
        _req(
            "POST",
            f"/meetings/{done_id}/template",
            form={"template_name": "does-not-exist"},
        )
    )
    assert status == 400
    assert b"does-not-exist" in body


# ---- JSON API -----------------------------------------------------------


def test_api_meetings_json(seeded_meetings):
    status, hdrs, body = handlers.dispatch(_req("GET", "/api/meetings"))
    assert status == 200
    assert hdrs["Content-Type"].startswith("application/json")
    data = json.loads(body)
    assert "meetings" in data
    assert len(data["meetings"]) == 2


def test_api_meetings_filters(seeded_meetings):
    status, _hdrs, body = handlers.dispatch(
        _req("GET", "/api/meetings", query={"has_notes": "1"})
    )
    data = json.loads(body)
    assert len(data["meetings"]) == 1
    assert data["meetings"][0]["title"] == "Weekly sync"


def test_api_meeting_404_for_unknown(isolated_data_dir):
    status, _hdrs, body = handlers.dispatch(_req("GET", "/api/meetings/none"))
    assert status == 404
    assert json.loads(body)["error"]


def test_api_meeting_shapes(seeded_meetings):
    done_id, _ = seeded_meetings
    status, _hdrs, body = handlers.dispatch(
        _req("GET", f"/api/meetings/{done_id}")
    )
    assert status == 200
    data = json.loads(body)
    assert data["meeting"]["id"] == done_id
    assert "attendees" in data["meeting"]


def test_api_transcript(seeded_meetings):
    done_id, _ = seeded_meetings
    status, _hdrs, body = handlers.dispatch(
        _req("GET", f"/api/meetings/{done_id}/transcript")
    )
    data = json.loads(body)
    assert status == 200
    assert "Welcome" in data["transcript"]


def test_api_live_notes(seeded_meetings):
    done_id, _ = seeded_meetings
    status, _hdrs, body = handlers.dispatch(
        _req("GET", f"/api/meetings/{done_id}/live_notes")
    )
    data = json.loads(body)
    assert status == 200
    assert "Attendees" in data["live_notes"]


def test_api_notes_round_trips(seeded_meetings):
    done_id, _ = seeded_meetings
    status, _hdrs, body = handlers.dispatch(
        _req("GET", f"/api/meetings/{done_id}/notes")
    )
    data = json.loads(body)
    assert status == 200
    assert "Prior synthesis" in data["notes"]


def test_api_prompt(seeded_meetings):
    done_id, _ = seeded_meetings
    status, _hdrs, body = handlers.dispatch(
        _req("GET", f"/api/meetings/{done_id}/prompt")
    )
    assert status == 200
    data = json.loads(body)
    assert data["prompt"]  # non-empty


def test_api_save_round_trip(seeded_meetings):
    done_id, _ = seeded_meetings
    status, _hdrs, body = handlers.dispatch(
        _req(
            "POST",
            f"/api/meetings/{done_id}/save",
            json_body={"notes": "# via api\nok"},
            headers={"content-type": "application/json"},
        )
    )
    assert status == 200
    data = json.loads(body)
    assert data["bytes_written"] > 0
    assert tools.get_notes({"meeting_id": done_id})["notes"].startswith("# via api")


def test_api_save_bad_meeting_is_400(isolated_data_dir):
    status, _hdrs, body = handlers.dispatch(
        _req(
            "POST",
            "/api/meetings/none/save",
            json_body={"notes": "x"},
            headers={"content-type": "application/json"},
        )
    )
    assert status == 400
    assert json.loads(body)["error"]


def test_api_set_template(seeded_meetings):
    done_id, _ = seeded_meetings
    status, _hdrs, body = handlers.dispatch(
        _req(
            "POST",
            f"/api/meetings/{done_id}/template",
            json_body={"template_name": ""},
            headers={"content-type": "application/json"},
        )
    )
    assert status == 200
    assert json.loads(body)["template_name"] == ""


def test_api_templates(seeded_meetings):
    status, _hdrs, body = handlers.dispatch(_req("GET", "/api/templates"))
    assert status == 200
    assert "templates" in json.loads(body)


def test_api_template_unknown(seeded_meetings):
    status, _hdrs, body = handlers.dispatch(
        _req("GET", "/api/templates/does-not-exist")
    )
    assert status == 404
    assert json.loads(body)["error"]


# ---- error path propagation --------------------------------------------


def test_meetings_list_renders_series_contact_topic_filters(seeded_meetings):
    """The expanded filter form carries the new classification selects
    and the Export column; `(any)` is selected by default. Covers the
    graceful-degrade path when the classification store returns no
    in-use rows."""
    status, _h, body = handlers.dispatch(_req("GET", "/meetings"))
    assert status == 200
    text = body.decode("utf-8")
    for name in ('name="series_id"', 'name="contact_id"', 'name="topic_id"'):
        assert name in text, f"missing filter select: {name}"
    # The label text runs together with the leading `<select ...>`.
    assert "Series <select" in text
    assert "Person <select" in text
    assert "Topic <select" in text
    # Folder filter + per-row Export column were removed in v0.8.1 to
    # match the Python app's list view and to collapse export into a
    # single detail-page dropdown.
    assert 'name="folder_id"' not in text
    assert "<th>Export</th>" not in text


def test_meetings_list_filters_pass_through_to_tools(seeded_meetings, monkeypatch):
    """series_id in the query string reaches tools.list_meetings as an int."""
    captured: list[dict] = []
    real_list = tools.list_meetings

    def spy(args: dict) -> dict:
        captured.append(args)
        return real_list(args)

    monkeypatch.setattr(tools, "list_meetings", spy)
    # Also patch the handlers' reference since it imported the name.
    monkeypatch.setattr(handlers.tools, "list_meetings", spy)
    status, _h, _body = handlers.dispatch(
        _req("GET", "/meetings", query={"series_id": "42"})
    )
    assert status == 200
    assert any(c.get("series_id") == 42 for c in captured), captured


def test_meetings_list_invalid_series_id_degrades(seeded_meetings):
    """A non-integer series_id must not raise; the filter is dropped."""
    status, _h, _body = handlers.dispatch(
        _req("GET", "/meetings", query={"series_id": "not-an-int"})
    )
    assert status == 200


def test_meeting_detail_renders_synthesis_as_markdown(seeded_meetings, monkeypatch):
    """Synthesis body comes back as rendered HTML in a .md container,
    not inside the plain <pre> wrapper."""
    completed_id, _new_id = seeded_meetings
    TranscriptStore(completed_id).save_notes(
        "# Decisions\n\n- ship it\n- *soon*\n",
        archive_existing=False,
    )
    status, _h, body = handlers.dispatch(
        _req("GET", f"/meetings/{completed_id}")
    )
    assert status == 200
    text = body.decode("utf-8")
    # Rich container is present, not the fallback <pre>.
    assert '<div class="md">' in text
    assert "<h1>Decisions</h1>" in text
    assert "<li>ship it</li>" in text


def test_meeting_detail_export_section_shows_hint_when_none_enabled(seeded_meetings):
    """Without any destination enabled in Settings, the Export section
    renders the hint rather than a dropdown."""
    completed_id, _new_id = seeded_meetings
    status, _h, body = handlers.dispatch(
        _req("GET", f"/meetings/{completed_id}")
    )
    assert status == 200
    text = body.decode("utf-8")
    assert 'id="export"' in text
    assert "No export destinations are enabled" in text
    assert 'name="destination"' not in text


def test_meeting_export_obsidian_without_vault_flashes_err(seeded_meetings):
    """Legacy /export/obsidian form path still works, redirects with
    an `export_err=...` flash."""
    completed_id, _new_id = seeded_meetings
    status, hdrs, _body = handlers.dispatch(
        _req("POST", f"/meetings/{completed_id}/export/obsidian", form={})
    )
    assert status == 303
    assert hdrs["Location"].startswith(f"/meetings/{completed_id}")
    assert "export_err=" in hdrs["Location"]
    # Loading the detail page with that query renders the err flash.
    import urllib.parse  # local import; the module is already imported above
    parsed = urllib.parse.urlparse(hdrs["Location"])
    query = urllib.parse.parse_qs(parsed.query)
    status, _h, body = handlers.dispatch(
        _req("GET", f"/meetings/{completed_id}",
             query={"export_err": query["export_err"][0]})
    )
    assert status == 200
    text = body.decode("utf-8")
    assert 'class="flash err"' in text
    assert "Export failed" in text


def test_meeting_export_obsidian_json_returns_400_without_vault(seeded_meetings):
    """JSON POST surfaces the error as a 400 so curl automation sees it."""
    completed_id, _new_id = seeded_meetings
    status, _h, body = handlers.dispatch(
        _req(
            "POST",
            f"/meetings/{completed_id}/export/obsidian",
            headers={"accept": "application/json"},
            json_body={},
        )
    )
    assert status == 400
    data = json.loads(body)
    assert "Obsidian vault is not configured" in data["error"]


def test_meeting_export_dropdown_rejects_missing_destination(seeded_meetings):
    """A form POST to /export with no destination field returns 400
    rather than silently exporting to the first enabled destination."""
    completed_id, _new_id = seeded_meetings
    status, _h, body = handlers.dispatch(
        _req("POST", f"/meetings/{completed_id}/export", form={})
    )
    assert status == 400
    assert b"destination is required" in body


def test_meeting_export_dropdown_dispatches_to_destination(seeded_meetings):
    """POST /export with destination=obsidian hits the obsidian handler.
    With no vault configured, the dispatcher still returns a 303 with
    export_err just like /export/obsidian."""
    completed_id, _new_id = seeded_meetings
    status, hdrs, _body = handlers.dispatch(
        _req(
            "POST",
            f"/meetings/{completed_id}/export",
            form={"destination": "obsidian"},
        )
    )
    assert status == 303
    assert "export_err=" in hdrs["Location"]


def test_api_export_destinations_returns_three_rows(isolated_data_dir):
    """The destinations endpoint always carries one row per key, each
    carrying configured + enabled booleans."""
    status, _h, body = handlers.dispatch(
        _req("GET", "/api/export_destinations",
             headers={"accept": "application/json"})
    )
    assert status == 200
    data = json.loads(body)
    keys = sorted(r["key"] for r in data["destinations"])
    assert keys == ["confluence", "notion", "obsidian"]
    for row in data["destinations"]:
        assert "configured" in row and "enabled" in row


def test_api_classification_route_returns_empty_lists(isolated_data_dir):
    """With no classification rows yet, the endpoint returns three
    empty arrays rather than an error."""
    status, _h, body = handlers.dispatch(
        _req("GET", "/api/classification",
             headers={"accept": "application/json"})
    )
    assert status == 200
    data = json.loads(body)
    assert data == {"series": [], "contacts": [], "topics": []}


def test_dispatcher_wraps_handler_exceptions(monkeypatch):
    """An exception in a handler must not crash the server; it should
    produce an HTML 500 for HTML routes and a JSON 500 for /api/."""
    def boom(_req: handlers.Request) -> handlers.Response:
        raise RuntimeError("deliberate")

    monkeypatch.setitem(
        {r.pattern.pattern: r for r in handlers.ROUTES},
        r"^/healthz$",
        handlers.Route("GET", handlers.ROUTES[2].pattern, boom),
    )
    # The real route table is not mutable through that dict trick,
    # so swap the handler directly on the Route object.
    for idx, route in enumerate(handlers.ROUTES):
        if route.pattern.pattern == r"^/healthz$":
            handlers.ROUTES[idx] = handlers.Route("GET", route.pattern, boom)
            break
    try:
        status_html, _h_html, body_html = handlers.dispatch(
            _req("GET", "/healthz")
        )
        assert status_html == 500
        assert b"deliberate" in body_html
        status_api, _h_api, body_api = handlers.dispatch(
            _req("GET", "/healthz", headers={"accept": "application/json"})
        )
        assert status_api == 500
        assert json.loads(body_api)["error"]
    finally:
        # Restore the real handler so later tests see a working /healthz.
        for idx, route in enumerate(handlers.ROUTES):
            if route.pattern.pattern == r"^/healthz$":
                handlers.ROUTES[idx] = handlers.Route(
                    "GET", route.pattern, handlers.healthz
                )
                break
