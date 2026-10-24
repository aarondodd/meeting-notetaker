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
        folders=[{"id": "fid1", "name": "Topic X"}],
        filters={"state": "complete", "has_transcript": "1", "limit": "25"},
        total_shown=1,
    )
    assert "Alpha" in html
    assert "complete" in html
    assert "Topic X" in html
    # Filter echoed in form values.
    assert 'value="25"' in html
    assert 'checked' in html


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
