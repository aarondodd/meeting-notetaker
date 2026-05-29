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
    # include_system_prompts=False isolates the substitution behavior;
    # appendix injection is tested separately in test_prompts_system_appendix.
    out = prompts_mod.render(
        body, session_title="X", session_date="2026-05-15",
        transcript="", include_system_prompts=False,
    )
    assert out == "X -- {{unknown_thing}}"


def test_render_accepts_pre_formatted_date_string(isolated_data_dir):
    out = prompts_mod.render(
        "{{date}}", session_title="x", session_date="custom",
        transcript="", include_system_prompts=False,
    )
    assert out == "custom"


def test_bundled_templates_use_live_notes_placeholder_once(isolated_data_dir):
    """Every {{live_notes}} occurrence in a bundled template should be the
    actual insertion point for the user's notes -- not a placeholder used
    as a textual reference. Multiple occurrences cause the full notes
    blob to get spliced into prompt prose, which broke the rendered
    prompt prior to this regression test.
    """
    for name in ("default", "one-on-one", "standup"):
        tpl = prompts_mod.get_template(name)
        assert tpl is not None, f"missing bundled template: {name}"
        count = tpl.body.count("{{live_notes}}")
        assert count == 1, (
            f"{name}.md has {count} {{live_notes}} occurrences; "
            "expected exactly 1 (the final substitution point)."
        )


def test_seed_refreshes_unmodified_prior_bundled_template(isolated_data_dir):
    """If a user file matches a prior-shipped hash, re-seed refreshes it."""
    user_dir = prompts_dir()
    # First seed populates the current bundled bodies.
    prompts_mod.seed_user_prompts()
    # Simulate "user has the v0.1 default.md unchanged from prior release"
    # by writing the v0.1 body whose hash is in _PRIOR_BUNDLED_HASHES.
    v01_default = (
        "You are summarizing a meeting transcript. The transcript is labeled by\n"
        "source: lines starting with \"Me:\" are the user's microphone; \"Them:\"\n"
        "are the system audio (other meeting participants). Produce:\n\n"
        "1. A 3-bullet TL;DR.\n"
        "2. Decisions made (or \"none\").\n"
        "3. Action items as `[ ] Owner -- task` lines. If owner is unclear, use \"TBD\".\n"
        "4. Open questions.\n"
        "5. Verbatim quotes for any commitment or numbered fact.\n\n"
        "Use plain ASCII. Do not invent content the transcript does not support.\n\n"
        "Session: {{session_title}}\n"
        "Date: {{date}}\n\n"
        "Transcript:\n"
        "{{transcript}}\n"
    )
    (user_dir / "default.md").write_text(v01_default, encoding="utf-8")
    written = prompts_mod.seed_user_prompts()
    assert written >= 1
    # File now contains the current bundled body, which includes the new merge
    # instructions referencing live_notes.
    body = (user_dir / "default.md").read_text(encoding="utf-8")
    assert "{{live_notes}}" in body


def test_seed_preserves_user_modified_template(isolated_data_dir):
    user_dir = prompts_dir()
    prompts_mod.seed_user_prompts()
    custom = "MY CUSTOM PROMPT\n{{transcript}}\n"
    (user_dir / "default.md").write_text(custom, encoding="utf-8")
    prompts_mod.seed_user_prompts()
    assert (user_dir / "default.md").read_text(encoding="utf-8") == custom


