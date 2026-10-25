"""HTML smoke tests for the web UI views.

The handlers tests cover routing + status codes; this file isolates
the rendering layer, which has no I/O. Enough to catch obvious
regressions (template, textarea, Save button, prompt pane).
"""
from __future__ import annotations

from meeting_notetaker.web import views


def test_list_page_handles_empty_input():
    html = views.render_meeting_list(
        meetings=[],
        folders=[],
        filters={},
        total_shown=0,
    )
    assert "<!doctype html>" in html
    assert "No meetings match" in html
    assert "/static/style.css" in html


def test_list_page_renders_rows_and_echoes_filters():
    html = views.render_meeting_list(
        meetings=[
            {
                "id": "sess-a",
                "title": "Alpha",
                "state": "complete",
                "created_at": "2026-10-23T01:02:03",
                "started_at": "2026-10-23T01:02:03",
                "has_transcript": True,
                "has_notes": False,
                "duration_seconds": 90,
            },
        ],
        filters={"state": "complete", "has_transcript": "1", "limit": "25"},
        total_shown=1,
    )
    assert "Alpha" in html
    assert "complete" in html
    # Filter echoed in form values.
    assert 'value="25"' in html
    assert 'checked' in html
    # Folder filter was removed in v0.8.1; no folder select rendered.
    assert 'name="folder_id"' not in html


def test_detail_page_has_save_form_and_prompt_pane():
    meeting = {
        "id": "mid",
        "title": "Detail test",
        "state": "complete",
        "started_at": "2026-10-23T01:02:03",
        "ended_at": "2026-10-23T02:02:03",
        "duration_seconds": 3600,
        "has_transcript": True,
        "has_notes": True,
        "folder_id": None,
        "attendees": ["Alex", "Me"],
    }
    html = views.render_meeting_detail(
        meeting=meeting,
        transcript="[00:00:00] Alex: hi",
        live_notes="# Attendees\n- Alex\n- Me",
        existing_notes="# Prior\nkeep",
        prompt_text="Prompt body",
        prompt_template_name="",
        templates=[{"name": "default", "display_name": "default (bundled)"}],
    )
    assert "Detail test" in html
    assert "Alex" in html
    assert "Me" in html
    assert "Prompt body" in html
    assert '<pre id="rendered-prompt">' in html
    assert 'name="notes"' in html
    assert 'action="/meetings/mid/save"' in html
    assert "Save synthesis" in html
    assert 'action="/meetings/mid/template"' in html


def test_detail_page_renders_flash_when_set():
    meeting = {
        "id": "mid",
        "title": "Flash",
        "state": "complete",
        "started_at": "",
        "ended_at": "",
        "duration_seconds": 0,
        "has_transcript": False,
        "has_notes": False,
        "attendees": [],
    }
    html = views.render_meeting_detail(
        meeting=meeting,
        transcript="",
        live_notes="",
        existing_notes="",
        prompt_text="",
        prompt_template_name="",
        templates=[],
        flash=("ok", "Synthesis saved; previous notes.md archived."),
    )
    assert 'class="flash ok"' in html
    assert "Synthesis saved" in html


def test_detail_page_handles_no_attendees_and_no_notes():
    meeting = {
        "id": "mid",
        "title": "Empty",
        "state": "new",
        "started_at": "",
        "ended_at": "",
        "duration_seconds": 0,
        "has_transcript": False,
        "has_notes": False,
        "attendees": [],
    }
    html = views.render_meeting_detail(
        meeting=meeting,
        transcript="",
        live_notes="",
        existing_notes="",
        prompt_text="",
        prompt_template_name="",
        templates=[],
    )
    assert "No attendees parsed" in html
    assert "No synthesis saved yet" in html


def test_templates_page_lists_names():
    html = views.render_templates_list(
        [
            {"name": "default", "display_name": "default (bundled)"},
            {"name": "status-update", "display_name": "Status update"},
        ]
    )
    assert "default (bundled)" in html
    assert "Status update" in html


