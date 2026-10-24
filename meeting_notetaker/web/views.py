"""HTML rendering for the web UI.

Server-side only. No client JS; every mutation is a plain form POST
followed by a 303 redirect, which keeps the surface small enough that
``read_page`` finds everything in the DOM and ``form_input`` finds the
textarea without a selector race.
"""
from __future__ import annotations

import html
from typing import Iterable

from ..models.session import ALL_STATES
from .static import STYLE_CSS


def _esc(value) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def render_markdown(body: str) -> str:
    """Return a ``<div class="md">...</div>`` block rendering ``body``.

    Uses mistune 3 with ``escape=True`` so any raw HTML or injected
    `<script>` from the model's output is neutralized at parse time;
    the hostile-input tests under ``test_web_views.py`` already pin
    this behavior for the detail page. When mistune is not importable
    (defensive: it is in requirements.txt and the pyinstaller hints),
    falls back to the previous ``<pre>...</pre>`` presentation so the
    page still renders the content.
    """
    if not body:
        return ""
    try:
        import mistune  # noqa: PLC0415
    except Exception:  # pragma: no cover - mistune ships in requirements
        return f'<pre class="md-fallback">{_esc(body)}</pre>'
    try:
        md = mistune.create_markdown(
            escape=True,
            plugins=["strikethrough", "table", "task_lists", "url"],
        )
        rendered = md(body)
    except Exception:  # pragma: no cover
        return f'<pre class="md-fallback">{_esc(body)}</pre>'
    return f'<div class="md">{rendered}</div>'


def _page(title: str, body: str, flash: str | None = None) -> str:
    flash_html = ""
    if flash:
        kind, msg = flash
        flash_html = f'<div class="flash {_esc(kind)}">{_esc(msg)}</div>'
    return (
        "<!doctype html>\n"
        "<html lang=\"en\">\n"
        "<head>\n"
        "  <meta charset=\"utf-8\">\n"
        "  <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n"
        f"  <title>{_esc(title)}</title>\n"
        "  <link rel=\"stylesheet\" href=\"/static/style.css\">\n"
        "</head>\n"
        "<body>\n"
        "  <header>\n"
        "    <div class=\"title\">Meeting Notetaker</div>\n"
        "    <div class=\"sub\">\n"
        "      <a href=\"/meetings\">All meetings</a>\n"
        "      <a href=\"/templates\">Templates</a>\n"
        "    </div>\n"
        "  </header>\n"
        "  <main>\n"
        f"    {flash_html}\n"
        f"    {body}\n"
        "    <div class=\"footer\">Local, loopback-only. "
        "Data path: %APPDATA%\\MeetingNotetaker (or $MEETING_NOTETAKER_DATA_DIR).</div>\n"
        "  </main>\n"
        "</body>\n"
        "</html>\n"
    )


def _state_badge(state: str) -> str:
    cls = _esc(state) if state in ALL_STATES else "state"
    return f'<span class="badge state {cls}">{_esc(state)}</span>'


def _yesno_badge(flag: bool, label_yes: str = "yes", label_no: str = "no") -> str:
    if flag:
        return f'<span class="badge has">{_esc(label_yes)}</span>'
    return f'<span class="badge hasnt">{_esc(label_no)}</span>'


# ---------------------------------------------------------------------------
# List page
# ---------------------------------------------------------------------------


def _classification_options(
    kind_label: str,
    rows: list[dict],
    name_key: str,
    current: str,
) -> str:
    """Build an "(any)" + sorted-row options list for a classification select.

    ``current`` is the str() of the selected id as it comes off the query
    string; an empty string / "any" keeps the (any) default selected.
    """
    opts = [
        '<option value=""'
        + ("" if current and current != "any" else " selected")
        + '>(any)</option>'
    ]
    for row in rows:
        rid = str(row["id"])
        sel = " selected" if current == rid else ""
        opts.append(
            f'<option value="{_esc(rid)}"{sel}>{_esc(row[name_key])}</option>'
        )
    return "".join(opts)


