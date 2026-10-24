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