def test_error_page_shows_status_and_message():
    html = views.render_error(500, "oh no")
    assert "500" in html
    assert "oh no" in html


def test_html_escapes_hostile_input():
    html = views.render_meeting_detail(
        meeting={
            "id": "mid",
            "title": "<script>alert(1)</script>",
            "state": "complete",
            "started_at": "",
            "ended_at": "",
            "duration_seconds": 0,
            "has_transcript": False,
            "has_notes": False,
            "attendees": ["</pre><img onerror=1>"],
        },
        transcript="</pre><script>evil</script>",
        live_notes="",
        existing_notes="",
        prompt_text="",
        prompt_template_name="",
        templates=[],
    )
    assert "<script>alert" not in html
    assert "&lt;script&gt;alert(1)" in html
    assert "&lt;/pre&gt;" in html


def test_render_markdown_renders_core_blocks():
    out = views.render_markdown(
        "# h1\n\n- a\n- b\n\n**bold** _italic_ ~~strike~~\n\n"
        "| col | val |\n|-----|-----|\n| x   | 1   |\n"
    )
    assert '<div class="md">' in out
    assert "<h1>h1</h1>" in out
    assert "<li>a</li>" in out
    assert "<strong>bold</strong>" in out
    assert "<em>italic</em>" in out
    assert "<del>strike</del>" in out
    assert "<table>" in out and "<th>col</th>" in out


def test_render_markdown_neutralizes_raw_html():
    out = views.render_markdown(
        "<script>alert(1)</script>\n\nfine body"
    )
    assert "<script>alert" not in out
    assert "&lt;script&gt;alert(1)" in out
    assert "fine body" in out


def test_render_markdown_empty_body_returns_empty_string():
    assert views.render_markdown("") == ""


def test_detail_renders_synthesis_as_markdown_container():
    meeting = {
        "id": "mid", "title": "Markdown case", "state": "complete",
        "started_at": "", "ended_at": "", "duration_seconds": 0,
        "has_transcript": True, "has_notes": True, "attendees": [],
    }
    html = views.render_meeting_detail(
        meeting=meeting,
        transcript="raw transcript\nline two",
        live_notes="# Attendees\n- Me",
        existing_notes="## Decisions\n\n- ship it",
        prompt_text="prompt",
        prompt_template_name="",
        templates=[],
    )
    # Synthesis + live notes render inside .md; transcript stays <pre>.
    assert '<div class="md">' in html
    assert "<h2>Decisions</h2>" in html
    assert "<li>ship it</li>" in html
    # Attendees from live_notes get the same treatment.
    assert "<h1>Attendees</h1>" in html
    # Transcript still raw <pre>.
    assert "raw transcript\nline two" in html


def test_list_renders_classification_filters():
    """The expanded filter form carries the three classification selects
    and no Folder / Export column (both dropped in v0.8.1)."""
    classification = {
        "series": [{"id": 1, "name": "Platform"}],
        "contacts": [{"id": 7, "display_name": "Alex"}],
        "topics": [{"id": 3, "name": "auth"}],
    }
    html = views.render_meeting_list(
        meetings=[
            {
                "id": "mid", "title": "sync", "state": "complete",
                "started_at": "2026-10-25T01:02:03",
                "created_at": "2026-10-25T01:02:03",
                "has_transcript": True, "has_notes": True,
                "duration_seconds": 60,
            },
        ],
        filters={"series_id": "1"},
        total_shown=1,
        classification=classification,
    )
    # Classification selects + their options.
    assert 'name="series_id"' in html
    assert 'value="1" selected' in html
    assert ">Platform<" in html
    assert 'name="contact_id"' in html and ">Alex<" in html
    assert 'name="topic_id"' in html and ">auth<" in html
    # Folder filter + per-row Export column were removed in v0.8.1.
    assert 'name="folder_id"' not in html
    assert "<th>Export</th>" not in html


