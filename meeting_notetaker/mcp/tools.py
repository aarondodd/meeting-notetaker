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
        series_id: optional int filter; only meetings tagged with this series.
        contact_id: optional int filter; only meetings this contact attended.
        topic_id: optional int filter; only meetings tagged with this topic.
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

    # Classification filters are opt-in: no store open unless at least
    # one id was passed. Keeps the common unfiltered path on the single
    # SessionStore query the stdio clients already measured against.
    allowed_ids: Optional[set[str]] = None
    for key, lookup in (
        ("series_id", "session_ids_for_series"),
        ("contact_id", "session_ids_for_contact"),
        ("topic_id", "session_ids_for_topic"),
    ):
        raw = args.get(key)
        if raw in (None, "", "any"):
            continue
        try:
            class_id = int(raw)
        except (TypeError, ValueError):
            return {"error": f"{key} must be an integer or empty"}
        try:
            from ..models.classification import ClassificationStore  # noqa: PLC0415
            with ClassificationStore() as cstore:
                ids = set(getattr(cstore, lookup)(class_id))
        except Exception as exc:
            return {"error": f"classification lookup failed: {exc}"}
        allowed_ids = ids if allowed_ids is None else allowed_ids & ids

    with SessionStore() as store:
        if folder_filter is None:
            sessions = store.list_sessions()
        else:
            sessions = store.list_sessions(folder_id=folder_filter or None)
        if state is not None:
            sessions = [s for s in sessions if s.state == state]
        if allowed_ids is not None:
            sessions = [s for s in sessions if s.id in allowed_ids]
        return {"meetings": [_session_summary(s) for s in sessions[:limit]]}


def list_classification(_args: dict) -> dict:
    """Return the series / contacts / topics currently in use.

    The web UI feeds these into the /meetings filter dropdowns; the
    stdio clients can call it to let a model name a filter by text.
    "In use" means the row has at least one session tagged with it,
    which matches the Python app's navigator convention (offering a
    classification value that returns zero rows wastes a click).
    """
    try:
        from ..models.classification import ClassificationStore  # noqa: PLC0415
        with ClassificationStore() as cstore:
            series = [
                {"id": s.id, "name": s.name}
                for s in cstore.list_series_in_use()
            ]
            contacts = [
                {"id": c.id, "display_name": c.display_name}
                for c in cstore.list_contacts_in_use()
            ]
            topics = [
                {"id": t.id, "name": t.name}
                for t in cstore.list_topics_in_use()
            ]
    except Exception as exc:
        return {"error": f"classification store unavailable: {exc}"}
    return {"series": series, "contacts": contacts, "topics": topics}


