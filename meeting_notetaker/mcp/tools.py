"""Tool catalog + handlers for the Meeting Notetaker MCP server.

Each tool is a pure function that reads from or writes to the user's
local store. The handlers are callable directly from unit tests
without going through the stdio transport.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional

from ..models.session import ALL_STATES, SessionStore
from ..models.transcript import TranscriptStore
from ..utils import prompts as prompts_mod
from ..utils.config import Config
from ..utils.live_notes import parse_attendees


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], dict]


# ---------------------------------------------------------------------------
# Session + transcript tools
# ---------------------------------------------------------------------------


def _session_summary(session) -> dict:
    return {
        "id": session.id,
        "title": session.title,
        "state": session.state,
        "created_at": session.created_at,
        "started_at": session.started_at,
        "ended_at": session.ended_at,
        "duration_seconds": session.duration_seconds,
        "has_transcript": session.has_transcript,
        "has_notes": session.has_notes,
        "folder_id": session.folder_id,
    }


def list_meetings(args: dict) -> dict:
    """List recent meetings, newest first.

    Args:
        limit: optional int, default 20, max 200.
        state: optional filter, one of the lifecycle states; omit for all.
        folder_id: optional folder filter; empty string means unfiled only.
    """
    limit = int(args.get("limit", 20))
    if limit < 1:
        limit = 1
    if limit > 200:
        limit = 200
    state = args.get("state")
    if state is not None and state not in ALL_STATES:
        return {"error": f"unknown state '{state}'; valid: {sorted(ALL_STATES)}"}
    folder_filter = args.get("folder_id")
    with SessionStore() as store:
        if folder_filter is None:
            sessions = store.list_sessions()
        else:
            sessions = store.list_sessions(folder_id=folder_filter or None)
        if state is not None:
            sessions = [s for s in sessions if s.state == state]
        return {"meetings": [_session_summary(s) for s in sessions[:limit]]}


def get_meeting(args: dict) -> dict:
    """Return a single meeting's metadata."""
    mid = args.get("meeting_id")
    if not mid:
        return {"error": "meeting_id is required"}
    with SessionStore() as store:
        session = store.get_session(mid)
    if not session:
        return {"error": f"no meeting with id {mid}"}
    attendees: list[str] = []
    try:
        live_notes = TranscriptStore(session.id).read_live_notes()
        attendees = parse_attendees(live_notes)
    except Exception:
        pass
    data = _session_summary(session)
    data["attendees"] = attendees
    data["prompt_template_name"] = TranscriptStore(session.id).read_prompt_template_name()
    return {"meeting": data}


def get_transcript(args: dict) -> dict:
    """Return a meeting's raw transcript text."""
    mid = args.get("meeting_id")
    if not mid:
        return {"error": "meeting_id is required"}
    with SessionStore() as store:
        session = store.get_session(mid)
    if not session:
        return {"error": f"no meeting with id {mid}"}
    body = TranscriptStore(mid).read_transcript()
    return {"meeting_id": mid, "transcript": body}


def get_live_notes(args: dict) -> dict:
    """Return a meeting's live-notes buffer (user-authored)."""
    mid = args.get("meeting_id")
    if not mid:
        return {"error": "meeting_id is required"}
    with SessionStore() as store:
        session = store.get_session(mid)
    if not session:
        return {"error": f"no meeting with id {mid}"}
    body = TranscriptStore(mid).read_live_notes()
    return {"meeting_id": mid, "live_notes": body}


def get_notes(args: dict) -> dict:
    """Return the current synthesized notes (notes.md) for a meeting."""
    mid = args.get("meeting_id")
    if not mid:
        return {"error": "meeting_id is required"}
    with SessionStore() as store:
        session = store.get_session(mid)
    if not session:
        return {"error": f"no meeting with id {mid}"}
    return {"meeting_id": mid, "notes": TranscriptStore(mid).read_notes()}


# ---------------------------------------------------------------------------
# Prompt-template tools
# ---------------------------------------------------------------------------


def list_templates(_args: dict) -> dict:
    """List available synthesis prompt templates."""
    templates = prompts_mod.list_templates()
    return {
        "templates": [
            {"name": t.name, "display_name": t.display_name}
            for t in templates
        ]
    }


def get_template(args: dict) -> dict:
    """Return a prompt template's raw body."""
    name = args.get("name")
    if not name:
        return {"error": "name is required"}
    tmpl = prompts_mod.get_template(name)
    if tmpl is None:
        return {"error": f"no template named '{name}'"}
    return {
        "name": tmpl.name,
        "display_name": tmpl.display_name,
        "body": tmpl.body,
    }


def render_synthesis_prompt(args: dict) -> dict:
    """Render a ready-to-execute synthesis prompt for a meeting.

    This is the convenience tool: give it a meeting id, get back a
    fully-substituted prompt that an MCP client can feed to its own
    model to produce synthesized notes without any further round-trip.
    """
    mid = args.get("meeting_id")
    if not mid:
        return {"error": "meeting_id is required"}
    with SessionStore() as store:
        session = store.get_session(mid)
    if not session:
        return {"error": f"no meeting with id {mid}"}

    tstore = TranscriptStore(mid)
    transcript = tstore.read_transcript()
    live_notes = tstore.read_live_notes()

    config = Config.load()
    template_name = args.get("template_name") or tstore.read_prompt_template_name()
    if not template_name:
        template_name = config.synthesis.default_template_name or "default"
    tmpl = prompts_mod.get_template(template_name)
    if tmpl is None:
        return {"error": f"no template named '{template_name}'"}

    user_name = config.ui.user_name or ""

    session_date_str = session.started_at or session.created_at
    try:
        session_dt: datetime | str = datetime.fromisoformat(
            session_date_str.replace("Z", "+00:00")
        )
    except (ValueError, AttributeError):
        session_dt = session_date_str

    rendered = prompts_mod.render(
        tmpl,
        session_title=session.title,
        session_date=session_dt,
        transcript=transcript,
        live_notes=live_notes,
        user_name=user_name,
    )
    return {
        "meeting_id": mid,
        "template_name": template_name,
        "prompt": rendered,
    }