def test_seed_refreshes_unmodified_template_with_crlf_line_endings(isolated_data_dir):
    """A Git-on-Windows checkout converts LF to CRLF. The upgrade check must
    still recognize that body as 'unmodified from prior bundle' and refresh it."""
    user_dir = prompts_dir()
    prompts_mod.seed_user_prompts()
    v01_default_lf = (
        "You are summarizing a meeting transcript. The transcript is labeled by\n"
        "source: lines starting with \"Me:\" are the user's microphone; \"Them:\"\n"
        "are the system audio (other meeting participants). Produce:\n\n"
        "1. A 3-bullet TL;DR.\n"
        "2. Decisions made (or \"none\").\n"
        "3. Action items as `[ ] Owner -- task` lines. If owner is unclear, use \"TBD\".\n"
        "4. Open questions.\n"
        "5. Verbatim quotes for any commitment or numbered fact.\n\n"
        "Use plain ASCII. Do not invent content the transcript does not support.\n\n"
        "Session: {{session_title}}\n"
        "Date: {{date}}\n\n"
        "Transcript:\n"
        "{{transcript}}\n"
    )
    crlf = v01_default_lf.replace("\n", "\r\n")
    (user_dir / "default.md").write_bytes(crlf.encode("utf-8"))
    written = prompts_mod.seed_user_prompts()
    assert written >= 1
    body = (user_dir / "default.md").read_text(encoding="utf-8")
    assert "{{live_notes}}" in body


def test_seed_refreshes_v02_template_to_v03(isolated_data_dir):
    """v0.2 templates (merged-synthesis, no {{user_name}}) should be refreshed
    to the current bundled body when the user hasn't customized them."""
    user_dir = prompts_dir()
    prompts_mod.seed_user_prompts()
    v02_default_lf = (
        "You are synthesizing a meeting from two sources written in parallel:\n\n"
        "1. A live transcript labeled by source -- \"Me:\" is the user's microphone, \"Them:\" is the system audio (other participants).\n"
        "2. The user's own running notes (\"live notes\") taken during the meeting. These reflect the user's framing, emphasis, and any pre-meeting context (agenda, prior decisions) that does not appear in the transcript.\n\n"
        "Merge the two. Treat the user's live notes as the source of truth for intent and any pre-meeting context (the agenda especially). Refine and expand them with what the transcript supports; add transcript-only content the user did not capture. Do not contradict the user's notes unless the transcript clearly does -- if so, flag the conflict under \"Open Questions\".\n\n"
        "Known attendees: {{attendees}}\n"
        "When assigning Action Items, prefer one of these names as the owner. Use \"TBD\" only if no attendee is plausibly the owner from context.\n\n"
        "Produce, in this order, in plain ASCII markdown:\n\n"
        "# Attendees\n"
        "- Carry over the user's list. Add anyone the transcript reveals who was not listed.\n\n"
        "# Agenda\n"
        "- Carry over the user's agenda exactly. If the transcript shows the meeting deviated, note that under \"Open Questions\", not here.\n\n"
        "# TL;DR\n"
        "- 3 bullets, what a manager would want to know in 20 seconds.\n\n"
        "# Decisions\n"
        "- One line per decision. If no decisions were made, write \"none\".\n\n"
        "# Notes\n"
        "- The merged narrative. Start from the user's \"# Notes\" content, refine and expand with transcript-supported detail. Use subheadings where helpful.\n\n"
        "# Action Items\n"
        "- Each item as `[ ] Owner -- task`. Owner is an attendee name or \"TBD\".\n\n"
        "# Open Questions\n"
        "- Anything the meeting did not resolve, plus any conflicts between the user's notes and the transcript.\n\n"
        "# Verbatim Quotes\n"
        "- Any commitment, numbered fact, or notably-phrased statement. Format as `Speaker: \"quote\"`.\n\n"
        "Do not invent content beyond what the transcript and live notes support.\n\n"
        "Session: {{session_title}}\n"
        "Date: {{date}}\n\n"
        "User's Live Notes:\n"
        "{{live_notes}}\n\n"
        "Transcript:\n"
        "{{transcript}}\n"
    )
    (user_dir / "default.md").write_text(v02_default_lf, encoding="utf-8")
    written = prompts_mod.seed_user_prompts()
    assert written >= 1
    body = (user_dir / "default.md").read_text(encoding="utf-8")
    assert "{{user_name}}" in body