def export_meeting_to_obsidian(args: dict) -> dict:
    """Publish a meeting's current synthesis to the configured Obsidian vault.

    Reads the vault config from the user's live Config and lets the
    Obsidian integration decide the subfolder + filename from the
    configured location template. Fails cleanly if the vault is not
    configured or no notes.md exists.
    """
    mid = args.get("meeting_id")
    if not mid:
        return {"error": "meeting_id is required"}
    config = Config.load()
    vault_root = (config.obsidian.vault_root or "").strip()
    if not vault_root:
        return {
            "error": (
                "Obsidian vault is not configured. Set the vault path in "
                "Settings > Obsidian first."
            )
        }
    from pathlib import Path  # noqa: PLC0415
    vault_path = Path(vault_root)
    if not vault_path.is_dir():
        return {
            "error": (
                f"Configured Obsidian vault_root does not exist: {vault_root}"
            )
        }
    with SessionStore() as store:
        session = store.get_session(mid)
    if not session:
        return {"error": f"no meeting with id {mid}"}
    tstore = TranscriptStore(mid)
    body = tstore.read_notes()
    if not body.strip():
        return {
            "error": (
                "No synthesis saved yet; use save_notes before exporting."
            )
        }
    try:
        from ..integrations.obsidian_export import (  # noqa: PLC0415
            ObsidianPublishOptions,
            ObsidianSessionInfo,
            export_to_obsidian,
            resolve_location_template,
            resolve_filename_template,
            sanitize_obsidian_stem,
            truncate_filename_stem,
        )
    except Exception as exc:
        return {"error": f"obsidian_export unavailable: {exc}"}

    attendees = parse_attendees(tstore.read_live_notes())
    started_at = None
    if session.started_at:
        try:
            started_at = datetime.fromisoformat(session.started_at)
        except ValueError:
            started_at = None
    duration_minutes = None
    if session.duration_seconds:
        duration_minutes = max(1, int(round(session.duration_seconds / 60)))

    # Enrich with series + topics when available, so wikilinks and
    # frontmatter land the same way the GUI's Save to Obsidian path
    # does. A missing classification store is a quiet downgrade; the
    # fields default to empty.
    series_name = ""
    topic_names: list[str] = []
    try:
        from ..models.classification import ClassificationStore  # noqa: PLC0415
        with ClassificationStore() as cstore:
            series = cstore.series_for_session(mid)
            if series:
                series_name = series.name
            topic_names = [
                t.name for t in cstore.topics_for_session(mid)
            ]
    except Exception:
        pass

    sess_info = ObsidianSessionInfo(
        session_id=session.id,
        title=session.title or "Untitled",
        started_at=started_at,
        duration_minutes=duration_minutes,
        attendees=attendees,
        series=series_name,
        tags=topic_names,
    )

    sub = resolve_location_template(
        template_name=config.obsidian.location_template_name,
        template_custom=config.obsidian.location_template_custom,
        session=sess_info,
        when=started_at,
    )
    stem = truncate_filename_stem(
        sanitize_obsidian_stem(
            resolve_filename_template(
                template_name=config.obsidian.location_template_name,
                template_custom=config.obsidian.location_template_custom,
                session=sess_info,
                when=started_at,
            ),
            fallback=session.id,
        )
    )
    options = ObsidianPublishOptions(
        vault_root=vault_path,
        vault_name=config.obsidian.vault_name or vault_path.name,
        target_subdir=sub,
        filename_stem=stem,
        write_frontmatter=config.obsidian.write_frontmatter,
        wikilink_attendees=config.obsidian.wikilink_attendees,
        wikilink_series=config.obsidian.wikilink_series,
        include_classification=config.obsidian.include_classification,
        include_attachments=config.obsidian.default_include_attachments,
        daily_note_backlink=config.obsidian.daily_note_backlink,
        open_after_save=False,  # web UI never opens the vault URI itself
        on_conflict="save_as_new",
        source_tab="Synthesis",
    )
    try:
        from ..utils.paths import sessions_dir  # noqa: PLC0415
        session_dir = sessions_dir() / session.id
        result = export_to_obsidian(
            session=sess_info,
            body=body,
            options=options,
            session_dir=session_dir,
            attachments=[],
            when=started_at,
            location_template_name=config.obsidian.location_template_name,
            location_template_custom=config.obsidian.location_template_custom,
        )
    except Exception as exc:
        return {"error": f"obsidian export failed: {exc}"}
    return {
        "meeting_id": mid,
        "vault_root": str(vault_path),
        "vault_name": options.vault_name,
        "page_url": result.page_url,
        "title": result.title,
        "message": result.message,
    }


def _pick_integration_target(entries: list[dict], favorites: list[dict]) -> Optional[dict]:
    """Pick a saved target page from recents, then favorites.

    Returns the raw entry dict (``{id, title, used_at?, extra?}``) or
    None when nothing has been saved. Used by the Notion + Confluence
    web-UI export paths, which have no interactive picker to drill into.
    """
    for entry in entries:
        if entry.get("id"):
            return entry
    for entry in favorites:
        if entry.get("id"):
            return entry
    return None


