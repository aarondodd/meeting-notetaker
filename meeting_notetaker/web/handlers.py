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


def _enabled_export_destinations() -> list[dict]:
    """Return destinations the user has enabled AND that are configured.

    The dropdown on the detail page offers only destinations in this
    list. ``list_export_destinations`` returns every destination with
    enabled + configured flags; this filter is the AND of both so a
    checkbox for an unconfigured integration (should not happen,
    Settings disables the box) stays silent instead of 500-ing.
    """
    result = tools.list_export_destinations({})
    out = []
    for row in result.get("destinations", []):
        if row.get("enabled") and row.get("configured"):
            out.append(row)
    return out


_EXPORT_TOOLS = {
    "obsidian": tools.export_meeting_to_obsidian,
    "notion": tools.export_meeting_to_notion,
    "confluence": tools.export_meeting_to_confluence,
}


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

    classification = tools.list_classification({})
    if "error" in classification:
        # A disabled classification store should degrade quietly: no
        # chip dropdowns, same filters as before.
        classification = {"series": [], "contacts": [], "topics": []}

    body = views.render_meeting_list(
        meetings=meetings,
        filters=raw_filters,
        total_shown=len(meetings),
        classification=classification,
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
    export_ok = _first(request.query.get("export"))
    if export_ok:
        flash_tuple = ("ok", f"Exported: {export_ok}")
    export_err = _first(request.query.get("export_err"))
    if export_err:
        flash_tuple = ("err", f"Export failed: {export_err}")

    body = views.render_meeting_detail(
        meeting=meta["meeting"],
        transcript=transcript_data.get("transcript", ""),
        live_notes=live_notes_data.get("live_notes", ""),
        existing_notes=notes_data.get("notes", ""),
        prompt_text=prompt_text,
        prompt_template_name=template_name,
        templates=templates,
        flash=flash_tuple,
        export_destinations=_enabled_export_destinations(),
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


def _run_export(mid: str, destination: str) -> dict:
    """Dispatch to the right MCP tool for ``destination``.

    Returns the tool's response dict (with an ``error`` key on
    failure). ``destination`` is validated here so a bogus value fails
    with a clean 400 at the handler layer.
    """
    handler = _EXPORT_TOOLS.get(destination)
    if handler is None:
        return {"error": f"unknown export destination '{destination}'"}
    return handler({"meeting_id": mid})


def _export_redirect(mid: str, result: dict, destination: str) -> Response:
    quoted_mid = urllib.parse.quote(mid)
    if "error" in result:
        return _redirect(
            f"/meetings/{quoted_mid}"
            f"?export_err={urllib.parse.quote(result['error'])}"
        )
    label = {
        "obsidian": "Obsidian",
        "notion": "Notion",
        "confluence": "Confluence",
    }.get(destination, destination)
    msg = result.get("message") or result.get("page_url") or "ok"
    return _redirect(
        f"/meetings/{quoted_mid}"
        f"?export={urllib.parse.quote(f'{label} -- {msg}')}"
    )


def meeting_export(request: Request) -> Response:
    """Dispatch /meetings/<id>/export on form ``destination`` field.

    The detail page's dropdown POSTs here. Form path redirects (303)
    back to the detail with a flash; JSON path returns the tool's
    response for scripted automation.
    """
    mid = request.path_params["id"]
    destination = _first(request.form.get("destination"), "")
    if request.json_body is not None:
        body_dict = request.json_body if isinstance(request.json_body, dict) else {}
        destination = (body_dict.get("destination") or destination or "").strip()
    destination = (destination or "").strip()
    wants_json = _wants_json(request)
    if not destination:
        err = "destination is required (one of obsidian, notion, confluence)"
        return _error_json(400, err) if wants_json else _error_html(400, err)
    result = _run_export(mid, destination)
    if wants_json:
        status = 400 if "error" in result else 200
        return _json(status, result)
    return _export_redirect(mid, result, destination)


def meeting_export_obsidian(request: Request) -> Response:
    """Legacy per-destination route; kept so an older skill still works.

    Form path redirects back to the detail with a flash; JSON path
    returns the tool's response body with a 200/400 status.
    """
    mid = request.path_params["id"]
    result = _run_export(mid, "obsidian")
    wants_json = _wants_json(request)
    if wants_json:
        status = 400 if "error" in result else 200
        return _json(status, result)
    return _export_redirect(mid, result, "obsidian")


def meeting_export_notion(request: Request) -> Response:
    mid = request.path_params["id"]
    result = _run_export(mid, "notion")
    wants_json = _wants_json(request)
    if wants_json:
        status = 400 if "error" in result else 200
        return _json(status, result)
    return _export_redirect(mid, result, "notion")


def meeting_export_confluence(request: Request) -> Response:
    mid = request.path_params["id"]
    result = _run_export(mid, "confluence")
    wants_json = _wants_json(request)
    if wants_json:
        status = 400 if "error" in result else 200
        return _json(status, result)
    return _export_redirect(mid, result, "confluence")


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


def api_export_notion(request: Request) -> Response:
    result = tools.export_meeting_to_notion(
        {"meeting_id": request.path_params["id"]}
    )
    status = 400 if "error" in result else 200
    return _json(status, result)


def api_export_confluence(request: Request) -> Response:
    result = tools.export_meeting_to_confluence(
        {"meeting_id": request.path_params["id"]}
    )
    status = 400 if "error" in result else 200
    return _json(status, result)


def api_export_destinations(_req: Request) -> Response:
    result = tools.list_export_destinations({})
    status = 500 if "error" in result else 200
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
        _compile(r"/meetings/(?P<id>[A-Za-z0-9_\-]+)/export"),
        meeting_export,
    ),
    Route(
        "POST",
        _compile(r"/meetings/(?P<id>[A-Za-z0-9_\-]+)/export/obsidian"),
        meeting_export_obsidian,
    ),
    Route(
        "POST",
        _compile(r"/meetings/(?P<id>[A-Za-z0-9_\-]+)/export/notion"),
        meeting_export_notion,
    ),
    Route(
        "POST",
        _compile(r"/meetings/(?P<id>[A-Za-z0-9_\-]+)/export/confluence"),
        meeting_export_confluence,
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
    Route(
        "POST",
        _compile(r"/api/meetings/(?P<id>[A-Za-z0-9_\-]+)/export/notion"),
        api_export_notion,
    ),
    Route(
        "POST",
        _compile(r"/api/meetings/(?P<id>[A-Za-z0-9_\-]+)/export/confluence"),
        api_export_confluence,
    ),
    Route("GET", _compile(r"/api/templates"), api_templates),
    Route("GET", _compile(r"/api/templates/(?P<name>[A-Za-z0-9_\-]+)"), api_template),
    Route("GET", _compile(r"/api/classification"), api_classification),
    Route("GET", _compile(r"/api/export_destinations"), api_export_destinations),
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