def test_detail_renders_export_dropdown_with_destinations():
    """When at least one destination is enabled and configured AND the
    meeting has synthesis, the Export section renders a dropdown
    posting to /meetings/<id>/export."""
    meeting = {
        "id": "mid", "title": "sync", "state": "complete",
        "started_at": "", "ended_at": "", "duration_seconds": 0,
        "has_transcript": True, "has_notes": True, "attendees": [],
    }
    html = views.render_meeting_detail(
        meeting=meeting,
        transcript="t",
        live_notes="",
        existing_notes="body",
        prompt_text="",
        prompt_template_name="",
        templates=[],
        export_destinations=[
            {"key": "obsidian", "label": "Obsidian vault",
             "configured": True, "enabled": True, "target_title": "notes"},
            {"key": "notion", "label": "Notion",
             "configured": True, "enabled": True, "target_title": "Weekly"},
        ],
    )
    assert 'id="export"' in html
    assert 'action="/meetings/mid/export"' in html
    assert 'name="destination"' in html
    assert '<option value="obsidian">Obsidian vault -- notes</option>' in html
    assert '<option value="notion">Notion -- Weekly</option>' in html
    # Sidebar TOC carries each section's anchor.
    for anchor in ("#meta", "#attendees", "#transcript", "#export", "#workflow"):
        assert f'href="{anchor}"' in html


def test_detail_export_section_renders_no_destinations_hint():
    """With no destinations enabled, the Export section renders a
    hint pointing at Settings rather than a dropdown."""
    meeting = {
        "id": "mid", "title": "sync", "state": "new",
        "started_at": "", "ended_at": "", "duration_seconds": 0,
        "has_transcript": False, "has_notes": False, "attendees": [],
    }
    html = views.render_meeting_detail(
        meeting=meeting,
        transcript="",
        live_notes="",
        existing_notes="",
        prompt_text="",
        prompt_template_name="",
        templates=[],
        export_destinations=[],
    )
    assert 'id="export"' in html
    assert "No export destinations are enabled" in html
    assert 'name="destination"' not in html


def test_detail_export_section_renders_configured_but_no_notes_hint():
    """At least one destination enabled but the meeting has no
    synthesis yet: show the save-first hint, no dropdown."""
    meeting = {
        "id": "mid", "title": "sync", "state": "new",
        "started_at": "", "ended_at": "", "duration_seconds": 0,
        "has_transcript": False, "has_notes": False, "attendees": [],
    }
    html = views.render_meeting_detail(
        meeting=meeting,
        transcript="",
        live_notes="",
        existing_notes="",
        prompt_text="",
        prompt_template_name="",
        templates=[],
        export_destinations=[
            {"key": "obsidian", "label": "Obsidian vault",
             "configured": True, "enabled": True, "target_title": ""},
        ],
    )
    assert 'id="export"' in html
    assert "Save a synthesis below first" in html
    assert 'name="destination"' not in html


def test_detail_renders_sidebar_toc_with_section_ids():
    """Each left-nav link resolves to a section's id on the page."""
    meeting = {
        "id": "mid", "title": "sync", "state": "complete",
        "started_at": "", "ended_at": "", "duration_seconds": 0,
        "has_transcript": True, "has_notes": True, "attendees": [],
    }
    html = views.render_meeting_detail(
        meeting=meeting,
        transcript="t",
        live_notes="",
        existing_notes="body",
        prompt_text="",
        prompt_template_name="",
        templates=[],
        export_destinations=[],
    )
    assert 'class="toc"' in html
    assert 'class="with-sidebar"' in html
    for anchor in ("meta", "attendees", "live-notes", "transcript",
                   "current-synthesis", "export", "workflow"):
        assert f'id="{anchor}"' in html
        assert f'href="#{anchor}"' in html
