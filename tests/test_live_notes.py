"""Live-notes template seeding and attendee parsing."""
from __future__ import annotations

from datetime import datetime, timezone

from meeting_notetaker.models.transcript import TranscriptStore
from meeting_notetaker.utils import live_notes as ln
from meeting_notetaker.utils import prompts as prompts_mod


def test_seed_body_contains_required_sections():
    body = ln.seed_body()
    for section in ("# Attendees", "# Agenda", "# Notes", "# Action Items"):
        assert section in body


def test_parse_attendees_from_basic_bullets():
    body = """# Attendees
- Aaron
- Bob
- Charlie

# Agenda
- intro
"""
    assert ln.parse_attendees(body) == ["Aaron", "Bob", "Charlie"]


def test_parse_attendees_handles_asterisk_and_plus_bullets():
    body = """# Attendees
* Aaron
+ Bob
- Charlie
"""
    assert ln.parse_attendees(body) == ["Aaron", "Bob", "Charlie"]


def test_parse_attendees_strips_trailing_comments():
    body = """# Attendees
- Aaron -- host
- Bob # facilitator
- Charlie
"""
    assert ln.parse_attendees(body) == ["Aaron", "Bob", "Charlie"]


def test_parse_attendees_dedupes_case_insensitively():
    body = """# Attendees
- Aaron
- aaron
- Bob
"""
    assert ln.parse_attendees(body) == ["Aaron", "Bob"]


def test_parse_attendees_empty_seed_template_returns_empty_list():
    assert ln.parse_attendees(ln.seed_body()) == []


def test_parse_attendees_stops_at_next_heading():
    body = """# Attendees
- Aaron

# Agenda
- Topic A
- Topic B
"""
    assert ln.parse_attendees(body) == ["Aaron"]


def test_parse_attendees_handles_missing_section():
    body = "# Notes\n- Some note\n"
    assert ln.parse_attendees(body) == []


def test_has_user_content_false_for_seed():
    assert ln.has_user_content(ln.seed_body()) is False


def test_has_user_content_true_when_user_typed_anything():
    body = ln.seed_body() + "\n# Notes\n- Something\n"
    assert ln.has_user_content(body) is True


def test_format_attendee_list_with_names():
    assert ln.format_attendee_list(["Aaron", "Bob"]) == "Aaron, Bob"


def test_format_attendee_list_empty():
    assert ln.format_attendee_list([]) == "(none specified)"


def test_render_substitutes_live_notes_and_attendees():
    body = "Attendees: {{attendees}}\nNotes:\n{{live_notes}}\nT: {{transcript}}"
    live_notes = "# Attendees\n- Aaron\n- Bob\n\n# Notes\n- decided X\n"
    out = prompts_mod.render(
        body,
        session_title="x",
        session_date="2026-05-15",
        transcript="T body",
        live_notes=live_notes,
    )
    assert "Attendees: Aaron, Bob" in out
    assert "# Notes" in out
    assert "decided X" in out
    assert "T: T body" in out


def test_render_uses_explicit_attendees_when_provided():
    body = "{{attendees}}"
    out = prompts_mod.render(
        body,
        session_title="x",
        session_date="2026-05-15",
        transcript="",
        live_notes="# Attendees\n- Ignored\n",
        attendees=["Override"],
    )
    assert out == "Override"


def test_render_empty_live_notes_substitutes_placeholder():
    body = "{{live_notes}}"
    out = prompts_mod.render(
        body,
        session_title="x",
        session_date="2026-05-15",
        transcript="",
        live_notes=ln.seed_body(),
    )
    assert "none" in out.lower()


def test_transcript_store_seeds_live_notes_on_first_read(isolated_data_dir):
    store = TranscriptStore("test-session")
    body = store.read_live_notes()
    assert "# Attendees" in body
    # The path now exists with the seed content.
    assert store.live_notes_path.exists()


def test_transcript_store_save_then_read_live_notes_roundtrip(isolated_data_dir):
    store = TranscriptStore("test-session")
    store.save_live_notes("# Attendees\n- Aaron\n")
    assert store.read_live_notes() == "# Attendees\n- Aaron\n"
