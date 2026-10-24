"""Request handlers for the local web UI.

Every HTML route and every JSON API route goes through
``meeting_notetaker.mcp.tools``. That function module is the API
layer; this file is the HTTP transport over it.

Each handler takes a ``Request`` and returns a ``Response`` tuple of
``(status, headers, body)``. The server loop in ``server.py`` just
dispatches and writes the result to the socket, which keeps the test
seam at the handler level.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Mapping

from ..mcp import tools
from ..models.session import SessionStore
from . import views
from .static import STYLE_CSS


Response = tuple[int, dict, bytes]


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, list[str]]
    form: dict[str, list[str]]
    json_body: object | None
    headers: dict[str, str]
    path_params: dict[str, str]


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _html(status: int, body: str, extra_headers: Mapping[str, str] | None = None) -> Response:
    payload = body.encode("utf-8")
    headers = {
        "Content-Type": "text/html; charset=utf-8",
        "Content-Length": str(len(payload)),
        "Cache-Control": "no-store",
    }
    if extra_headers:
        headers.update(extra_headers)
    return status, headers, payload


def _json(status: int, data: object) -> Response:
    payload = json.dumps(data, indent=2, default=str).encode("utf-8")
    return (
        status,
        {
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(payload)),
            "Cache-Control": "no-store",
        },
        payload,
    )


def _text(status: int, body: str, content_type: str = "text/plain; charset=utf-8") -> Response:
    payload = body.encode("utf-8")
    return (
        status,
        {
            "Content-Type": content_type,
            "Content-Length": str(len(payload)),
            "Cache-Control": "no-store",
        },
        payload,
    )


def _redirect(location: str, status: int = 303) -> Response:
    return status, {"Location": location, "Content-Length": "0"}, b""


def _error_json(status: int, message: str) -> Response:
    return _json(status, {"error": message})


def _error_html(status: int, message: str) -> Response:
    return _html(status, views.render_error(status, message))


def _first(values: list[str] | None, default: str | None = None) -> str | None:
    if not values:
        return default
    return values[0]


# ---------------------------------------------------------------------------
# Shared filter semantics
# ---------------------------------------------------------------------------


def _apply_list_filters(request: Request) -> tuple[dict, dict]:
    """Pull the common list filters out of ``request.query`` and
    produce (args_for_list_meetings, raw_filters_for_echoing_in_html)."""
    raw: dict = {}
    args: dict = {}

    limit_val = _first(request.query.get("limit"))
    if limit_val:
        raw["limit"] = limit_val
        try:
            args["limit"] = int(limit_val)
        except ValueError:
            pass

    state_val = _first(request.query.get("state"))
    if state_val:
        raw["state"] = state_val
        args["state"] = state_val

    if "folder_id" in request.query:
        folder_val = _first(request.query.get("folder_id"), "") or ""
        raw["folder_id"] = folder_val
        # Treat an explicit empty string as "unfiled only"; omission as "any".
        args["folder_id"] = folder_val

    # Classification filters: series_id / contact_id / topic_id. Each
    # one is an integer id; the string value stays in raw so the HTML
    # form keeps its selection after submit. An empty value or
    # literal "any" means no filter (the default (any) option).
    for key in ("series_id", "contact_id", "topic_id"):
        val = _first(request.query.get(key))
        if val is None:
            continue
        raw[key] = val
        if val == "" or val == "any":
            continue
        try:
            args[key] = int(val)
        except ValueError:
            # Invalid id goes to raw so the user sees their input in
            # the form; the tool call simply gets an empty filter.
            pass

    has_transcript = _first(request.query.get("has_transcript"))
    if has_transcript == "1":
        raw["has_transcript"] = "1"

    has_notes = _first(request.query.get("has_notes"))
    if has_notes == "1":
        raw["has_notes"] = "1"

    return args, raw


def _obsidian_configured() -> bool:
    """Cheap test for whether a vault export is wired up.

    Reads Config once per request; the common case is a user who sets
    this once in Settings, so round-tripping config is fine. On error
    (missing config.toml etc.) return False so the UI shows the
    'not configured' hint rather than a 500.
    """
    try:
        from ..utils.config import Config  # noqa: PLC0415
        config = Config.load()
    except Exception:
        return False
    return bool((config.obsidian.vault_root or "").strip())


# ---------------------------------------------------------------------------
# HTML handlers
# ---------------------------------------------------------------------------


def home(_req: Request) -> Response:
    return _redirect("/meetings", status=302)


def static_style(_req: Request) -> Response:
    payload = STYLE_CSS.encode("utf-8")
    return (
        200,
        {
            "Content-Type": "text/css; charset=utf-8",
            "Content-Length": str(len(payload)),
            "Cache-Control": "public, max-age=3600",
        },
        payload,
    )


def healthz(_req: Request) -> Response:
    return _text(200, "ok\n")


def meetings_list(request: Request) -> Response:
    args, raw_filters = _apply_list_filters(request)
    result = tools.list_meetings(args)
    if "error" in result:
        return _error_html(400, result["error"])
    meetings = result.get("meetings", [])

    # Client-side-ish filters that tools.list_meetings does not do.
    if raw_filters.get("has_transcript") == "1":
        meetings = [m for m in meetings if m.get("has_transcript")]
    if raw_filters.get("has_notes") == "1":
        meetings = [m for m in meetings if m.get("has_notes")]

    with SessionStore() as store:
        folders = [
            {"id": f.id, "name": f.name, "parent_id": f.parent_id}
            for f in store.list_folders()
        ]

    classification = tools.list_classification({})
    if "error" in classification:
        # A disabled classification store should degrade quietly: no
        # chip dropdowns, same filters as before.
        classification = {"series": [], "contacts": [], "topics": []}

    body = views.render_meeting_list(
        meetings=meetings,
        folders=folders,
        filters=raw_filters,
        total_shown=len(meetings),
        classification=classification,
        obsidian_configured=_obsidian_configured(),
    )
    return _html(200, body)


def meeting_detail(request: Request) -> Response:
    mid = request.path_params["id"]
    meta = tools.get_meeting({"meeting_id": mid})
    if "error" in meta:
        return _error_html(404, meta["error"])
    transcript_data = tools.get_transcript({"meeting_id": mid})
    live_notes_data = tools.get_live_notes({"meeting_id": mid})
    notes_data = tools.get_notes({"meeting_id": mid})

    prompt_data = tools.render_synthesis_prompt({"meeting_id": mid})
    prompt_text = prompt_data.get("prompt", "")
    prompt_tmpl = prompt_data.get("template_name", "")
    if "error" in prompt_data and not prompt_text:
        prompt_text = ""

    template_name = meta["meeting"].get("prompt_template_name", "") or prompt_tmpl

    templates = tools.list_templates({}).get("templates", [])

    flash_tuple = None
    saved_flag = _first(request.query.get("saved"))
    if saved_flag == "1":
        flash_tuple = ("ok", "Synthesis saved; previous notes.md archived.")
    tmpl_set_flag = _first(request.query.get("template_set"))
    if tmpl_set_flag:
        flash_tuple = ("ok", f"Template set to '{tmpl_set_flag}'.")
    obsidian_ok = _first(request.query.get("obsidian"))
    if obsidian_ok:
        flash_tuple = ("ok", f"Saved to Obsidian: {obsidian_ok}")
    obsidian_err = _first(request.query.get("obsidian_err"))
    if obsidian_err:
        flash_tuple = ("err", f"Obsidian export failed: {obsidian_err}")

    body = views.render_meeting_detail(
        meeting=meta["meeting"],
        transcript=transcript_data.get("transcript", ""),
        live_notes=live_notes_data.get("live_notes", ""),
        existing_notes=notes_data.get("notes", ""),
        prompt_text=prompt_text,
        prompt_template_name=template_name,
        templates=templates,
        flash=flash_tuple,
        obsidian_configured=_obsidian_configured(),
    )
    return _html(200, body)


def meeting_save(request: Request) -> Response:
    mid = request.path_params["id"]
    if request.json_body is not None:
        body_dict = request.json_body if isinstance(request.json_body, dict) else {}
        notes = body_dict.get("notes")
    else:
        notes = _first(request.form.get("notes"), "")
    result = tools.save_notes({"meeting_id": mid, "notes": notes or ""})
    wants_json = _wants_json(request)
    if "error" in result:
        return _error_json(400, result["error"]) if wants_json else _error_html(400, result["error"])
    if wants_json:
        return _json(200, result)
    return _redirect(f"/meetings/{urllib.parse.quote(mid)}?saved=1")


def meeting_export_obsidian(request: Request) -> Response:
    """Save a meeting's synthesis into the configured Obsidian vault.

    Form-POST path redirects (303) back to the meeting detail with a
    flash describing the result; JSON path returns the tool's response
    body with a 200/400 status so curl automation can read it.
    """
    mid = request.path_params["id"]
    result = tools.export_meeting_to_obsidian({"meeting_id": mid})
    wants_json = _wants_json(request)
    if wants_json:
        status = 400 if "error" in result else 200
        return _json(status, result)
    quoted_mid = urllib.parse.quote(mid)
    if "error" in result:
        return _redirect(
            f"/meetings/{quoted_mid}"
            f"?obsidian_err={urllib.parse.quote(result['error'])}"
        )
    msg = result.get("message") or result.get("page_url") or "ok"
    return _redirect(
        f"/meetings/{quoted_mid}?obsidian={urllib.parse.quote(msg)}"
    )


def meeting_set_template(request: Request) -> Response:
    mid = request.path_params["id"]
    if request.json_body is not None:
        body_dict = request.json_body if isinstance(request.json_body, dict) else {}
        template_name = body_dict.get("template_name", "")
    else:
        template_name = _first(request.form.get("template_name"), "")
    result = tools.set_prompt_template(
        {"meeting_id": mid, "template_name": template_name or ""}
    )
    wants_json = _wants_json(request)
    if "error" in result:
        return _error_json(400, result["error"]) if wants_json else _error_html(400, result["error"])
    if wants_json:
        return _json(200, result)
    name = template_name or "(default)"
    return _redirect(
        f"/meetings/{urllib.parse.quote(mid)}?template_set={urllib.parse.quote(name)}"
    )


def templates_list(_req: Request) -> Response:
    result = tools.list_templates({})
    return _html(200, views.render_templates_list(result.get("templates", [])))


# ---------------------------------------------------------------------------
# JSON API handlers
# ---------------------------------------------------------------------------


def _wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "").lower()
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in accept:
        return True
    if content_type.startswith("application/json"):
        return True
    if request.path.startswith("/api/"):
        return True
    return False


def api_meetings(request: Request) -> Response:
    args, raw = _apply_list_filters(request)
    result = tools.list_meetings(args)
    if "error" in result:
        return _error_json(400, result["error"])
    meetings = result.get("meetings", [])
    if raw.get("has_transcript") == "1":
        meetings = [m for m in meetings if m.get("has_transcript")]
    if raw.get("has_notes") == "1":
        meetings = [m for m in meetings if m.get("has_notes")]
    return _json(200, {"meetings": meetings})


def api_meeting(request: Request) -> Response:
    result = tools.get_meeting({"meeting_id": request.path_params["id"]})
    status = 404 if "error" in result else 200
    return _json(status, result)


def api_transcript(request: Request) -> Response:
    result = tools.get_transcript({"meeting_id": request.path_params["id"]})
    status = 404 if "error" in result else 200
    return _json(status, result)


def api_live_notes(request: Request) -> Response:
    result = tools.get_live_notes({"meeting_id": request.path_params["id"]})
    status = 404 if "error" in result else 200
    return _json(status, result)


def api_notes(request: Request) -> Response:
    result = tools.get_notes({"meeting_id": request.path_params["id"]})
    status = 404 if "error" in result else 200
    return _json(status, result)


def api_prompt(request: Request) -> Response:
    args: dict = {"meeting_id": request.path_params["id"]}
    tmpl = _first(request.query.get("template_name"))
    if tmpl:
        args["template_name"] = tmpl
    result = tools.render_synthesis_prompt(args)
    status = 404 if "error" in result else 200
    return _json(status, result)


def api_save(request: Request) -> Response:
    body_dict = request.json_body if isinstance(request.json_body, dict) else {}
    args = {
        "meeting_id": request.path_params["id"],
        "notes": body_dict.get("notes", ""),
    }
    result = tools.save_notes(args)
    status = 400 if "error" in result else 200
    return _json(status, result)


def api_set_template(request: Request) -> Response:
    body_dict = request.json_body if isinstance(request.json_body, dict) else {}
    args = {
        "meeting_id": request.path_params["id"],
        "template_name": body_dict.get("template_name", ""),
    }
    result = tools.set_prompt_template(args)
    status = 400 if "error" in result else 200
    return _json(status, result)


def api_templates(_req: Request) -> Response:
    return _json(200, tools.list_templates({}))


def api_template(request: Request) -> Response:
    result = tools.get_template({"name": request.path_params["name"]})
    status = 404 if "error" in result else 200
    return _json(status, result)


def api_classification(_req: Request) -> Response:
    result = tools.list_classification({})
    status = 500 if "error" in result else 200
    return _json(status, result)


def api_export_obsidian(request: Request) -> Response:
    result = tools.export_meeting_to_obsidian(
        {"meeting_id": request.path_params["id"]}
    )
    status = 400 if "error" in result else 200
    return _json(status, result)


# ---------------------------------------------------------------------------
# Routing table
# ---------------------------------------------------------------------------


RouteHandler = Callable[[Request], Response]


@dataclass(frozen=True)
class Route:
    method: str
    pattern: re.Pattern
    handler: RouteHandler


def _compile(regex: str) -> re.Pattern:
    return re.compile(f"^{regex}$")


ROUTES: list[Route] = [
    # HTML surface.
    Route("GET", _compile(r"/"), home),
    Route("GET", _compile(r"/static/style\.css"), static_style),
    Route("GET", _compile(r"/healthz"), healthz),
    Route("GET", _compile(r"/meetings"), meetings_list),
    Route("GET", _compile(r"/meetings/(?P<id>[A-Za-z0-9_\-]+)"), meeting_detail),
    Route("POST", _compile(r"/meetings/(?P<id>[A-Za-z0-9_\-]+)/save"), meeting_save),
    Route(
        "POST",
        _compile(r"/meetings/(?P<id>[A-Za-z0-9_\-]+)/template"),
        meeting_set_template,
    ),
    Route(
        "POST",
        _compile(r"/meetings/(?P<id>[A-Za-z0-9_\-]+)/export/obsidian"),
        meeting_export_obsidian,
    ),
    Route("GET", _compile(r"/templates"), templates_list),
    # JSON API.
    Route("GET", _compile(r"/api/meetings"), api_meetings),
    Route("GET", _compile(r"/api/meetings/(?P<id>[A-Za-z0-9_\-]+)"), api_meeting),
    Route("GET", _compile(r"/api/meetings/(?P<id>[A-Za-z0-9_\-]+)/transcript"), api_transcript),
    Route("GET", _compile(r"/api/meetings/(?P<id>[A-Za-z0-9_\-]+)/live_notes"), api_live_notes),
    Route("GET", _compile(r"/api/meetings/(?P<id>[A-Za-z0-9_\-]+)/notes"), api_notes),
    Route("GET", _compile(r"/api/meetings/(?P<id>[A-Za-z0-9_\-]+)/prompt"), api_prompt),
    Route("POST", _compile(r"/api/meetings/(?P<id>[A-Za-z0-9_\-]+)/save"), api_save),
    Route("POST", _compile(r"/api/meetings/(?P<id>[A-Za-z0-9_\-]+)/template"), api_set_template),
    Route(
        "POST",
        _compile(r"/api/meetings/(?P<id>[A-Za-z0-9_\-]+)/export/obsidian"),
        api_export_obsidian,
    ),
    Route("GET", _compile(r"/api/templates"), api_templates),
    Route("GET", _compile(r"/api/templates/(?P<name>[A-Za-z0-9_\-]+)"), api_template),
    Route("GET", _compile(r"/api/classification"), api_classification),
]


def match_route(method: str, path: str) -> tuple[Route, dict] | None:
    for route in ROUTES:
        if route.method != method:
            continue
        m = route.pattern.match(path)
        if m is None:
            continue
        return route, m.groupdict()
    return None


def dispatch(request: Request) -> Response:
    hit = match_route(request.method, request.path)
    if hit is None:
        wants_json = _wants_json(request)
        if wants_json:
            return _error_json(404, f"no route for {request.method} {request.path}")
        return _error_html(404, f"no route for {request.method} {request.path}")
    route, params = hit
    request.path_params = params
    try:
        return route.handler(request)
    except Exception as exc:
        wants_json = _wants_json(request)
        msg = f"internal error: {exc}"
        if wants_json:
            return _error_json(500, msg)
        return _error_html(500, msg)
