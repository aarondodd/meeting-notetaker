"""Integration tests that bind a real socket on 127.0.0.1:0.

These exercise the full request path (socket -> http.server ->
request handler -> dispatch -> tools). urllib is in the stdlib so
there is no new dependency.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from meeting_notetaker.mcp import tools
from meeting_notetaker.models.session import (
    STATE_COMPLETE,
    SessionStore,
)
from meeting_notetaker.models.transcript import TranscriptStore
from meeting_notetaker.web import server as web_server


@pytest.fixture
def running_server(isolated_data_dir):
    srv, port = web_server.make_server("127.0.0.1", 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    # Give the OS a tick to accept.
    time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2.0)


@pytest.fixture
def seeded_session(isolated_data_dir):
    with SessionStore() as store:
        session = store.create_session("Integration meeting")
        store.update_session(
            session.id,
            state=STATE_COMPLETE,
            has_transcript=True,
        )
    tstore = TranscriptStore(session.id)
    tstore.set_imported_transcript("[00:00:00] Me: hi\n")
    tstore.save_live_notes("# Attendees\n- Me\n")
    return session.id


def _get(url: str, *, accept: str | None = None) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url)
    if accept:
        req.add_header("Accept", accept)
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return resp.status, dict(resp.getheaders()), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()


def _post_form(url: str, body: dict) -> tuple[int, dict]:
    data = urllib.parse.urlencode(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    # Do not follow redirects so we can observe the 303.
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(req, timeout=5.0) as resp:
            return resp.status, dict(resp.getheaders())
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items())


def _post_json(url: str, body: object) -> tuple[int, dict, bytes]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return resp.status, dict(resp.getheaders()), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw) -> None:
        return None


# ---- real socket smoke -------------------------------------------------


def test_server_binds_on_loopback_only(running_server):
    assert running_server.startswith("http://127.0.0.1:")


def test_healthz_over_the_wire(running_server):
    status, _h, body = _get(f"{running_server}/healthz")
    assert status == 200
    assert body == b"ok\n"


def test_meetings_list_over_the_wire(running_server, seeded_session):
    status, headers, body = _get(f"{running_server}/meetings")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"Integration meeting" in body


def test_api_meeting_over_the_wire(running_server, seeded_session):
    status, _h, body = _get(f"{running_server}/api/meetings/{seeded_session}")
    assert status == 200
    data = json.loads(body)
    assert data["meeting"]["id"] == seeded_session


def test_form_save_round_trip_over_the_wire(running_server, seeded_session):
    status, headers = _post_form(
        f"{running_server}/meetings/{seeded_session}/save",
        {"notes": "# browser round-trip\nhello"},
    )
    assert status == 303
    assert headers["Location"].startswith(f"/meetings/{seeded_session}")
    # Confirm the write actually landed.
    result = tools.get_notes({"meeting_id": seeded_session})
    assert "browser round-trip" in result["notes"]


def test_json_api_save_round_trip_over_the_wire(running_server, seeded_session):
    status, _h, body = _post_json(
        f"{running_server}/api/meetings/{seeded_session}/save",
        {"notes": "# api round-trip\nhello"},
    )
    assert status == 200
    data = json.loads(body)
    assert data["bytes_written"] > 0
    assert tools.get_notes({"meeting_id": seeded_session})["notes"].startswith(
        "# api round-trip"
    )


def test_404_is_html_for_browser(running_server):
    status, headers, _body = _get(f"{running_server}/nope")
    assert status == 404
    assert headers["Content-Type"].startswith("text/html")


def test_404_is_json_for_api_prefix(running_server):
    status, headers, body = _get(f"{running_server}/api/nope")
    assert status == 404
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body)["error"]


# ---- port fallback -----------------------------------------------------


def test_port_fallback_picks_next_free(isolated_data_dir):
    """When the preferred port is taken, make_server (with
    allow_port_fallback) should pick the next free one in the range."""
    # Grab an OS-assigned port, then ask for THAT port with fallback on.
    squat_srv, taken_port = web_server.make_server("127.0.0.1", 0)
    try:
        srv, chosen = web_server.make_server(
            "127.0.0.1", taken_port, allow_port_fallback=True
        )
        try:
            assert chosen != taken_port
            assert chosen in range(taken_port, taken_port + web_server.PORT_FALLBACK_RANGE + 1)
        finally:
            srv.server_close()
    finally:
        squat_srv.server_close()


# ---- background server singleton ---------------------------------------


def test_background_server_is_idempotent(isolated_data_dir):
    web_server._reset_background_server_for_tests()
    try:
        url1 = web_server.start_background_server("127.0.0.1", 0)
        url2 = web_server.start_background_server("127.0.0.1", 0)
        assert url1 == url2
        # The URL is actually serving.
        status, _h, body = _get(f"{url1}healthz")
        assert status == 200
        assert body == b"ok\n"
    finally:
        web_server._reset_background_server_for_tests()