def render_meeting_list(
    *,
    meetings: list[dict],
    folders: list[dict],
    filters: dict,
    total_shown: int,
    classification: dict | None = None,
    obsidian_configured: bool = False,
) -> str:
    """Render /meetings. ``filters`` carries the live query string so the
    form repopulates after submit; ``folders`` is the full folder list,
    ``classification`` is ``{"series": [...], "contacts": [...], "topics": [...]}``
    for the series / person / topic dropdowns, and ``obsidian_configured``
    gates the per-row Save to Obsidian button."""
    classification = classification or {"series": [], "contacts": [], "topics": []}
    options = "".join(
        f'<option value="{_esc(s)}"'
        f'{" selected" if filters.get("state") == s else ""}>{_esc(s)}</option>'
        for s in ALL_STATES
    )
    folder_options = (
        '<option value="">(any)</option>'
        '<option value=""'
        f'{" selected" if filters.get("folder_id") == "" and "folder_id" in filters else ""}'
        '>-- unfiled only --</option>'
    )
    for folder in folders:
        folder_options += (
            f'<option value="{_esc(folder["id"])}"'
            f'{" selected" if filters.get("folder_id") == folder["id"] else ""}'
            f'>{_esc(folder["name"])}</option>'
        )
    series_options = _classification_options(
        "series", classification.get("series", []), "name",
        filters.get("series_id", ""),
    )
    contact_options = _classification_options(
        "contact", classification.get("contacts", []), "display_name",
        filters.get("contact_id", ""),
    )
    topic_options = _classification_options(
        "topic", classification.get("topics", []), "name",
        filters.get("topic_id", ""),
    )
    limit_val = _esc(filters.get("limit", 50))
    has_transcript_checked = (
        " checked" if filters.get("has_transcript") == "1" else ""
    )
    has_notes_checked = " checked" if filters.get("has_notes") == "1" else ""
    unselected_state = "" if filters.get("state") else " selected"
    filters_html = (
        '<form class="filters" method="get" action="/meetings">\n'
        '  <label>State '
        f'<select name="state"><option value=""{unselected_state}>(any)</option>'
        f'{options}</select></label>\n'
        '  <label>Folder '
        f'<select name="folder_id">{folder_options}</select></label>\n'
        '  <label>Series '
        f'<select name="series_id">{series_options}</select></label>\n'
        '  <label>Person '
        f'<select name="contact_id">{contact_options}</select></label>\n'
        '  <label>Topic '
        f'<select name="topic_id">{topic_options}</select></label>\n'
        '  <label><input type="checkbox" name="has_transcript" value="1"'
        f'{has_transcript_checked}> has transcript</label>\n'
        '  <label><input type="checkbox" name="has_notes" value="1"'
        f'{has_notes_checked}> has synthesis</label>\n'
        '  <label>Limit '
        f'<input type="number" name="limit" min="1" max="200" value="{limit_val}"></label>\n'
        '  <button type="submit">Apply</button>\n'
        '  <a href="/meetings" class="row-link">Reset</a>\n'
        '</form>'
    )
    if not meetings:
        rows_html = '<tr><td colspan="7" style="color:#6e7781">No meetings match these filters.</td></tr>'
    else:
        rows: list[str] = []
        for m in meetings:
            mid = _esc(m["id"])
            has_notes = bool(m.get("has_notes"))
            if obsidian_configured and has_notes:
                obsidian_cell = (
                    f'<form method="post" class="inline-form" '
                    f'action="/meetings/{mid}/export/obsidian">'
                    '<button type="submit" class="secondary tiny">To Obsidian</button>'
                    '</form>'
                )
            elif obsidian_configured:
                obsidian_cell = (
                    '<span class="muted" title="Save a synthesis first; the row-level '
                    'button writes notes.md to the configured vault.">needs synthesis</span>'
                )
            else:
                obsidian_cell = (
                    '<span class="muted" title="Set Obsidian vault_root in Settings.">'
                    'not configured</span>'
                )
            rows.append(
                "<tr>\n"
                f'  <td><a class="row-link" href="/meetings/{mid}">'
                f'{_esc(m.get("title") or "(untitled)")}</a></td>\n'
                f'  <td>{_esc((m.get("started_at") or m.get("created_at") or "")[:19])}</td>\n'
                f"  <td>{_state_badge(m.get('state', ''))}</td>\n"
                f"  <td>{_yesno_badge(bool(m.get('has_transcript')))}</td>\n"
                f"  <td>{_yesno_badge(has_notes, 'synthesized', 'none')}</td>\n"
                f'  <td>{_esc(m.get("duration_seconds") or "")}</td>\n'
                f'  <td>{obsidian_cell}</td>\n'
                "</tr>"
            )
        rows_html = "\n".join(rows)
    table_html = (
        "<table>\n"
        "<thead><tr>"
        "<th>Title</th><th>When</th><th>State</th>"
        "<th>Transcript</th><th>Synthesis</th><th>Duration (s)</th>"
        "<th>Export</th>"
        "</tr></thead>\n"
        f"<tbody>\n{rows_html}\n</tbody>\n"
        "</table>"
    )
    body = (
        "<section>\n"
        "  <h2>Filters</h2>\n"
        f"  {filters_html}\n"
        "</section>\n"
        "<section>\n"
        f"  <h2>{_esc(total_shown)} meeting(s)</h2>\n"
        f"  {table_html}\n"
        "</section>"
    )
    return _page("Meetings", body)