# ---------------------------------------------------------------------------
# Mutating tools
# ---------------------------------------------------------------------------


def save_notes(args: dict) -> dict:
    """Write synthesized notes to a meeting's notes.md.

    Archives any prior notes.md to notes-<timestamp>.md before writing.
    Updates the session's has_notes flag so the app's session-list
    badge refreshes next time it's loaded.
    """
    mid = args.get("meeting_id")
    body = args.get("notes")
    if not mid:
        return {"error": "meeting_id is required"}
    if body is None:
        return {"error": "notes is required"}
    if not isinstance(body, str):
        return {"error": "notes must be a string"}
    with SessionStore() as store:
        session = store.get_session(mid)
        if not session:
            return {"error": f"no meeting with id {mid}"}
        archive = TranscriptStore(mid).save_notes(body, archive_existing=True)
        store.update_session(mid, has_notes=True)
    return {
        "meeting_id": mid,
        "bytes_written": len(body.encode("utf-8")),
        "archived_previous": str(archive) if archive else None,
    }


def set_prompt_template(args: dict) -> dict:
    """Record which prompt template to use for a given meeting."""
    mid = args.get("meeting_id")
    name = args.get("template_name", "")
    if not mid:
        return {"error": "meeting_id is required"}
    with SessionStore() as store:
        session = store.get_session(mid)
    if not session:
        return {"error": f"no meeting with id {mid}"}
    if name:
        tmpl = prompts_mod.get_template(name)
        if tmpl is None:
            return {"error": f"no template named '{name}'"}
    TranscriptStore(mid).write_prompt_template_name(name)
    return {"meeting_id": mid, "template_name": name}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _schema(properties: dict, required: Optional[list[str]] = None) -> dict:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema


TOOLS: list[Tool] = [
    Tool(
        name="list_meetings",
        description=(
            "List recent meetings, newest first. Use to find a meeting id "
            "before calling get_transcript or render_synthesis_prompt."
        ),
        input_schema=_schema(
            {
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
                "state": {
                    "type": "string",
                    "enum": list(ALL_STATES),
                    "description": "Lifecycle state filter (optional).",
                },
                "folder_id": {
                    "type": "string",
                    "description": "Folder filter; empty string returns unfiled only.",
                },
            }
        ),
        handler=list_meetings,
    ),
    Tool(
        name="get_meeting",
        description="Fetch a single meeting's metadata plus parsed attendees.",
        input_schema=_schema({"meeting_id": {"type": "string"}}, required=["meeting_id"]),
        handler=get_meeting,
    ),
    Tool(
        name="get_transcript",
        description="Return a meeting's raw transcript text (raw.transcript.md).",
        input_schema=_schema({"meeting_id": {"type": "string"}}, required=["meeting_id"]),
        handler=get_transcript,
    ),
    Tool(
        name="get_live_notes",
        description=(
            "Return a meeting's live-notes buffer, the user-authored "
            "Attendees / Agenda / Notes / Action Items body."
        ),
        input_schema=_schema({"meeting_id": {"type": "string"}}, required=["meeting_id"]),
        handler=get_live_notes,
    ),
    Tool(
        name="get_notes",
        description="Return a meeting's synthesized notes (notes.md), if any.",
        input_schema=_schema({"meeting_id": {"type": "string"}}, required=["meeting_id"]),
        handler=get_notes,
    ),
    Tool(
        name="list_templates",
        description="List available synthesis prompt templates.",
        input_schema=_schema({}),
        handler=list_templates,
    ),
    Tool(
        name="get_template",
        description="Return a prompt template's raw body.",
        input_schema=_schema({"name": {"type": "string"}}, required=["name"]),
        handler=get_template,
    ),
    Tool(
        name="render_synthesis_prompt",
        description=(
            "Render a ready-to-execute synthesis prompt for a meeting. "
            "Feed the returned text to your own model to generate notes, "
            "then call save_notes with the result."
        ),
        input_schema=_schema(
            {
                "meeting_id": {"type": "string"},
                "template_name": {
                    "type": "string",
                    "description": "Optional override; defaults to the per-meeting choice.",
                },
            },
            required=["meeting_id"],
        ),
        handler=render_synthesis_prompt,
    ),
    Tool(
        name="save_notes",
        description=(
            "Write synthesized notes to the meeting's notes.md. Any "
            "existing notes.md is archived first; the operation is reversible."
        ),
        input_schema=_schema(
            {
                "meeting_id": {"type": "string"},
                "notes": {
                    "type": "string",
                    "description": "Markdown body to persist.",
                },
            },
            required=["meeting_id", "notes"],
        ),
        handler=save_notes,
    ),
    Tool(
        name="set_prompt_template",
        description="Record which prompt template to use for a given meeting.",
        input_schema=_schema(
            {
                "meeting_id": {"type": "string"},
                "template_name": {
                    "type": "string",
                    "description": "Template name; empty string clears the override.",
                },
            },
            required=["meeting_id"],
        ),
        handler=set_prompt_template,
    ),
]


def tool_by_name(name: str) -> Optional[Tool]:
    for t in TOOLS:
        if t.name == name:
            return t
    return None