def export_meeting_to_notion(args: dict) -> dict:
    """Publish a meeting's current synthesis to Notion.

    Targets the most-recent (or first favorite) parent page saved by
    the Python app's picker. Fails cleanly when Notion is not verified
    or no target has been saved yet.
    """
    mid = args.get("meeting_id")
    if not mid:
        return {"error": "meeting_id is required"}
    config = Config.load()
    nc = config.notion
    if not ((nc.api_token or "").strip() and (nc.last_verified_at or "").strip()):
        return {
            "error": (
                "Notion is not configured. Verify the connection in "
                "Settings > Integrations first."
            )
        }
    target = _pick_integration_target(nc.recents, nc.favorites)
    if not target:
        return {
            "error": (
                "No Notion target page saved yet. Export once from the "
                "Python app (Session view > Save to Notion) to seed a "
                "recent or favorite parent."
            )
        }
    with SessionStore() as store:
        session = store.get_session(mid)
    if not session:
        return {"error": f"no meeting with id {mid}"}
    body = TranscriptStore(mid).read_notes()
    if not body.strip():
        return {
            "error": (
                "No synthesis saved yet; use save_notes before exporting."
            )
        }
    try:
        from ..integrations.notion_api import NotionClient  # noqa: PLC0415
        from ..integrations.export import export_to_notion  # noqa: PLC0415
        from ..utils.paths import sessions_dir  # noqa: PLC0415
    except Exception as exc:
        return {"error": f"notion export unavailable: {exc}"}
    client = NotionClient(nc.api_token)
    title = session.title or "Untitled meeting"
    try:
        result = export_to_notion(
            client=client,
            parent_id=target["id"],
            title=title,
            markdown_body=body,
            session_dir=sessions_dir() / session.id,
            attachments=[],
            number_headings=bool(config.synthesis.heading_numbering),
            include_toc=bool(config.synthesis.toc_in_exports),
            toc_max_depth=int(config.synthesis.toc_max_depth or 3),
        )
    except Exception as exc:
        return {"error": f"notion export failed: {exc}"}
    return {
        "meeting_id": mid,
        "target": result.target,
        "parent_id": target["id"],
        "parent_title": target.get("title") or "",
        "page_id": result.page_id,
        "page_url": result.page_url,
        "title": result.title,
        "message": result.message,
    }


def export_meeting_to_confluence(args: dict) -> dict:
    """Publish a meeting's current synthesis to Confluence.

    Targets the most-recent (or first favorite) parent page saved by
    the Python app's picker; the parent's space_id is read from the
    saved extra. Fails cleanly when Confluence is not verified, no
    target has been saved, or the saved target has no space_id.
    """
    mid = args.get("meeting_id")
    if not mid:
        return {"error": "meeting_id is required"}
    config = Config.load()
    cc = config.confluence
    if not (
        (cc.base_url or "").strip()
        and (cc.email or "").strip()
        and (cc.api_token or "").strip()
        and (cc.last_verified_at or "").strip()
    ):
        return {
            "error": (
                "Confluence is not configured. Verify the connection in "
                "Settings > Integrations first."
            )
        }
    target = _pick_integration_target(cc.recents, cc.favorites)
    if not target:
        return {
            "error": (
                "No Confluence target page saved yet. Export once from "
                "the Python app (Session view > Save to Confluence) to "
                "seed a recent or favorite parent."
            )
        }
    extra = target.get("extra") or {}
    space_id = (extra.get("space_id") or "").strip()
    if not space_id:
        return {
            "error": (
                "The saved Confluence target has no space_id. Re-pick a "
                "parent page from the Python app so the space is "
                "recorded alongside it."
            )
        }
    with SessionStore() as store:
        session = store.get_session(mid)
    if not session:
        return {"error": f"no meeting with id {mid}"}
    body = TranscriptStore(mid).read_notes()
    if not body.strip():
        return {
            "error": (
                "No synthesis saved yet; use save_notes before exporting."
            )
        }
    try:
        from ..integrations.confluence_api import ConfluenceClient  # noqa: PLC0415
        from ..integrations.export import export_to_confluence  # noqa: PLC0415
        from ..utils.paths import sessions_dir  # noqa: PLC0415
    except Exception as exc:
        return {"error": f"confluence export unavailable: {exc}"}
    client = ConfluenceClient(cc.base_url, cc.email, cc.api_token)
    title = session.title or "Untitled meeting"
    try:
        result = export_to_confluence(
            client=client,
            parent_id=target["id"],
            space_id=space_id,
            title=title,
            markdown_body=body,
            session_dir=sessions_dir() / session.id,
            attachments=[],
            number_headings=bool(config.synthesis.heading_numbering),
            include_toc=bool(config.synthesis.toc_in_exports),
            toc_max_depth=int(config.synthesis.toc_max_depth or 3),
        )
    except Exception as exc:
        return {"error": f"confluence export failed: {exc}"}
    return {
        "meeting_id": mid,
        "target": result.target,
        "parent_id": target["id"],
        "parent_title": target.get("title") or "",
        "space_id": space_id,
        "page_id": result.page_id,
        "page_url": result.page_url,
        "title": result.title,
        "message": result.message,
    }


_EXPORT_DESTINATIONS = ("obsidian", "notion", "confluence")