# ---------------------------------------------------------------------------
# Detail page
# ---------------------------------------------------------------------------


def _attendees_list(attendees: Iterable[str]) -> str:
    items = [f"<li>{_esc(a)}</li>" for a in attendees if a]
    if not items:
        return '<p style="color:#6e7781;font-size:13px;margin:0">No attendees parsed from live notes.</p>'
    return '<ul class="attendees">\n' + "\n".join(items) + "\n</ul>"


def render_meeting_detail(
    *,
    meeting: dict,
    transcript: str,
    live_notes: str,
    existing_notes: str,
    prompt_text: str,
    prompt_template_name: str,
    templates: list[dict],
    flash: tuple[str, str] | None = None,
    obsidian_configured: bool = False,
) -> str:
    mid = meeting["id"]
    title = meeting.get("title") or "(untitled)"
    meta_cells = [
        ("Session id", mid),
        ("State", _state_badge(meeting.get("state", ""))),
        ("Started", meeting.get("started_at") or "-"),
        ("Ended", meeting.get("ended_at") or "-"),
        ("Duration (s)", meeting.get("duration_seconds") or "-"),
        ("Has transcript", _yesno_badge(bool(meeting.get("has_transcript")))),
        ("Has synthesis", _yesno_badge(bool(meeting.get("has_notes")))),
        ("Folder id", meeting.get("folder_id") or "-"),
    ]
    meta_html = "".join(
        f'<div class="cell"><div class="lbl">{_esc(label)}</div>'
        f'<div>{value if "<span" in str(value) else _esc(value)}</div></div>'
        for label, value in meta_cells
    )
    attendees = meeting.get("attendees") or []
    tmpl_options = []
    tmpl_options.append(
        '<option value=""'
        + (" selected" if not prompt_template_name else "")
        + ">(use default)</option>"
    )
    for tmpl in templates:
        sel = " selected" if tmpl["name"] == prompt_template_name else ""
        tmpl_options.append(
            f'<option value="{_esc(tmpl["name"])}"{sel}>'
            f'{_esc(tmpl.get("display_name") or tmpl["name"])}</option>'
        )
    template_picker_html = (
        f'<form class="inline" method="post" action="/meetings/{_esc(mid)}/template">\n'
        f'  <label for="template-select">Template:</label>\n'
        f'  <select id="template-select" name="template_name">{"".join(tmpl_options)}</select>\n'
        '  <button type="submit" class="secondary">Set template</button>\n'
        '</form>'
    )

    # Synthesis + live notes render markdown richly (headings, lists,
    # tables, strikethrough, task lists, autolinks). The transcript
    # stays <pre> because it is a plain log, not Markdown. render_markdown
    # uses escape=True so raw HTML in the body is neutralized, matching
    # the hostile-input pin for the detail page.
    if existing_notes:
        existing_html = render_markdown(existing_notes)
    else:
        existing_html = (
            '<p style="color:#6e7781;font-size:13px;margin:0">'
            'No synthesis saved yet.</p>'
        )
    if live_notes:
        live_notes_html = render_markdown(live_notes)
    else:
        live_notes_html = '<pre>(empty)</pre>'

    # Save-to-Obsidian form (detail page). Button is disabled with a
    # tooltip when the vault is unconfigured, when the meeting has no
    # synthesis yet, or when the handler has nothing to export.
    has_notes = bool(meeting.get("has_notes")) or bool(existing_notes)
    if obsidian_configured and has_notes:
        obsidian_section = (
            '<section>\n'
            '  <h2>Export to Obsidian</h2>\n'
            '  <p style="font-size:13px;color:#57606a;margin:0 0 10px">'
            'Writes the current synthesis to your configured vault using the '
            'location template and frontmatter rules from Settings &gt; Obsidian.</p>\n'
            f'  <form method="post" action="/meetings/{_esc(mid)}/export/obsidian">\n'
            '    <button type="submit">Save to Obsidian</button>\n'
            '  </form>\n'
            '</section>\n'
        )
    elif obsidian_configured:
        obsidian_section = (
            '<section>\n'
            '  <h2>Export to Obsidian</h2>\n'
            '  <p style="font-size:13px;color:#6e7781;margin:0">'
            'Save a synthesis below first; the export writes the current '
            'notes.md to the vault.</p>\n'
            '</section>\n'
        )
    else:
        obsidian_section = (
            '<section>\n'
            '  <h2>Export to Obsidian</h2>\n'
            '  <p style="font-size:13px;color:#6e7781;margin:0">'
            'Obsidian vault is not configured. Open Meeting Notetaker &gt; '
            'Settings &gt; Obsidian and set the vault path.</p>\n'
            '</section>\n'
        )

    body = (
        f'<section>\n  <h2>{_esc(title)}</h2>\n'
        f'  <div class="meta-grid">{meta_html}</div>\n'
        '</section>\n'
        '<section>\n  <h2>Attendees</h2>\n'
        f'  {_attendees_list(attendees)}\n'
        '</section>\n'
        '<section>\n  <h2>Live notes</h2>\n'
        '  <details><summary>Show / hide</summary>\n'
        f'  {live_notes_html}\n'
        '  </details>\n'
        '</section>\n'
        '<section>\n  <h2>Transcript</h2>\n'
        '  <details><summary>Show / hide</summary>\n'
        f'  <pre>{_esc(transcript) or "(empty)"}</pre>\n'
        '  </details>\n'
        '</section>\n'
        '<section>\n  <h2>Current synthesis</h2>\n'
        f'  {existing_html}\n'
        '</section>\n'
        f'{obsidian_section}'
        '<section>\n  <h2>Synthesis workflow</h2>\n'
        f'  <p style="font-size:13px;color:#57606a;margin-top:0">Template to use for the prompt below. Changing this re-renders the prompt on reload.</p>\n'
        f'  {template_picker_html}\n'
        '  <h3 style="font-size:13px;margin-top:14px;color:#57606a;text-transform:uppercase;letter-spacing:0.03em">Rendered prompt</h3>\n'
        f'  <pre id="rendered-prompt">{_esc(prompt_text) or "(no transcript or template available)"}</pre>\n'
        '  <h3 style="font-size:13px;margin-top:14px;color:#57606a;text-transform:uppercase;letter-spacing:0.03em">Synthesis</h3>\n'
        f'  <form method="post" action="/meetings/{_esc(mid)}/save">\n'
        '    <textarea class="notes" name="notes" id="synthesis" '
        'placeholder="Paste or type the synthesized notes here. Clicking Save writes notes.md and archives the previous version."></textarea>\n'
        '    <div style="margin-top:10px">\n'
        '      <button type="submit">Save synthesis</button>\n'
        f'      <a href="/meetings/{_esc(mid)}" class="row-link" style="margin-left:12px">Reload</a>\n'
        '    </div>\n'
        '  </form>\n'
        '</section>'
    )
    return _page(title, body, flash=flash)


