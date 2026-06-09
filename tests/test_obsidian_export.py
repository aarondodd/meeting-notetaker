"""Tests for the Obsidian export module (#96).

Pure-Python; tmp-vault + tmp-session fixtures. No PyQt, no Obsidian
install required.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

import pytest

from meeting_notetaker.integrations.obsidian_export import (
    ObsidianPublishOptions,
    ObsidianSessionInfo,
    build_frontmatter,
    collect_local_image_refs,
    copy_image_dedupe,
    export_to_obsidian,
    find_republish_candidate,
    read_existing_frontmatter,
    resolve_local_image_path,
    resolve_location_template,
    rewrite_image_refs,
    sanitize_obsidian_path_segment,
    sanitize_obsidian_stem,
    truncate_filename_stem,
    unique_note_path,
)


# ---- filename sanitization ----------------------------------------------


def test_sanitize_strips_windows_set():
    assert sanitize_obsidian_stem('file/name\\with:bad*chars?"<>|') == "file name with bad chars"


def test_sanitize_strips_obsidian_set():
    assert sanitize_obsidian_stem("title [draft] #tag ^anchor") == "title draft tag anchor"


def test_sanitize_collapses_whitespace():
    assert sanitize_obsidian_stem("a   b  c") == "a b c"


def test_sanitize_returns_fallback_when_empty():
    assert sanitize_obsidian_stem("") == "Untitled"
    assert sanitize_obsidian_stem("   ", fallback="X") == "X"
    assert sanitize_obsidian_stem("[][]#^", fallback="X") == "X"


def test_sanitize_strips_trailing_dot():
    assert sanitize_obsidian_stem("title.") == "title"


def test_sanitize_path_segment_preserves_slashes():
    assert sanitize_obsidian_path_segment("a/b/c") == "a/b/c"
    assert sanitize_obsidian_path_segment("Meetings/2026/06") == "Meetings/2026/06"


def test_sanitize_path_segment_strips_per_piece():
    assert sanitize_obsidian_path_segment("a [b]/c#d") == "a b/c d"


def test_truncate_filename_stem_short_passes_through():
    assert truncate_filename_stem("short") == "short"


def test_truncate_filename_stem_clamps_long():
    long = "a" * 300
    out = truncate_filename_stem(long)
    assert len(out.encode("utf-8")) <= 200


# ---- location template --------------------------------------------------


def _info(**kw):
    base = dict(
        session_id="abc-123",
        title="Sample",
        started_at=datetime(2026, 6, 9, 14, 30),
    )
    base.update(kw)
    return ObsidianSessionInfo(**base)


def test_location_template_year_month():
    assert resolve_location_template(
        template_name="year_month", template_custom="",
        session=_info(),
    ) == "Meetings/2026/06"


def test_location_template_by_series():
    out = resolve_location_template(
        template_name="by_series", template_custom="",
        session=_info(series="Weekly Sync"),
    )
    assert out == "Meetings/Weekly Sync"


def test_location_template_by_series_empty_falls_back():
    out = resolve_location_template(
        template_name="by_series", template_custom="",
        session=_info(series=""),
    )
    assert out == "Meetings/Untitled"


def test_location_template_flat():
    assert resolve_location_template(
        template_name="flat", template_custom="",
        session=_info(),
    ) == "Meetings"


def test_location_template_custom():
    out = resolve_location_template(
        template_name="custom",
        template_custom="Notes/{YYYY}-{MM}/{title}",
        session=_info(title="Q3 Plan"),
    )
    assert out == "Notes/2026-06/Q3 Plan"


def test_location_template_custom_with_session_id():
    out = resolve_location_template(
        template_name="custom",
        template_custom="Notes/{session_id}",
        session=_info(session_id="abc-123"),
    )
    assert out == "Notes/abc-123"


def test_location_template_unknown_falls_back_to_year_month():
    out = resolve_location_template(
        template_name="bogus", template_custom="",
        session=_info(),
    )
    assert out == "Meetings/2026/06"


# ---- frontmatter --------------------------------------------------------


def test_frontmatter_basic_shape():
    info = _info(
        title="Standup",
        attendees=["Alice", "Bob"],
        series="Daily",
        tags=["sync"],
        duration_minutes=30,
    )
    opts = ObsidianPublishOptions(
        vault_root=Path("/v"), vault_name="v",
        target_subdir="", filename_stem="Standup",
    )
    out = build_frontmatter(info, opts)
    assert out.startswith("---\n")
    assert out.rstrip().endswith("---")
    assert "title: Standup" in out
    assert "date: 2026-06-09" in out
    assert "time: 14:30" in out
    assert "duration_minutes: 30" in out
    assert '"[[Alice]]"' in out
    assert '"[[Bob]]"' in out
    assert 'series: "[[Daily]]"' in out
    assert "tags: [sync]" in out
    assert "source_app: meeting-notetaker" in out
    assert "source_session_id: abc-123" in out


def test_frontmatter_classification_only_when_opted_in():
    info = _info(classification="confidential")
    opts = ObsidianPublishOptions(
        vault_root=Path("/v"), vault_name="v",
        target_subdir="", filename_stem="s",
        include_classification=False,
    )
    assert "classification:" not in build_frontmatter(info, opts)
    opts.include_classification = True
    assert "classification: confidential" in build_frontmatter(info, opts)


def test_frontmatter_wikilink_attendees_off():
    info = _info(attendees=["Alice", "Bob"])
    opts = ObsidianPublishOptions(
        vault_root=Path("/v"), vault_name="v",
        target_subdir="", filename_stem="s",
        wikilink_attendees=False,
    )
    out = build_frontmatter(info, opts)
    assert "[[Alice]]" not in out
    assert "- Alice" in out


def test_frontmatter_wikilink_series_off():
    info = _info(series="My Series")
    opts = ObsidianPublishOptions(
        vault_root=Path("/v"), vault_name="v",
        target_subdir="", filename_stem="s",
        wikilink_series=False,
    )
    out = build_frontmatter(info, opts)
    assert "[[My Series]]" not in out
    assert "series: My Series" in out


def test_frontmatter_disabled_returns_empty():
    info = _info()
    opts = ObsidianPublishOptions(
        vault_root=Path("/v"), vault_name="v",
        target_subdir="", filename_stem="s",
        write_frontmatter=False,
    )
    assert build_frontmatter(info, opts) == ""


def test_frontmatter_strips_forbidden_chars_from_wikilink_target():
    info = _info(attendees=["Alice [Cooper] #1"])
    opts = ObsidianPublishOptions(
        vault_root=Path("/v"), vault_name="v",
        target_subdir="", filename_stem="s",
    )
    out = build_frontmatter(info, opts)
    assert "[[Alice Cooper 1]]" in out


def test_frontmatter_quotes_scalars_with_special_chars():
    info = _info(title="weird: title")
    opts = ObsidianPublishOptions(
        vault_root=Path("/v"), vault_name="v",
        target_subdir="", filename_stem="s",
    )
    out = build_frontmatter(info, opts)
    assert 'title: "weird: title"' in out


def test_frontmatter_skips_empty_attendees():
    info = _info(attendees=["", "  ", "Real"])
    opts = ObsidianPublishOptions(
        vault_root=Path("/v"), vault_name="v",
        target_subdir="", filename_stem="s",
    )
    out = build_frontmatter(info, opts)
    assert '"[[Real]]"' in out
    # exactly one attendee line:
    assert out.count("  - ") == 1


def test_frontmatter_tag_token_cleanup():
    info = _info(tags=["hello world", "two,three", "[brackets]"])
    opts = ObsidianPublishOptions(
        vault_root=Path("/v"), vault_name="v",
        target_subdir="", filename_stem="s",
    )
    out = build_frontmatter(info, opts)
    # space + comma + brackets all collapsed to dashes
    assert "tags: [hello-world, two-three, brackets]" in out


# ---- re-publish detection -----------------------------------------------


def test_find_republish_candidate_finds_matching_id(tmp_path):
    target = tmp_path / "Meetings" / "Note.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        "---\ntitle: x\nsource_session_id: abc-123\n---\n\nbody\n",
        encoding="utf-8",
    )
    cand = find_republish_candidate(search_root=tmp_path, session_id="abc-123")
    assert cand is not None
    assert cand.existing_path == target


def test_find_republish_candidate_skips_nonmatching(tmp_path):
    target = tmp_path / "Note.md"
    target.write_text(
        "---\ntitle: x\nsource_session_id: other-id\n---\n\nbody\n",
        encoding="utf-8",
    )
    assert find_republish_candidate(
        search_root=tmp_path, session_id="abc-123",
    ) is None


def test_find_republish_candidate_handles_missing_frontmatter(tmp_path):
    (tmp_path / "Note.md").write_text("no frontmatter\n", encoding="utf-8")
    assert find_republish_candidate(
        search_root=tmp_path, session_id="abc-123",
    ) is None


def test_read_existing_frontmatter_block_sequence(tmp_path):
    p = tmp_path / "n.md"
    p.write_text(
        "---\ntitle: x\nattendees:\n  - Alice\n  - Bob\n---\nbody\n",
        encoding="utf-8",
    )
    fm = read_existing_frontmatter(p)
    assert fm is not None
    assert fm["title"] == "x"
    assert fm["attendees"] == ["Alice", "Bob"]


def test_read_existing_frontmatter_inline_sequence(tmp_path):
    p = tmp_path / "n.md"
    p.write_text("---\ntags: [a, b, c]\n---\n", encoding="utf-8")
    fm = read_existing_frontmatter(p)
    assert fm == {"tags": ["a", "b", "c"]}


# ---- image refs ---------------------------------------------------------


def test_collect_local_image_refs():
    body = (
        "Text ![alt1](images/a.png)\n"
        "More ![alt2](https://example.com/b.png)\n"
        "Inline ![](c.jpg)\n"
        "Datauri ![](data:image/png;base64,xxx)\n"
    )
    refs = collect_local_image_refs(body)
    assert ("alt1", "images/a.png") in refs
    assert ("", "c.jpg") in refs
    # remote + data: are excluded
    assert all("example.com" not in url for _, url in refs)
    assert all(not url.startswith("data:") for _, url in refs)


def test_rewrite_image_refs():
    body = "x ![alt](images/foo.png) y"
    out = rewrite_image_refs(body, rewrites={"images/foo.png": "../assets/foo.png"})
    assert out == "x ![alt](../assets/foo.png) y"


def test_rewrite_image_refs_no_match_unchanged():
    body = "x ![alt](images/foo.png) y"
    assert rewrite_image_refs(body, rewrites={}) == body


def test_resolve_local_image_path(tmp_path):
    base = tmp_path / "session"
    base.mkdir()
    (base / "images").mkdir()
    (base / "images" / "foo.png").write_bytes(b"x")
    found = resolve_local_image_path("images/foo.png", base_dir=base)
    assert found == (base / "images" / "foo.png").resolve()
    assert resolve_local_image_path("missing.png", base_dir=base) is None


def test_copy_image_dedupe_no_collision(tmp_path):
    src = tmp_path / "src.png"
    src.write_bytes(b"abc")
    dest = tmp_path / "out"
    out = copy_image_dedupe(src=src, dest_dir=dest)
    assert out == dest / "src.png"
    assert out.read_bytes() == b"abc"


def test_copy_image_dedupe_identical_reuses(tmp_path):
    src = tmp_path / "src.png"
    src.write_bytes(b"abc")
    dest = tmp_path / "out"
    a = copy_image_dedupe(src=src, dest_dir=dest)
    b = copy_image_dedupe(src=src, dest_dir=dest)
    assert a == b


def test_copy_image_dedupe_different_suffixes(tmp_path):
    src = tmp_path / "src.png"
    src.write_bytes(b"abc")
    dest = tmp_path / "out"
    a = copy_image_dedupe(src=src, dest_dir=dest)
    src.write_bytes(b"different content")
    b = copy_image_dedupe(src=src, dest_dir=dest)
    assert a != b
    assert b.name == "src-2.png"


# ---- unique_note_path ---------------------------------------------------


def test_unique_note_path_returns_target_when_absent(tmp_path):
    p = tmp_path / "n.md"
    assert unique_note_path(p) == p


def test_unique_note_path_counter_suffix(tmp_path):
    (tmp_path / "n.md").touch()
    (tmp_path / "n-2.md").touch()
    out = unique_note_path(tmp_path / "n.md")
    assert out.name == "n-3.md"


# ---- end-to-end export --------------------------------------------------


def test_export_writes_note_with_frontmatter_and_relative_image(tmp_path):
    vault = tmp_path / "Vault"
    vault.mkdir()
    sess_dir = tmp_path / "session"
    sess_dir.mkdir()
    (sess_dir / "images").mkdir()
    (sess_dir / "images" / "foo.png").write_bytes(b"img")
    info = ObsidianSessionInfo(
        session_id="abc",
        title="Hello",
        started_at=datetime(2026, 6, 9, 14, 30),
        attendees=["Alice"],
        series="Weekly",
        tags=["x"],
    )
    opts = ObsidianPublishOptions(
        vault_root=vault, vault_name="Vault",
        target_subdir="Meetings/2026/06",
        filename_stem="Hello",
    )
    body = "See ![alt](images/foo.png)"
    res = export_to_obsidian(
        session=info, body=body, options=opts, session_dir=sess_dir,
    )
    note = vault / "Meetings" / "2026" / "06" / "Hello.md"
    assert note.is_file()
    text = note.read_text()
    assert "source_session_id: abc" in text
    # image was copied + ref rewritten relative
    img = vault / "Meetings" / "_assets" / "abc" / "foo.png"
    assert img.is_file()
    assert "../../_assets/abc/foo.png" in text
    assert res.target == "obsidian"
    assert res.page_url.startswith("file://")


def test_export_counter_suffix_on_existing_file(tmp_path):
    vault = tmp_path / "Vault"
    vault.mkdir()
    sess_dir = tmp_path / "session"
    sess_dir.mkdir()
    info = ObsidianSessionInfo(session_id="abc", title="t")
    opts = ObsidianPublishOptions(
        vault_root=vault, vault_name="Vault",
        target_subdir="Notes", filename_stem="t",
    )
    res1 = export_to_obsidian(
        session=info, body="body", options=opts, session_dir=sess_dir,
    )
    res2 = export_to_obsidian(
        session=info, body="body", options=opts, session_dir=sess_dir,
    )
    assert (vault / "Notes" / "t.md").is_file()
    assert (vault / "Notes" / "t-2.md").is_file()
    assert "t.md" in res1.page_url
    assert "t-2.md" in res2.page_url


def test_export_overwrite_uses_target_path(tmp_path):
    vault = tmp_path / "Vault"
    vault.mkdir()
    sess_dir = tmp_path / "session"
    sess_dir.mkdir()
    info = ObsidianSessionInfo(session_id="abc", title="t")
    opts = ObsidianPublishOptions(
        vault_root=vault, vault_name="Vault",
        target_subdir="Notes", filename_stem="t",
    )
    export_to_obsidian(
        session=info, body="first", options=opts, session_dir=sess_dir,
    )
    opts.on_conflict = "overwrite"
    res = export_to_obsidian(
        session=info, body="second body", options=opts, session_dir=sess_dir,
    )
    note = vault / "Notes" / "t.md"
    assert "second body" in note.read_text()
    assert "first" not in note.read_text()
    assert "Notes/t.md" in res.page_url


def test_export_does_not_emit_table_of_contents(tmp_path):
    """Per issue #96: Obsidian's outline view handles TOC; we never
    emit one. Verifies no 'Contents' heading + no inline TOC bullet
    list in the output."""
    vault = tmp_path / "Vault"
    vault.mkdir()
    sess_dir = tmp_path / "session"
    sess_dir.mkdir()
    info = ObsidianSessionInfo(session_id="abc", title="t")
    opts = ObsidianPublishOptions(
        vault_root=vault, vault_name="Vault",
        target_subdir="Notes", filename_stem="t",
    )
    body = "# Section A\n\nbody\n\n# Section B\n\nmore\n"
    export_to_obsidian(
        session=info, body=body, options=opts, session_dir=sess_dir,
    )
    note = vault / "Notes" / "t.md"
    text = note.read_text()
    assert "## Contents" not in text
    assert "- [Section A](#" not in text


def test_export_daily_note_backlink(tmp_path):
    vault = tmp_path / "Vault"
    vault.mkdir()
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "daily-notes.json").write_text(
        json.dumps({"folder": "Journal", "format": "YYYY-MM-DD"}),
        encoding="utf-8",
    )
    sess_dir = tmp_path / "session"
    sess_dir.mkdir()
    info = ObsidianSessionInfo(
        session_id="abc",
        title="Sync",
        started_at=datetime(2026, 6, 9, 14, 30),
        duration_minutes=30,
    )
    opts = ObsidianPublishOptions(
        vault_root=vault, vault_name="Vault",
        target_subdir="Meetings", filename_stem="Sync",
        daily_note_backlink=True,
    )
    export_to_obsidian(
        session=info, body="body", options=opts, session_dir=sess_dir,
    )
    daily = vault / "Journal" / "2026-06-09.md"
    assert daily.is_file()
    text = daily.read_text()
    assert "Meetings/Sync" in text
    assert "Sync" in text
    assert "30 min" in text


def test_export_daily_note_backlink_idempotent(tmp_path):
    """Calling export twice doesn't double-append the backlink."""
    vault = tmp_path / "Vault"
    vault.mkdir()
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "daily-notes.json").write_text(
        json.dumps({"folder": "", "format": "YYYY-MM-DD"}),
        encoding="utf-8",
    )
    sess_dir = tmp_path / "session"
    sess_dir.mkdir()
    info = ObsidianSessionInfo(
        session_id="abc",
        title="Sync",
        started_at=datetime(2026, 6, 9, 14, 30),
    )
    opts = ObsidianPublishOptions(
        vault_root=vault, vault_name="Vault",
        target_subdir="Meetings", filename_stem="Sync",
        daily_note_backlink=True,
        on_conflict="overwrite",
    )
    export_to_obsidian(
        session=info, body="body", options=opts, session_dir=sess_dir,
    )
    export_to_obsidian(
        session=info, body="body", options=opts, session_dir=sess_dir,
    )
    daily = vault / "2026-06-09.md"
    text = daily.read_text()
    # The backlink line appears exactly once, even after two saves.
    assert text.count("[[Meetings/Sync|Sync]]") == 1