def list_export_destinations(_args: dict) -> dict:
    """List the export destinations the web UI should offer.

    Each entry reports whether the destination is configured (its own
    Settings pane is wired up) and whether the user has enabled it for
    the web UI dropdown (``Settings > Synthesis > Web UI export
    destinations``). The handler also carries the display label and,
    for Notion/Confluence, the saved target page title so the dropdown
    row can show "Notion -- Projects/Meetings" without an API call.
    """
    try:
        config = Config.load()
    except Exception as exc:
        return {"error": f"config unavailable: {exc}"}
    destinations: list[dict] = []
    obs_configured = bool((config.obsidian.vault_root or "").strip())
    destinations.append({
        "key": "obsidian",
        "label": "Obsidian vault",
        "configured": obs_configured,
        "enabled": bool(config.synthesis.web_ui_export_obsidian),
        "target_title": config.obsidian.vault_name or "",
    })
    notion_configured = bool(
        (config.notion.api_token or "").strip()
        and (config.notion.last_verified_at or "").strip()
    )
    notion_target = _pick_integration_target(
        config.notion.recents, config.notion.favorites,
    )
    destinations.append({
        "key": "notion",
        "label": "Notion",
        "configured": notion_configured and bool(notion_target),
        "enabled": bool(config.synthesis.web_ui_export_notion),
        "target_title": (notion_target or {}).get("title", ""),
    })
    conf_configured = bool(
        (config.confluence.base_url or "").strip()
        and (config.confluence.email or "").strip()
        and (config.confluence.api_token or "").strip()
        and (config.confluence.last_verified_at or "").strip()
    )
    conf_target = _pick_integration_target(
        config.confluence.recents, config.confluence.favorites,
    )
    conf_target_has_space = bool(
        conf_target
        and (conf_target.get("extra") or {}).get("space_id")
    )
    destinations.append({
        "key": "confluence",
        "label": "Confluence",
        "configured": conf_configured and conf_target_has_space,
        "enabled": bool(config.synthesis.web_ui_export_confluence),
        "target_title": (conf_target or {}).get("title", ""),
    })
    return {"destinations": destinations}


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
                "series_id": {
                    "type": "integer",
                    "description": "Classification series id; omit for any series.",
                },
                "contact_id": {
                    "type": "integer",
                    "description": "Classification contact id; omit for any contact.",
                },
                "topic_id": {
                    "type": "integer",
                    "description": "Classification topic id; omit for any topic.",
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
    Tool(
        name="list_classification",
        description=(
            "List classification values currently in use (series, contacts, "
            "topics). Use before calling list_meetings with series_id, "
            "contact_id, or topic_id to turn a user's text into the id."
        ),
        input_schema=_schema({}),
        handler=list_classification,
    ),
    Tool(
        name="export_meeting_to_obsidian",
        description=(
            "Publish a meeting's current synthesis to the configured "
            "Obsidian vault. Reads the vault location, filename template, "
            "and frontmatter flags from Settings > Obsidian. Fails if the "
            "vault is unconfigured or the meeting has no saved synthesis."
        ),
        input_schema=_schema(
            {"meeting_id": {"type": "string"}}, required=["meeting_id"],
        ),
        handler=export_meeting_to_obsidian,
    ),
    Tool(
        name="export_meeting_to_notion",
        description=(
            "Publish a meeting's current synthesis to Notion as a new "
            "page under the most-recent (or first favorite) parent "
            "saved by the Python app's picker. Fails cleanly when "
            "Notion is not verified or no target has been saved yet."
        ),
        input_schema=_schema(
            {"meeting_id": {"type": "string"}}, required=["meeting_id"],
        ),
        handler=export_meeting_to_notion,
    ),
    Tool(
        name="export_meeting_to_confluence",
        description=(
            "Publish a meeting's current synthesis to Confluence as a "
            "new page under the most-recent (or first favorite) parent "
            "saved by the Python app's picker. Fails cleanly when "
            "Confluence is not verified or no target has been saved yet."
        ),
        input_schema=_schema(
            {"meeting_id": {"type": "string"}}, required=["meeting_id"],
        ),
        handler=export_meeting_to_confluence,
    ),
    Tool(
        name="list_export_destinations",
        description=(
            "List the export destinations the web UI exposes to the "
            "Chrome Claude extension. Returns per-destination "
            "configured + enabled booleans plus the display label and "
            "the saved target page title where applicable."
        ),
        input_schema=_schema({}),
        handler=list_export_destinations,
    ),
]


def tool_by_name(name: str) -> Optional[Tool]:
    for t in TOOLS:
        if t.name == name:
            return t
    return None