# ---------------------------------------------------------------------------
# Templates page
# ---------------------------------------------------------------------------


def render_templates_list(templates: list[dict]) -> str:
    if not templates:
        rows = '<tr><td colspan="2" style="color:#6e7781">No templates configured.</td></tr>'
    else:
        rows = "\n".join(
            f'<tr><td>{_esc(t["name"])}</td><td>{_esc(t.get("display_name") or "")}</td></tr>'
            for t in templates
        )
    body = (
        "<section>\n"
        f"  <h2>{_esc(len(templates))} template(s)</h2>\n"
        '  <p style="font-size:13px;color:#57606a">Templates live in the user data directory under <code>prompts/</code>. Edit a file to change its wording.</p>\n'
        "  <table>\n"
        "    <thead><tr><th>Name</th><th>Display name</th></tr></thead>\n"
        f"    <tbody>{rows}</tbody>\n"
        "  </table>\n"
        "</section>"
    )
    return _page("Templates", body)


# ---------------------------------------------------------------------------
# Error page
# ---------------------------------------------------------------------------


def render_error(status: int, message: str) -> str:
    body = (
        '<section>\n'
        f'  <h2>HTTP {_esc(status)}</h2>\n'
        f'  <p>{_esc(message)}</p>\n'
        '</section>'
    )
    return _page(f"Error {status}", body)


__all__ = [
    "STYLE_CSS",
    "render_error",
    "render_meeting_detail",
    "render_meeting_list",
    "render_templates_list",
]
