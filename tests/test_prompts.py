"""Prompt seeding, template listing, render substitution."""
from __future__ import annotations

from datetime import datetime, timezone

from meeting_notetaker.utils import prompts as prompts_mod
from meeting_notetaker.utils.paths import prompts_dir


def test_seed_copies_bundled_templates_once(isolated_data_dir):
    user_dir = prompts_dir()
    # First call seeds three bundled templates.
    n1 = prompts_mod.seed_user_prompts()
    assert n1 == 3
    assert {p.name for p in user_dir.glob("*.md")} == {"default.md", "one-on-one.md", "standup.md"}
    # Second call is a no-op (user files always win).
    n2 = prompts_mod.seed_user_prompts()
    assert n2 == 0


def test_user_edits_persist_across_seeding(isolated_data_dir):
    user_dir = prompts_dir()
    prompts_mod.seed_user_prompts()
    (user_dir / "default.md").write_text("custom body", encoding="utf-8")
    prompts_mod.seed_user_prompts()
    assert (user_dir / "default.md").read_text(encoding="utf-8") == "custom body"


def test_list_templates_orders_alphabetically(isolated_data_dir):
    templates = prompts_mod.list_templates()
    names = [t.name for t in templates]
    assert names == sorted(names)
    assert "default" in names


def test_get_template_by_name(isolated_data_dir):
    tpl = prompts_mod.get_template("default")
    assert tpl is not None
    assert tpl.name == "default"
    assert "{{transcript}}" in tpl.body


def test_render_substitutes_three_placeholders(isolated_data_dir):
    body = "Title: {{session_title}}\nDate: {{date}}\n---\n{{transcript}}"
    out = prompts_mod.render(
        body,
        session_title="Test Call",
        session_date=datetime(2026, 5, 15, 9, 30, tzinfo=timezone.utc),
        transcript="Me: hi\nThem: hi",
    )
    assert "Title: Test Call" in out
    assert "Date: 2026-05-15 09:30" in out
    assert "Me: hi\nThem: hi" in out


def test_render_leaves_unknown_placeholders_intact(isolated_data_dir):
    body = "{{session_title}} -- {{unknown_thing}}"
    out = prompts_mod.render(body, session_title="X", session_date="2026-05-15", transcript="")
    assert out == "X -- {{unknown_thing}}"


def test_render_accepts_pre_formatted_date_string(isolated_data_dir):
    out = prompts_mod.render(
        "{{date}}", session_title="x", session_date="custom", transcript=""
    )
    assert out == "custom"
