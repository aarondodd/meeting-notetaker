"""Pins the Claude Agent Skill shipped under ``claude-skill/``.

The skill is a one-way hand-off to the Chrome Claude extension: once
a user imports it, the extension drives Meeting Notetaker's web UI
by the structural details named inside SKILL.md. Any change to those
details on the Python side (route path, form name, prompt pane id,
filter query parameter) has to land in the skill at the same time or
the skill goes stale silently.

These checks are structural, not semantic: they verify the skill
exists, parses, and names every anchor the HTML surface currently
exposes. Content review stays a human read of the file.
"""
from __future__ import annotations

import re
from pathlib import Path


SKILL_PATH = Path(__file__).resolve().parent.parent / "claude-skill" / "SKILL.md"


def _read_skill() -> str:
    assert SKILL_PATH.exists(), f"missing {SKILL_PATH}"
    return SKILL_PATH.read_text(encoding="utf-8")


def test_skill_has_well_formed_frontmatter():
    body = _read_skill()
    match = re.match(r"^---\n(.*?)\n---\n", body, flags=re.DOTALL)
    assert match, "SKILL.md must open with a YAML frontmatter block"
    fm = match.group(1)
    assert re.search(r"^name:\s*meeting-notetaker\s*$", fm, flags=re.MULTILINE), (
        "frontmatter must declare name: meeting-notetaker"
    )
    desc_match = re.search(r"^description:\s*(.+)$", fm, flags=re.MULTILINE)
    assert desc_match, "frontmatter must declare a description"
    # The description is what Claude uses to decide when to activate
    # the skill; keep it long enough to carry real trigger keywords.
    assert len(desc_match.group(1)) > 120, (
        "description looks too short to trigger reliably"
    )


def test_skill_names_core_routes_and_form_handles():
    body = _read_skill()
    # Each anchor below is a structural detail the HTML surface
    # exposes; a change there needs a matching skill edit.
    required = [
        "/meetings",
        "/meetings/<id>",
        "/meetings/<id>/save",
        "/meetings/<id>/template",
        '/meetings/<id>/export"',
        "/meetings/<id>/export/obsidian",
        "/meetings/<id>/export/notion",
        "/meetings/<id>/export/confluence",
        "/templates",
        "/healthz",
        "127.0.0.1",
        "8765",
        'id="rendered-prompt"',
        'name="notes"',
        "Save synthesis",
        'name="destination"',
        "has_transcript",
        "has_notes",
        "series_id",
        "contact_id",
        "topic_id",
        "/api/classification",
        "/api/export_destinations",
    ]
    missing = [token for token in required if token not in body]
    assert not missing, f"SKILL.md is missing anchors: {missing}"


def test_skill_covers_both_single_and_cross_meeting_workflows():
    body = _read_skill()
    # The two workflow shapes the user explicitly asked for.
    assert "Canonical workflow" in body
    assert "Cross-meeting" in body or "cross-meeting" in body


def test_skill_tells_the_extension_to_grant_loopback_site_access():
    body = _read_skill()
    assert "127.0.0.1" in body
    assert "Site Access" in body
