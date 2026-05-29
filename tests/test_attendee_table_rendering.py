"""Attendees-as-table rendering for the PDF export path (#51 Phase 5).

When session contacts have rich-field data (title, company, department,
email, phone), the PDF render path swaps the bullet list under the
Attendees heading for a Markdown table. Without rich data the bullet
list stays. Empty columns are omitted from the table so a meeting with
only titles doesn't grow a useless Phone column.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from meeting_notetaker.utils.live_notes import (
    replace_attendees_section_with_table,
    should_render_attendees_as_table,
)


@dataclass
class FakeContact:
    display_name: str
    title: Optional[str] = None
    company: Optional[str] = None
    department: Optional[str] = None
    primary_email: Optional[str] = None
    phone: Optional[str] = None


# ---- should_render_attendees_as_table --------------------------------------


def test_should_render_false_for_names_only():
    contacts = [FakeContact(display_name="Bob"), FakeContact(display_name="Mary")]
    assert should_render_attendees_as_table(contacts) is False


def test_should_render_true_when_any_rich_field_set():
    contacts = [
        FakeContact(display_name="Bob", title="CEO"),
        FakeContact(display_name="Mary"),
    ]
    assert should_render_attendees_as_table(contacts) is True


def test_should_render_false_for_empty_list():
    assert should_render_attendees_as_table([]) is False
    assert should_render_attendees_as_table(None) is False


def test_should_render_recognizes_each_rich_field():
    for field in ("title", "company", "department", "primary_email", "phone"):
        contact = FakeContact(display_name="Bob", **{field: "value"})
        assert should_render_attendees_as_table([contact]) is True, field


# ---- replace_attendees_section_with_table ----------------------------------


def test_replace_swaps_bullets_for_table():
    body = """# Attendees
- Bob
- Mary

# Notes
- decided X
"""
    contacts = [
        FakeContact(display_name="Bob", title="CEO", company="Bobco"),
        FakeContact(display_name="Mary", title="VP"),
    ]
    out = replace_attendees_section_with_table(body, contacts)
    # Heading preserved.
    assert "# Attendees" in out
    # Bullet markers under the heading are gone (the original "- Bob" line).
    assert "- Bob" not in out
    assert "- Mary" not in out
    # Table headers present.
    assert "| Name | Title | Company |" in out
    # Row content present.
    assert "| Bob | CEO | Bobco |" in out
    # Notes section preserved unchanged.
    assert "# Notes" in out
    assert "- decided X" in out


def test_replace_omits_columns_with_no_data():
    """Only columns with at least one non-empty cell appear in the table."""
    body = "# Attendees\n- Bob\n\n# Notes\n"
    contacts = [FakeContact(display_name="Bob", title="CEO")]
    out = replace_attendees_section_with_table(body, contacts)
    # Title column present, Phone / Email / Company / Department omitted.
    assert "Title" in out
    assert "Phone" not in out
    assert "Email" not in out
    assert "Company" not in out
    assert "Department" not in out


def test_replace_preserves_body_when_no_attendees_section():
    """Body without an Attendees heading is preserved content-wise."""
    body = "# Notes\n- thing happened\n"
    contacts = [FakeContact(display_name="Bob", title="CEO")]
    out = replace_attendees_section_with_table(body, contacts)
    # Content preserved (trailing newline may differ from splitlines() round-trip).
    assert out.rstrip("\n") == body.rstrip("\n")


def test_replace_no_op_with_no_contacts():
    """No contacts -> body unchanged."""
    body = "# Attendees\n- Bob\n\n# Notes\n"
    assert replace_attendees_section_with_table(body, []) == body


def test_replace_returns_body_when_no_rich_fields_set():
    """All contacts names-only -> only-names table would be useless;
    function falls back to returning the body unchanged so the caller's
    auto-detect rule + this guard agree."""
    body = "# Attendees\n- Bob\n- Mary\n"
    contacts = [
        FakeContact(display_name="Bob"),
        FakeContact(display_name="Mary"),
    ]
    out = replace_attendees_section_with_table(body, contacts)
    # Single-column tables aren't worth it; helper returns body
    # unchanged so the bullet list survives.
    assert out == body


def test_replace_preserves_other_sections_verbatim():
    body = """# Attendees
- Bob

# Agenda
- topic A
- topic B

# Notes
- note 1

# Action Items
- thing
"""
    contacts = [FakeContact(display_name="Bob", title="CEO")]
    out = replace_attendees_section_with_table(body, contacts)
    for section in (
        "# Agenda",
        "- topic A",
        "- topic B",
        "# Notes",
        "- note 1",
        "# Action Items",
        "- thing",
    ):
        assert section in out


def test_replace_handles_blank_lines_between_attendees_and_next_heading():
    body = "# Attendees\n- Bob\n\n\n\n# Notes\n"
    contacts = [FakeContact(display_name="Bob", title="CEO")]
    out = replace_attendees_section_with_table(body, contacts)
    assert "# Notes" in out
    # No bullet survives.
    assert "- Bob" not in out


def test_replace_dedent_table_row_strips_empty_only_cells():
    """An empty title on one contact + filled title on another -> the
    column shows up, but the empty cell is a single space (not blank)."""
    body = "# Attendees\n- Bob\n- Mary\n"
    contacts = [
        FakeContact(display_name="Bob", title="CEO"),
        FakeContact(display_name="Mary"),  # no title
    ]
    out = replace_attendees_section_with_table(body, contacts)
    assert "| Bob | CEO |" in out
    # Mary's row keeps the column with a single-space placeholder.
    assert "| Mary |   |" in out


def test_replace_renders_all_rich_columns_when_populated():
    contacts = [
        FakeContact(
            display_name="Bob",
            title="CEO",
            company="Bobco",
            department="Eng",
            primary_email="bob@bobco.com",
            phone="555-0100",
        ),
    ]
    body = "# Attendees\n- Bob\n"
    out = replace_attendees_section_with_table(body, contacts)
    for header in ("Name", "Title", "Company", "Department", "Email", "Phone"):
        assert header in out
    for value in ("Bob", "CEO", "Bobco", "Eng", "bob@bobco.com", "555-0100"):
        assert value in out
